#!/usr/bin/env python3
"""Regenerate the side-by-side comparison of the HL7 Austria and HL7 International
AI Transparency FHIR IGs.

    python3 build.py              # runs SUSHI in both repos, then renders index.html
    python3 build.py --no-sushi   # reuse the existing fsh-generated/ output
    python3 build.py --selftest   # additionally loads index.html in headless Chrome
                                  # and checks that every ref and interaction works
    python3 build.py --issues HL7Austria/AT-INTL-AI-Transparency-IG-Comparison --out site
                                  # items + discussion threads from the GitHub issues (what CI does)

Inputs (read-only, apart from SUSHI writing the gitignored fsh-generated/):
    ../../AIST-TrustworthyAI-R5   (left,  HL7 Austria, FHIR R5)
    ../../aitransparency-ig       (right, HL7 International, FHIR R4)
    comparison.json               (summary, EU scenarios, analysed commits; no items)
    GitHub issues (--issues)      (the items: one issue per difference, see issues.py)
    diffs.json                    (offline fallback: items + summary in one file, if present)
    template.html                 (page shell: CSS + JS)

Outputs (next to this script):
    index.html      self-contained viewer (inline CSS/JS, works from file://)
    compared.json   git commits, dates and tool versions of this build

Every ref in diffs.json must resolve to an anchor rendered from the sources. Refs that no
longer resolve after an IG update are reported here and shown as a banner in the page, as is
a mismatch between the commits now compared and the commits diffs.json was curated against.

Requires Python 3.9+ and PyYAML; Pillow is optional (only used to shrink embedded images).
FHIR core packages (hl7.fhir.r4.core#4.0.1, hl7.fhir.r5.core#5.0.0) are read from
~/.fhir/packages for the base cardinalities shown next to each differential element.
"""
import argparse
import base64
import datetime
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
def _ig_dir(ci_name, sibling):
    """CI checks the IGs out into igs/<side> (pinned in compare.yaml); locally they sit next to this repo."""
    return HERE / "igs" / ci_name if (HERE / "igs" / ci_name).exists() else HERE.parent / sibling
PACKAGES = Path(os.path.expanduser("~/.fhir/packages"))

SIDES = {
    "at": {"label": "HL7 Austria", "short": "AT", "default_repo": _ig_dir("at", "AIST-TrustworthyAI-R5")},
    "intl": {"label": "HL7 International", "short": "Intl", "default_repo": _ig_dir("intl", "aitransparency-ig")},
}

esc = html.escape


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def git_info(repo):
    def g(*a):
        r = run(["git", *a], repo)
        return r.stdout.strip() if r.returncode == 0 else None
    return {
        "path": str(repo),
        "commit": g("rev-parse", "HEAD"),
        "commit_short": g("rev-parse", "--short", "HEAD"),
        "commit_date": g("log", "-1", "--format=%cI"),
        "subject": g("log", "-1", "--format=%s"),
        "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
        "remote": g("config", "--get", "remote.origin.url"),
        "dirty": bool(g("status", "--porcelain")),
    }


def slugify(text):
    s = re.sub(r"<[^>]+>", "", text)
    s = html.unescape(s).lower()
    s = re.sub(r"[`*_]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "section"


def short_url(u):
    if not u:
        return ""
    return u.rstrip("/").split("/")[-1].split("|")[0]


# --------------------------------------------------------------------------------------
# FHIR core packages: base element cardinalities, for context in the element tables
# --------------------------------------------------------------------------------------

class Core:
    def __init__(self, fhir_version):
        name = {"4.0.1": "hl7.fhir.r4.core#4.0.1", "5.0.0": "hl7.fhir.r5.core#5.0.0"}.get(fhir_version)
        self.dir = PACKAGES / name / "package" if name else None
        self.cache = {}

    def sd(self, type_name):
        if type_name in self.cache:
            return self.cache[type_name]
        res = None
        if self.dir:
            p = self.dir / f"StructureDefinition-{type_name}.json"
            if p.exists():
                sd = json.loads(p.read_text())
                res = {e["path"]: e for e in sd.get("snapshot", {}).get("element", [])}
        self.cache[type_name] = res
        return res

    def element(self, path):
        """Resolve a slice-free element path, walking into datatypes when needed."""
        segs = path.split(".")
        rel, i = [segs[0]], 1
        cur_type = segs[0]
        for _ in range(10):
            els = self.sd(cur_type)
            if els is None:
                return None
            full = ".".join(rel + segs[i:])
            if full in els:
                return els[full]
            moved = False
            for j in range(len(segs) - 1, i - 1, -1):
                cand = ".".join(rel + segs[i:j])
                if cand in els:
                    for t in [t["code"] for t in els[cand].get("type", [])]:
                        sub = self.sd(t)
                        if sub and f"{t}.{segs[j]}" in sub:
                            cur_type, rel, i, moved = t, [t], j, True
                            break
                    break
            if not moved:
                return None
        return None


def element_path(elem_id):
    """Device.identifier:euDatabaseId.type -> Device.identifier.type ; value[x]:valueX -> value[x]"""
    return ".".join(seg.split(":")[0] for seg in elem_id.split("."))


# --------------------------------------------------------------------------------------
# FSH source index: which file/line defines an artifact or an element rule
# --------------------------------------------------------------------------------------

FSH_KEYWORDS = ("Profile", "Extension", "Instance", "CodeSystem", "ValueSet", "Invariant",
                "Alias", "Logical", "Resource", "RuleSet", "Mapping")


class FshIndex:
    def __init__(self, repo):
        self.blocks = []
        for f in sorted((repo / "input" / "fsh").glob("*.fsh")):
            lines = f.read_text(encoding="utf-8").splitlines()
            cur = None
            for n, line in enumerate(lines, 1):
                m = re.match(r"^(%s):\s*(\S+)" % "|".join(FSH_KEYWORDS), line)
                if m:
                    cur = {"kind": m.group(1), "name": m.group(2), "id": m.group(2),
                           "file": f.relative_to(repo).as_posix(), "start": n, "lines": []}
                    self.blocks.append(cur)
                    continue
                if cur is not None:
                    mi = re.match(r"^Id:\s*(\S+)", line)
                    if mi:
                        cur["id"] = mi.group(1)
                    cur["lines"].append((n, line))

    def find(self, rid, name=None):
        for b in self.blocks:
            if b["kind"] in ("Profile", "Extension", "CodeSystem", "ValueSet", "Instance", "Logical") and (
                    b["id"] == rid or (name and b["name"] == name)):
                return b
        return None

    def element_line(self, block, elem_id):
        if not block:
            return None
        segs = elem_id.split(".")[1:]
        if not segs:
            return block["start"]
        fsh = []
        for s in segs:
            if ":" in s:
                base, sl = s.split(":", 1)
                fsh.append(sl if base == "value[x]" else f"{base}[{sl}]")
            else:
                fsh.append(s)
        path = ".".join(fsh)
        alts = {path, path.replace("[x]", "")}
        for a in list(alts):
            if "occurred." in a or "occurred[x]." in a:
                alts.add(re.sub(r"occurred(\[x\])?\.", "occurredPeriod.", a))
            if "value[x]." in a or "value." in a:
                alts.add(re.sub(r"value(\[x\])?\.", "valueSignature.", a))
        for n, line in block["lines"]:
            for a in alts:
                if re.match(r"^\s*\*\s+" + re.escape(a) + r"(\s|\^|$)", line):
                    return n
        last = segs[-1]
        if ":" in last:  # slice declared via "contains"
            sl = last.split(":", 1)[1]
            for n, line in block["lines"]:
                if re.search(r"(^|\s)" + re.escape(sl) + r"(\s|$)", line) and ("contains" in line or re.match(r"^\s+\S", line)):
                    return n
        return None


# --------------------------------------------------------------------------------------
# minimal Markdown renderer (kramdown/Jekyll flavoured, as used by the IG publisher)
# --------------------------------------------------------------------------------------

INLINE_HTML = re.compile(r"</?(?:a|br|b|i|em|strong|span|sup|sub|u|code|div|img|figure|figcaption|p)\b[^>]*>", re.I)
HTML_BLOCK = re.compile(r"^\s*</?(div|figure|figcaption|table|img|details|br|p)\b", re.I)


class LinkContext:
    """Rewrites IG-local links (StructureDefinition-x.html, page.html#anchor) to in-viewer refs."""

    def __init__(self, anchors, html_map, images):
        self.anchors, self.html_map, self.images = anchors, html_map, images

    def resolve(self, url):
        if re.match(r"^[a-z]+:", url) or url.startswith("//"):
            return None
        base, _, frag = url.partition("#")
        ref = self.html_map.get(base)
        if not ref:
            return None
        if frag:
            for cand in (f"{ref}#{frag}", f"{ref}/{frag}", f"{ref}#{slugify(frag)}"):
                if cand in self.anchors:
                    return cand
        return ref


def render_inline(text, ctx=None):
    keep = []

    def stash(s):
        keep.append(s)
        return f"\x00{len(keep) - 1}\x00"

    text = re.sub(r"(`+)(.+?)\1", lambda m: stash("<code>" + esc(m.group(2).strip()) + "</code>"), text)

    def img(m):
        alt, src = m.group(1), m.group(2).split()[0]
        if ctx and src in ctx.images:
            return stash(f'<img class="md-img" alt="{esc(alt)}" src="{ctx.images[src]}">')
        return stash(f'<span class="missing-img">[image: {esc(alt or src)}]</span>')
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", img, text)

    def raw(m):
        tag = m.group(0)
        if ctx and tag.lower().startswith("<img"):
            ms = re.search(r'src="([^"]+)"', tag)
            if ms and ms.group(1) in ctx.images:
                tag = tag.replace(ms.group(1), ctx.images[ms.group(1)])
                tag = re.sub(r"<img", '<img class="md-img"', tag, count=1)
        if ctx and tag.lower().startswith("<a ") and "href=" in tag:
            mh = re.search(r'href="([^"]+)"', tag)
            if mh:
                r = ctx.resolve(mh.group(1))
                if r:
                    tag = tag.replace(f'href="{mh.group(1)}"', f'href="#" data-goto="{esc(r)}"')
        return stash(tag)
    text = INLINE_HTML.sub(raw, text)
    text = esc(text, quote=False)

    def link(m):
        label, url = m.group(1), html.unescape(m.group(2).strip().split(" ")[0])
        r = ctx.resolve(url) if ctx else None
        if r:
            return f'<a href="#" data-goto="{esc(r)}" title="{esc(url)}">{label}</a>'
        if re.match(r"^[a-z]+:", url):
            return f'<a href="{esc(url)}" target="_blank" rel="noopener">{label}</a>'
        return f'<span class="local-link" title="IG-local link, not rendered here: {esc(url)}">{label}</span>'
    text = re.sub(r"\[((?:[^\[\]]|\x00\d+\x00)*)\]\(([^)\s]+(?:\s+&quot;[^&]*&quot;)?)\)", link, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w])_(?!\s)(.+?)(?<!\s)_(?![\w])", r"<em>\1</em>", text)
    while "\x00" in text:
        text = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)
    return text


LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")


def is_block_start(l):
    return bool(l.startswith("```") or re.match(r"^#{1,6}\s", l) or LIST_RE.match(l)
                or l.startswith(">") or l.lstrip().startswith("|") or HTML_BLOCK.match(l))


def md_blocks(lines, ctx, page_ref, headings, used_slugs):
    """Return blocks: ("b", html) or ("h", html, level, ref)."""
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or re.match(r"^\{:.*\}\s*$", line.strip()):
            i += 1
            continue
        m = re.match(r"^```\s*(\S*)", line)
        if m:
            lang, body = m.group(1), []
            i += 1
            while i < n and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            cap = f'<div class="code-cap">{esc(lang)} source (diagram not rendered)</div>' if lang in ("mermaid", "plantuml") else ""
            out.append(("b", f'{cap}<pre class="code lang-{esc(lang)}"><code>{esc(chr(10).join(body))}</code></pre>'))
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            level, txt = len(m.group(1)), m.group(2)
            mid = re.search(r"\{#([\w-]+)\}\s*$", txt)
            if mid:
                txt = txt[:mid.start()].strip()
            slug = mid.group(1) if mid else slugify(txt)
            base, k = slug, 2
            while slug in used_slugs:
                slug, k = f"{base}-{k}", k + 1
            used_slugs.add(slug)
            ref = f"{page_ref}#{slug}"
            headings.append((level, txt, ref))
            hl = min(level + 1, 6)
            out.append(("h", f'<h{hl} class="md-h">{render_inline(txt, ctx)}</h{hl}>', level, ref))
            i += 1
            continue
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            out.append(("b", "<hr>"))
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(("b", md_table(rows, ctx, page_ref)))
            continue
        if line.startswith(">"):
            body = []
            while i < n and lines[i].startswith(">"):
                body.append(re.sub(r"^> ?", "", lines[i]))
                i += 1
            inner = md_blocks(body, ctx, page_ref, [], set())
            out.append(("b", "<blockquote>" + "".join(b[1] for b in inner) + "</blockquote>"))
            continue
        if LIST_RE.match(line):
            block = []
            while i < n:
                l = lines[i]
                if not l.strip():
                    if i + 1 < n and (LIST_RE.match(lines[i + 1]) or lines[i + 1].startswith("  ")):
                        block.append(l)
                        i += 1
                        continue
                    break
                if LIST_RE.match(l) or l.startswith(" ") or l.startswith("\t"):
                    block.append(l)
                    i += 1
                    continue
                if block and not is_block_start(l):  # lazy continuation line
                    block.append("  " + l)
                    i += 1
                    continue
                break
            out.append(("b", md_list(block, ctx, page_ref)))
            continue
        if HTML_BLOCK.match(line):
            body = []
            while i < n and lines[i].strip():
                body.append(lines[i])
                i += 1
            raw = re.sub(r"<script\b.*?</script>", "", "\n".join(body), flags=re.S | re.I)
            out.append(("b", render_inline(raw, ctx)))
            continue
        para = []
        while i < n and lines[i].strip() and not (para and is_block_start(lines[i])):
            if re.match(r"^\{:.*\}\s*$", lines[i].strip()):
                break
            para.append(lines[i].strip())
            i += 1
        out.append(("b", "<p>" + render_inline(" ".join(para), ctx) + "</p>"))
    return out


def split_row(row):
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells, cur, in_code, k = [], "", False, 0
    while k < len(row):
        c = row[k]
        if c == "\\" and k + 1 < len(row) and row[k + 1] == "|":
            cur += "|"
            k += 2
            continue
        if c == "`":
            in_code = not in_code
        if c == "|" and not in_code:
            cells.append(cur.strip())
            cur = ""
        else:
            cur += c
        k += 1
    cells.append(cur.strip())
    return cells


def md_table(rows, ctx, page_ref):
    head = split_row(rows[0])
    h = ['<div class="tbl-wrap"><table class="md"><thead><tr>']
    h += [f"<th>{render_inline(c, ctx)}</th>" for c in head]
    h.append("</tr></thead><tbody>")
    for r in rows[2:]:
        cells = split_row(r)
        anchor = next((ma.group(1) for c in cells for ma in [re.search(r'<a name="([^"]+)"', c)] if ma), None)
        # table rows that start with a Jira key (Intl changes page is a table since 04.11.2026) get an anchor
        mj = None if anchor or not cells else re.match(r"^\[?(FHIR-\d+)", cells[0].strip())
        if mj and f"{page_ref}#{mj.group(1).lower()}" not in ctx.anchors:
            anchor = mj.group(1).lower()
        attr = ""
        if anchor:
            ctx.anchors.add(f"{page_ref}#{anchor}")
            attr = f' class="region md-row" data-ref="{esc(page_ref)}#{esc(anchor)}"'
        h.append(f"<tr{attr}>" + "".join(f"<td>{render_inline(c, ctx)}</td>" for c in cells) + "</tr>")
    h.append("</tbody></table></div>")
    return "".join(h)


def md_list(block, ctx, page_ref):
    items, base_indent = [], None
    for l in block:
        m = LIST_RE.match(l)
        ind = len(l) - len(l.lstrip()) if l.strip() else 999
        if base_indent is None and m:
            base_indent = ind
        if m and ind <= base_indent:
            items.append([m.group(2)[0].isdigit(), [m.group(3)]])
        elif items:
            items[-1][1].append(l[base_indent + 2:] if l[:base_indent + 2].strip() == "" else l.strip())
    if not items:
        return ""
    tag = "ol" if items[0][0] else "ul"
    h = [f"<{tag}>"]
    for _, lines in items:
        first, rest = lines[0], lines[1:]
        while rest and not rest[-1].strip():
            rest.pop()
        inner = "".join(b[1] for b in md_blocks(rest, ctx, page_ref, [], set())) if rest else ""
        # list items that start with a Jira key (changes pages) get their own anchor
        mj = re.match(r"^\[?(FHIR-\d+)", first)
        attr = ""
        if mj and f"{page_ref}#{mj.group(1).lower()}" not in ctx.anchors:
            ref = f"{page_ref}#{mj.group(1).lower()}"
            ctx.anchors.add(ref)
            attr = f' class="region md-li" data-ref="{esc(ref)}"'
        h.append(f"<li{attr}>" + render_inline(first, ctx) + inner + "</li>")
    h.append(f"</{tag}>")
    return "".join(h)


def nest_sections(blocks):
    """Wrap each heading and its following content in <section class="region" data-ref=...>."""
    out, stack = [], []
    for b in blocks:
        if b[0] == "h":
            level, ref = b[2], b[3]
            while stack and stack[-1] >= level:
                out.append("</section>")
                stack.pop()
            out.append(f'<section class="region pg-sec" data-ref="{esc(ref)}">' + b[1])
            stack.append(level)
        else:
            out.append(b[1])
    out.extend("</section>" for _ in stack)
    return "".join(out)


def preprocess_md(text):
    text = re.sub(r"\{\{\s*site\.data\.fhir\.ds4p\s*\}\}", "http://hl7.org/fhir/uv/security-label-ds4p", text)
    text = re.sub(r"\{%\s*include\s*([^%]+?)\s*%\}",
                  lambda m: f'<div class="liquid">IG Publisher include <code>{esc(m.group(1))}</code> (generated by the publisher, not rendered here)</div>', text)
    text = re.sub(r"\{:\s*[^}]*\}", "", text)
    return text


def render_markdown(text, ctx, page_ref):
    headings = []
    blocks = md_blocks(preprocess_md(text).splitlines(), ctx, page_ref, headings, set())
    for h in headings:
        ctx.anchors.add(h[2])
    return nest_sections(blocks), headings


# --------------------------------------------------------------------------------------
# artifact rendering
# --------------------------------------------------------------------------------------

def fmt_value(v):
    if isinstance(v, dict):
        if "coding" in v:
            return " | ".join(fmt_value(c) for c in v["coding"]) + (f' "{v["text"]}"' if v.get("text") else "")
        if "code" in v or "system" in v:
            s = f'{short_url(v.get("system", ""))}#{v.get("code", "")}'
            return s + (f' "{v["display"]}"' if v.get("display") else "")
        return json.dumps(v, ensure_ascii=False)
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


class SideModel:
    def __init__(self, key, repo):
        self.key, self.repo = key, repo
        self.cfg = yaml.safe_load((repo / "sushi-config.yaml").read_text())
        self.fhir_version = str(self.cfg.get("fhirVersion"))
        self.core = Core(self.fhir_version)
        self.fsh = FshIndex(repo)
        self.git = git_info(repo)
        self.resources = []
        for f in sorted((repo / "fsh-generated" / "resources").glob("*.json")):
            r = json.loads(f.read_text())
            r["_file"] = f"fsh-generated/resources/{f.name}"
            self.resources.append(r)
        ex_dir = repo / "input" / "examples"
        if ex_dir.exists():
            for f in sorted(ex_dir.glob("*.json")):
                try:
                    r = json.loads(f.read_text())
                except ValueError:
                    continue
                r["_file"] = f"input/examples/{f.name}"
                self.resources.append(r)
        self.anchors, self.toc, self.url_index, self.html_map = set(), [], {}, {}
        self.images = self.load_images()
        for r in self.resources:
            rt, rid = r.get("resourceType"), r.get("id")
            if rt == "ImplementationGuide":
                continue
            ref = {"StructureDefinition": "sd:", "CodeSystem": "cs:", "ValueSet": "vs:"}.get(rt, f"ex:{rt}/") + str(rid)
            r["_ref"] = ref
            if r.get("url"):
                self.url_index[r["url"]] = (ref, r.get("title") or r.get("name") or rid)
            self.html_map[f"{rt}-{rid}.html"] = ref
            if rt == "StructureDefinition":
                self.html_map[f"{rt}-{rid}-definitions.html"] = ref
        for p in (repo / "input" / "pagecontent").glob("*.md"):
            self.html_map[p.stem + ".html"] = f"page:{p.stem}"

    def load_images(self):
        imgs, d = {}, self.repo / "input" / "images"
        if not d.exists():
            return imgs
        for f in sorted(d.iterdir()):
            mime = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".gif": "image/gif"}.get(f.suffix.lower())
            if not mime:
                continue
            data = f.read_bytes()
            if mime == "image/png" and len(data) > 200_000:
                try:
                    from PIL import Image  # optional: shrink large screenshots
                    im = Image.open(io.BytesIO(data))
                    im.thumbnail((1400, 1400))
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, "JPEG", quality=82)
                    data, mime = buf.getvalue(), "image/jpeg"
                except Exception:
                    pass
            imgs[f.name] = f"data:{mime};base64," + base64.b64encode(data).decode()
        return imgs

    def name_for(self, url):
        u = url.split("|")[0]
        return self.url_index.get(u, (None, short_url(u)))

    def link_url(self, url):
        ref, title = self.name_for(url)
        if ref:
            return f'<a href="#" data-goto="{esc(ref)}" title="{esc(url)}">{esc(title)}</a>'
        return f'<span title="{esc(url)}">{esc(short_url(url))}</span>'

    def reg(self, ref):
        self.anchors.add(ref)
        return esc(ref)

    def render_ig_meta(self):
        c = self.cfg
        pub = c.get("publisher")
        rows = [
            ("id", "Package id", c.get("id")),
            ("canonical", "Canonical", c.get("canonical")),
            ("fhirVersion", "FHIR version", c.get("fhirVersion")),
            ("version", "IG version", c.get("version")),
            ("status", "Status", c.get("status")),
            ("releaseLabel", "Release label", c.get("releaseLabel")),
            ("publisher", "Publisher", pub.get("name") if isinstance(pub, dict) else pub),
            ("jurisdiction", "Jurisdiction", c.get("jurisdiction") or "(not set)"),
            ("license", "License", c.get("license") or "(not set)"),
            ("dependencies", "Dependencies", ", ".join(
                f"{k} {v.get('version') if isinstance(v, dict) else v}" for k, v in (c.get("dependencies") or {}).items()) or "(none)"),
            ("extension", "IG extensions", ", ".join(
                f"{short_url(e.get('url'))}={next((v for k, v in e.items() if k.startswith('value')), '')}"
                for e in (c.get("extension") or [])) or "(none)"),
        ]
        g = self.git
        h = [f'<article class="region artifact igmeta" data-ref="{self.reg("ig")}"><h2 class="art-title">IG metadata <span class="kind">sushi-config.yaml</span></h2>',
             f'<div class="srcline">{esc(g["remote"] or "")} @ <code>{esc(g["commit"] or "?")}</code> ({esc(g["commit_date"] or "")}){" <b>(uncommitted changes)</b>" if g["dirty"] else ""}</div>',
             '<table class="kv">']
        for key, label, val in rows:
            h.append(f'<tr class="region" data-ref="{self.reg("ig:" + key)}"><th>{esc(label)}</th><td>{esc(str(val))}</td></tr>')
        h.append("</table></article>")
        self.toc.append(("IG", "IG metadata", "ig", []))
        return "".join(h)

    def page_order(self):
        pdir = self.repo / "input" / "pagecontent"
        order = []
        for fname, meta in (self.cfg.get("pages") or {}).items():
            if (pdir / fname).exists():
                order.append((pdir / fname, meta.get("title") if isinstance(meta, dict) else None))
        listed = {p.name for p, _ in order}
        for p in sorted(pdir.glob("*.md")):
            if p.name not in listed and not re.match(r"^StructureDefinition-.*-intro\.md$", p.name):
                order.append((p, None))
        return order

    def render_pages(self):
        out = []
        for p, title in self.page_order():
            ref = f"page:{p.stem}"
            self.anchors.add(ref)
            body, heads = render_markdown(p.read_text(encoding="utf-8"), LinkContext(self.anchors, self.html_map, self.images), ref)
            title = title or p.stem.replace("_", " ").title()
            out.append(f'<article class="region page" data-ref="{esc(ref)}"><h2 class="art-title">{esc(title)} '
                       f'<span class="kind">input/pagecontent/{esc(p.name)}</span></h2>{body}</article>')
            self.toc.append(("Pages", title, ref, [(t, r) for lvl, t, r in heads if lvl <= 3]))
        return "".join(out)

    def render_sd(self, sd):
        ref = sd["_ref"]
        self.anchors.add(ref)
        block = self.fsh.find(sd["id"], sd.get("name"))
        src = f'{block["file"]}:{block["start"]}' if block else sd["_file"]
        is_ext = sd.get("type") == "Extension"
        h = [f'<article class="region artifact sd" data-ref="{esc(ref)}">',
             f'<h2 class="art-title">{esc(sd.get("title") or sd.get("name"))} <span class="kind">{"Extension" if is_ext else "Profile on " + esc(sd.get("type", ""))}</span></h2>',
             '<table class="kv">',
             f'<tr><th>id / name</th><td><code>{esc(sd["id"])}</code> / <code>{esc(sd.get("name", ""))}</code></td></tr>',
             f'<tr><th>url</th><td><code>{esc(sd.get("url", ""))}</code></td></tr>',
             f'<tr><th>parent</th><td>{self.link_url(sd.get("baseDefinition", ""))}</td></tr>',
             f'<tr><th>FHIR</th><td>{esc(sd.get("fhirVersion", self.fhir_version))}</td></tr>',
             f'<tr><th>source</th><td><code>{esc(src)}</code></td></tr>']
        if is_ext:
            ctxs = ", ".join(f'{c.get("type")}: {c.get("expression")}' for c in sd.get("context", []))
            h.append(f'<tr><th>context</th><td>{esc(ctxs)}</td></tr>')
        h.append("</table>")
        if sd.get("description"):
            h.append(f'<div class="desc">{render_inline(sd["description"])}</div>')
        intro = self.repo / "input" / "pagecontent" / f"StructureDefinition-{sd['id']}-intro.md"
        if intro.exists():
            iref = f"page:{intro.stem}"
            self.anchors.add(iref)
            body, _ = render_markdown(intro.read_text(encoding="utf-8"), LinkContext(self.anchors, self.html_map, self.images), iref)
            h.append(f'<details class="region intro" data-ref="{esc(iref)}"><summary>Profile page text '
                     f'(<code>input/pagecontent/{esc(intro.name)}</code>)</summary>{body}</details>')
        h.append('<div class="tbl-wrap"><table class="els"><thead><tr><th>Element (differential)</th><th>Card.</th>'
                 '<th>Type</th><th>Binding / fixed / slicing</th><th title="Must Support">MS</th><th>Short / definition / requirements</th></tr></thead><tbody>')
        root = sd.get("type")
        for e in sd.get("differential", {}).get("element", []):
            eid = e["id"]
            if is_ext and eid == "Extension.url" or re.match(r"^Extension(\.extension:[^.]+)*\.url$", eid) and "fixedUri" in e and len(e) <= 4:
                self.anchors.add(f"{ref}/{eid}")
                continue
            eref = f"{ref}/{eid}"
            self.anchors.add(eref)
            base = self.core.element(element_path(eid)) if eid != root else None
            bcard = f'{base["min"]}..{base["max"]}' if base else None
            card = ""
            if "min" in e or "max" in e:
                card = f'{e.get("min", base["min"] if base else "?")}..{e.get("max", base["max"] if base else "?")}'
            card_html = esc(card)
            if card and bcard and card != bcard:
                card_html = f'<span class="card-chg">{esc(card)}</span><span class="basecard">base {esc(bcard)}</span>'
            elif not card and bcard:
                card_html = f'<span class="basecard">({esc(bcard)})</span>'
            types = []
            for t in e.get("type", []):
                s = esc(t["code"])
                tps = t.get("targetProfile", []) + t.get("profile", [])
                if tps:
                    s += "(" + " | ".join(self.link_url(u) for u in tps) + ")"
                types.append(s)
            bind = []
            if e.get("binding"):
                b = e["binding"]
                bind.append(f'<span class="strength s-{esc(b.get("strength", ""))}">{esc(b.get("strength", ""))}</span> {self.link_url(b.get("valueSet", ""))}')
            for k, v in e.items():
                if k.startswith("fixed") or k.startswith("pattern"):
                    op = "==" if k.startswith("fixed") else "="
                    bind.append(f'<span class="fixed" title="{esc(k)}">{op} {esc(fmt_value(v))}</span>')
            if e.get("slicing"):
                s = e["slicing"]
                disc = ", ".join(f'{d.get("type")}:{d.get("path")}' for d in s.get("discriminator", []))
                bind.append(f'<span class="slicing">sliced by {esc(disc)} ({esc(s.get("rules", ""))})</span>')
            texts = []
            if e.get("short"):
                texts.append(f'<div class="short">{esc(e["short"])}</div>')
            if e.get("definition") and e.get("definition") != e.get("short"):
                texts.append(f'<div class="def">{esc(e["definition"])}</div>')
            if e.get("requirements"):
                texts.append(f'<div class="req">Requirements: {esc(e["requirements"])}</div>')
            if e.get("comment"):
                texts.append(f'<div class="cmt">Comment: {esc(e["comment"])}</div>')
            for c in e.get("constraint", []):
                texts.append(f'<div class="inv">{esc(c.get("key", ""))} ({esc(c.get("severity", ""))}): {esc(c.get("human", ""))}<br><code>{esc(c.get("expression", ""))}</code></div>')
            line = self.fsh.element_line(block, eid) if block else None
            loc = f'<span class="loc">{esc(block["file"].split("/")[-1])}:{line}</span>' if line else ""
            depth = eid.count(".")
            label = eid.split(".")[-1] if depth else eid
            label_html = re.sub(r":([\w\-]+)$", r'<span class="slice">:\1</span>', esc(label))
            h.append(f'<tr class="region el" data-ref="{esc(eref)}"><td class="path" style="padding-left:{0.3 + 0.7 * depth:.1f}em" title="{esc(eid)}">'
                     f'{label_html}{loc}</td><td class="card">{card_html}</td><td>{"<br>".join(types)}</td>'
                     f'<td>{"<br>".join(bind)}</td><td class="ms">{"S" if e.get("mustSupport") else ""}</td><td>{"".join(texts)}</td></tr>')
        h.append("</tbody></table></div></article>")
        return "".join(h)

    def render_cs(self, cs):
        ref = cs["_ref"]
        self.anchors.add(ref)
        block = self.fsh.find(cs["id"], cs.get("name"))
        src = f'{block["file"]}:{block["start"]}' if block else cs["_file"]
        h = [f'<article class="region artifact cs" data-ref="{esc(ref)}"><h2 class="art-title">{esc(cs.get("title") or cs.get("name"))} <span class="kind">CodeSystem</span></h2>',
             f'<div class="srcline"><code>{esc(cs.get("url", ""))}</code> · <code>{esc(src)}</code></div>']
        if cs.get("description"):
            h.append(f'<div class="desc">{render_inline(cs["description"])}</div>')
        h.append('<div class="tbl-wrap"><table class="els codes"><thead><tr><th>Code</th><th>Display</th><th>Definition</th></tr></thead><tbody>')

        def walk(concepts, depth):
            for c in concepts:
                cref = f"{ref}/{c['code']}"
                self.anchors.add(cref)
                abstract = any(p.get("code") in ("abstract", "notSelectable") and p.get("valueBoolean") for p in c.get("property", []))
                h.append(f'<tr class="region el" data-ref="{esc(cref)}"><td class="path" style="padding-left:{0.3 + 0.9 * depth:.1f}em"><code>{esc(c["code"])}</code>{" <i>(abstract)</i>" if abstract else ""}</td>'
                         f'<td>{esc(c.get("display", ""))}</td><td>{esc(c.get("definition", ""))}</td></tr>')
                walk(c.get("concept", []), depth + 1)
        walk(cs.get("concept", []), 0)
        h.append("</tbody></table></div></article>")
        return "".join(h)

    def render_vs(self, vs):
        ref = vs["_ref"]
        self.anchors.add(ref)
        h = [f'<article class="region artifact vs" data-ref="{esc(ref)}"><h2 class="art-title">{esc(vs.get("title") or vs.get("name"))} <span class="kind">ValueSet</span></h2>',
             f'<div class="srcline"><code>{esc(vs.get("url", ""))}</code></div>']
        if vs.get("description"):
            h.append(f'<div class="desc">{render_inline(vs["description"])}</div>')
        h.append("<ul>")
        for kind in ("include", "exclude"):
            for inc in vs.get("compose", {}).get(kind, []):
                what = self.link_url(inc["system"]) if inc.get("system") else ""
                codes = ", ".join(c["code"] for c in inc.get("concept", []))
                vss = ", ".join(self.link_url(u) for u in inc.get("valueSet", []))
                h.append(f"<li><b>{kind}</b> {what} {('codes: ' + esc(codes)) if codes else ('(all codes)' if what else '')} {vss}</li>")
        h.append("</ul></article>")
        return "".join(h)

    def render_examples(self):
        exs = [r for r in self.resources if r.get("_ref", "").startswith("ex:")]
        h = [f'<article class="region artifact examples" data-ref="{self.reg("examples")}"><h2 class="art-title">Example instances <span class="kind">{len(exs)} resources</span></h2>',
             '<p class="note">Profile and <code>meta.security</code> are read from the SUSHI output; expand an entry for its JSON.</p>']
        for r in exs:
            ref = r["_ref"]
            self.anchors.add(ref)
            prof = ", ".join(self.name_for(u)[1] for u in r.get("meta", {}).get("profile", [])) or "(no meta.profile)"
            sec = ", ".join(fmt_value(s) for s in r.get("meta", {}).get("security", [])) or "none"
            js = json.dumps({k: v for k, v in r.items() if not k.startswith("_")}, indent=1, ensure_ascii=False)
            if len(js) > 12000:
                js = js[:12000] + "\n... (truncated)"
            h.append(f'<details class="region example" data-ref="{esc(ref)}"><summary><code>{esc(ref[3:])}</code> · profile: {esc(prof)} · '
                     f'meta.security: <span class="sec">{esc(sec)}</span></summary><div class="srcline"><code>{esc(r["_file"])}</code></div>'
                     f'<pre class="code"><code>{esc(js)}</code></pre></details>')
        h.append("</article>")
        self.toc.append(("Examples", f"Example instances ({len(exs)})", "examples", []))
        return "".join(h)

    def render(self):
        parts = [self.render_ig_meta(), '<h1 class="group">Pages</h1>', self.render_pages()]
        groups = [
            ("Profiles", lambda r: r.get("resourceType") == "StructureDefinition" and r.get("type") != "Extension", self.render_sd),
            ("Extensions", lambda r: r.get("resourceType") == "StructureDefinition" and r.get("type") == "Extension", self.render_sd),
            ("CodeSystems", lambda r: r.get("resourceType") == "CodeSystem", self.render_cs),
            ("ValueSets", lambda r: r.get("resourceType") == "ValueSet", self.render_vs),
        ]
        for gname, pred, fn in groups:
            items = [r for r in self.resources if pred(r)]
            if not items:
                continue
            parts.append(f'<h1 class="group">{gname}</h1>')
            for r in items:
                parts.append(fn(r))
                self.toc.append((gname, r.get("title") or r.get("name") or r["id"], r["_ref"], []))
        parts.append('<h1 class="group">Examples</h1>')
        parts.append(self.render_examples())
        return "".join(parts)

    def toc_html(self):
        h, last = [], None
        for group, label, ref, kids in self.toc:
            if group != last:
                if last is not None:
                    h.append("</ul>")
                h.append(f'<div class="toc-group">{esc(group)}</div><ul>')
                last = group
            sub = ""
            if kids:
                sub = "<ul>" + "".join(f'<li><a href="#" data-goto="{esc(r)}" data-tocref="{esc(r)}">{render_inline(t)}</a></li>' for t, r in kids) + "</ul>"
            h.append(f'<li><a href="#" data-goto="{esc(ref)}" data-tocref="{esc(ref)}">{esc(label)}</a>{sub}</li>')
        h.append("</ul>")
        return "".join(h)


# --------------------------------------------------------------------------------------
# diffs
# --------------------------------------------------------------------------------------

RECON = ["unreconcilable", "reconcilable-with-change", "at-only-derivable", "equivalent"]
KINDS = ["fhir-version", "scope/use-case", "profile-modelling", "base-resource", "element/cardinality",
         "terminology/binding", "extension", "legal-requirement coverage", "narrative/guidance", "naming/canonical"]
REQUIRED = ["id", "title", "explanation", "left_refs", "right_refs", "kind", "reconcilability",
            "talking_point", "suggested_level", "severity", "presence"]


def md_paras(text):
    return "".join(f"<p>{render_inline(p)}</p>" for p in text.split("\n\n"))


def load_diffs(models, issues_repo=None):
    if issues_repo:
        sys.path.insert(0, str(HERE))
        import issues
        data = json.loads((HERE / "comparison.json").read_text())
        data["items"], problems = issues.fetch_items(issues_repo)
        data["repo"] = issues_repo
        print(f"[issues] {issues_repo}: {len(data['items'])} items, "
              f"{sum(len(i['issue']['comments']) for i in data['items'])} comments")
    else:
        data = json.loads((HERE / "diffs.json").read_text())
        problems = []
    seen = set()
    for it in data["items"]:
        for f in REQUIRED:
            if f not in it:
                problems.append(f'{it.get("id")}: missing field "{f}"')
        if it.get("id") in seen:
            problems.append(f'{it["id"]}: duplicate id')
        seen.add(it.get("id"))
        if it.get("reconcilability") not in RECON:
            problems.append(f'{it["id"]}: bad reconcilability {it.get("reconcilability")}')
        if it.get("kind") not in KINDS:
            problems.append(f'{it["id"]}: bad kind {it.get("kind")}')
        scen = data.get("summary", {}).get("eu_scenarios", {})
        for sc in it.get("eu_scenarios", []):
            if str(sc) not in scen:
                problems.append(f'{it["id"]}: unknown EU scenario {sc}')
        for side, key in (("at", "left_refs"), ("intl", "right_refs")):
            for r in it.get(key, []):
                if r not in models[side].anchors:
                    problems.append(f'{it["id"]}: {key} "{r}" does not resolve in the {SIDES[side]["short"]} render')
        for f in ("explanation", "change", "uncertainty"):
            if it.get(f):
                it[f + "_html"] = md_paras(it[f])
        it["talking_point_html"] = render_inline(it.get("talking_point", ""))
    s = data.get("summary", {})
    for k in s.get("key_modelling", []):
        k["html"] = render_inline(k["text"])
    if s.get("version_assessment"):
        s["version_assessment_html"] = md_paras(s["version_assessment"])
    return data, problems


# --------------------------------------------------------------------------------------
# page assembly
# --------------------------------------------------------------------------------------

def build(args):
    repos = {"at": Path(args.at).resolve(), "intl": Path(args.intl).resolve()}
    sushi_ver = None
    if not args.no_sushi:
        if not shutil.which("sushi"):
            sys.exit("sushi not found on PATH (use --no-sushi to reuse fsh-generated/)")
        for repo in repos.values():
            print(f"[sushi] {repo} ...", flush=True)
            r = run(["sushi", "."], repo)
            if r.returncode != 0:
                print(r.stdout[-3000:], r.stderr[-3000:])
                sys.exit(f"SUSHI failed in {repo}")
            m = re.search(r"(\d+) Errors\s+(\d+) Warnings", r.stdout)
            print(f"[sushi]   {m.group(0) if m else 'done'}")
        rv = run(["sushi", "--version"], HERE)
        sushi_ver = ((rv.stdout or rv.stderr).strip().splitlines() or [None])[-1]

    models = {k: SideModel(k, repos[k]) for k in SIDES}
    side_html = {k: m.render() for k, m in models.items()}
    diffs, problems = load_diffs(models, args.issues)
    out = Path(args.out).resolve() if args.out else HERE
    out.mkdir(parents=True, exist_ok=True)

    stale = []
    for k, m in models.items():
        want = (diffs.get("analysed_against") or {}).get(k)
        if want and m.git["commit"] and not m.git["commit"].startswith(want):
            stale.append(f'{SIDES[k]["short"]}: diffs.json was curated against {want[:9]}, this build compares {m.git["commit"][:9]}; re-review the items')
        if m.git["dirty"]:
            stale.append(f'{SIDES[k]["short"]}: working tree has uncommitted changes')

    compared = {
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sushi": sushi_ver or "not run (--no-sushi)",
        "sides": {k: {**m.git, "label": SIDES[k]["label"], "ig_id": m.cfg.get("id"), "ig_version": m.cfg.get("version"),
                      "fhirVersion": m.fhir_version, "anchors": len(m.anchors)} for k, m in models.items()},
        "analysed_against": diffs.get("analysed_against"),
        "items": len(diffs["items"]),
        "problems": problems,
        "staleness": stale,
    }
    (out / "compared.json").write_text(json.dumps(compared, indent=2) + "\n")

    payload = {"items": diffs["items"], "summary": diffs.get("summary", {}), "compared": compared,
               "repo": diffs.get("repo"),
               "kinds": KINDS, "recon": RECON}
    page = (HERE / "template.html").read_text(encoding="utf-8")
    for k, m in models.items():
        g = m.git
        page = page.replace(f"%%{k.upper()}_HEAD%%",
                            f'{esc(SIDES[k]["label"])} · <code>{esc(m.cfg.get("id", ""))}</code> {esc(str(m.cfg.get("version", "")))} · '
                            f'<b>FHIR {esc(m.fhir_version)}</b> · <code title="{esc(g["commit"] or "")} {esc(g["commit_date"] or "")}">{esc(g["commit_short"] or "?")}</code>')
        page = page.replace(f"%%{k.upper()}_TOC%%", m.toc_html())
        page = page.replace(f"%%{k.upper()}_BODY%%", side_html[k])
    page = page.replace("%%DATA%%", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    (out / "index.html").write_text(page, encoding="utf-8")

    print(f"[ok] index.html ({len(page) / 1e6:.1f} MB), {len(diffs['items'])} diff items, "
          f"anchors AT={len(models['at'].anchors)} Intl={len(models['intl'].anchors)}")
    for k, m in models.items():
        print(f"[git] {SIDES[k]['short']}: {m.git['commit']} {m.git['commit_date']}{' (dirty)' if m.git['dirty'] else ''}")
    for s in stale:
        print("[stale]", s)
    for p in problems:
        print("[ref-problem]", p)
    ok = not problems
    if args.selftest:
        ok = selftest(out) and ok
    return 0 if ok else 1


def selftest(out=HERE):
    chrome = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)
    if not chrome:
        print("[selftest] no Chrome/Chromium found, skipped")
        return True
    url = (out / "index.html").as_uri() + "#selftest"
    r = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=30000",
                        "--window-size=1800,1100", "--dump-dom", url], capture_output=True, text=True, timeout=240)
    m = re.search(r'<pre id="selftest-result"[^>]*>(.*?)</pre>', r.stdout, re.S)
    if not m:
        print("[selftest] no result found in the DOM", r.stderr[-1500:])
        return False
    res = json.loads(html.unescape(m.group(1)))
    print("[selftest]", json.dumps(res, indent=1)[:6000])
    return res.get("ok", False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", default=str(SIDES["at"]["default_repo"]), help="path to the AT IG repo")
    ap.add_argument("--intl", default=str(SIDES["intl"]["default_repo"]), help="path to the Intl IG repo")
    ap.add_argument("--no-sushi", action="store_true", help="reuse existing fsh-generated/ instead of running SUSHI")
    ap.add_argument("--issues", metavar="OWNER/REPO", help="read the items and their discussions from the GitHub issues of this repo")
    ap.add_argument("--out", help="output directory for index.html and compared.json (default: next to this script)")
    ap.add_argument("--selftest", action="store_true", help="check the generated page in headless Chrome")
    sys.exit(build(ap.parse_args()))
