"""Comparison items <-> GitHub issues.

Every difference between the two IGs is one GitHub issue labelled `comparison-item`.
The issue is the source of truth: title = item title, labels = classification, the
markdown sections of the body = the texts, and a hidden HTML comment at the end of the
body carries the machine data (id, anchors/refs, kind, EU scenarios, ...).

Labels win over the hidden block, so a working group can reclassify an item just by
changing its labels on GitHub (e.g. `reconcilable-with-change` -> `unreconcilable`).
"""
import json
import os
import re
import subprocess
import urllib.request

ITEM_LABEL = "comparison-item"
RECON = ["unreconcilable", "reconcilable-with-change", "at-only-derivable", "equivalent"]
SEVERITY = ["high", "medium", "low"]
LABELS = {  # name: (colour, description)
    ITEM_LABEL: ("5319e7", "One difference between the AT and Intl IG (rendered on the comparison page)"),
    "unreconcilable": ("c62828", "Cannot be expressed as AT deriving from (EU deriving from) Intl"),
    "reconcilable-with-change": ("ef8f00", "Works once one side changes"),
    "at-only-derivable": ("1e6fd9", "AT addition that can sit on top of Intl/EU"),
    "equivalent": ("2e7d32", "Same concept and compatible"),
    "severity:high": ("b60205", ""),
    "severity:medium": ("fbca04", ""),
    "severity:low": ("c5def5", ""),
    "eu-verdict": ("0052cc", "Where an AT IG part belongs under the chain Intl -> EU -> AT (EU placement tab of the page)"),
    "placement:eu": ("1f6feb", "Move into the HL7 Europe IG"),
    "placement:eu-intl": ("8250df", "Belongs in HL7 International; EU carries it until Intl has it"),
    "placement:at": ("b60205", "Stays in the Austrian IG"),
    "placement:drop": ("6e7781", "Drop or replace with existing Intl/core content"),
    "placement:open": ("fbca04", "Open question for the working group"),
    "confidence:high": ("0e8a16", ""),
    "confidence:medium": ("fbca04", ""),
    "confidence:low": ("d93f0b", ""),
}
VERDICT_LABEL = "eu-verdict"
PLACEMENTS = ["eu", "eu-intl", "at", "drop", "open"]
CONFIDENCE = ["high", "medium", "low"]
V_SECTIONS = [("rationale", "Rationale"), ("changes", "What should change"), ("guess", "Unverified reading")]
V_META_KEYS = ["id", "at_refs", "diffs", "confluence", "scenarios", "articles", "eu_target"]
V_META_RE = re.compile(r"<!--\s*eu-verdict\b[^\n]*\n(.*?)\n\s*-->", re.S)

SECTIONS = [("explanation", "What differs"), ("change", "What should change"),
            ("uncertainty", "Uncertain"), ("evidence", "Evidence")]
META_KEYS = ["id", "kind", "presence", "suggested_level", "eu_scenarios", "left_refs", "right_refs", "uncertain"]
META_RE = re.compile(r"<!--\s*comparison-item\b[^\n]*\n(.*?)\n\s*-->", re.S)
TP_PREFIX = "> **Talking point:** "


# ---------------------------------------------------------------- item -> issue

def issue_body(item):
    out = [TP_PREFIX + item.get("talking_point", "").strip().replace("\n", "\n> "), ""]
    for key, head in SECTIONS:
        val = item.get(key)
        if not val:
            continue
        out.append(f"### {head}")
        out.append("\n".join(f"- {e}" for e in val) if key == "evidence" else val.strip())
        out.append("")
    meta = {k: item[k] for k in META_KEYS if k in item}
    out.append("<!-- comparison-item: data for the comparison page (anchors into the IGs). "
               "Edit the text above freely; change this block only to re-point the item.")
    out.append(json.dumps(meta, ensure_ascii=False, indent=1))
    out.append("-->")
    return "\n".join(out) + "\n"


def issue_labels(item):
    return [ITEM_LABEL, item["reconcilability"], f"severity:{item['severity']}"]


# ---------------------------------------------------------------- issue -> item

def parse_issue(issue):
    body = (issue.get("body") or "").replace("\r\n", "\n")
    m = META_RE.search(body)
    if not m:
        raise ValueError(f"issue #{issue['number']}: no comparison-item data block")
    item = json.loads(m.group(1))
    text = body[:m.start()]

    tp = re.search(r"^> \*\*Talking point:\*\*[ \t]*(.*?)(?=\n(?!>)|\Z)", text, re.S | re.M)
    item["talking_point"] = re.sub(r"\n>\s?", " ", tp.group(1)).strip() if tp else ""

    parts = re.split(r"^###[ \t]+(.+?)[ \t]*$", text, flags=re.M)
    by_head = {h.strip().lower(): v.strip() for h, v in zip(parts[1::2], parts[2::2])}
    for key, head in SECTIONS:
        val = by_head.get(head.lower())
        if not val:
            continue
        item[key] = [re.sub(r"^[-*]\s+", "", l).strip() for l in val.splitlines() if l.strip()] if key == "evidence" else val

    labels = [l["name"] for l in issue.get("labels", [])]
    rec = [l for l in labels if l in RECON]
    sev = [l.split(":", 1)[1] for l in labels if l.startswith("severity:")]
    item["reconcilability"] = rec[0] if rec else item.get("reconcilability", "")
    item["severity"] = sev[0] if sev else item.get("severity", "medium")
    item["title"] = issue["title"]
    item.setdefault("evidence", [])
    item["issue"] = {"number": issue["number"], "url": issue["html_url"], "state": issue["state"],
                     "comments": []}
    return item


# ---------------------------------------------------------------- EU verdicts

def verdict_body(v):
    out = []
    for key, head in V_SECTIONS:
        val = v.get(key)
        if not val:
            continue
        out.append(f"### {head}")
        out.append("\n".join(f"- {c}" for c in val) if key == "changes" else val.strip())
        out.append("")
    meta = {k: v[k] for k in V_META_KEYS if k in v}
    out.append("<!-- eu-verdict: data for the comparison page (AT IG anchors, related differences). "
               "Edit the text above freely; change this block only to re-point the verdict.")
    out.append(json.dumps(meta, ensure_ascii=False, indent=1))
    out.append("-->")
    return "\n".join(out) + "\n"


def verdict_labels(v):
    return [VERDICT_LABEL, f"placement:{v['verdict']}", f"confidence:{v['confidence']}"]


def verdict_title(v):
    return f"[{v['id']}] {v['title']}"


def parse_verdict(issue):
    body = (issue.get("body") or "").replace("\r\n", "\n")
    m = V_META_RE.search(body)
    if not m:
        raise ValueError(f"issue #{issue['number']}: no eu-verdict data block")
    v = json.loads(m.group(1))
    parts = re.split(r"^###[ \t]+(.+?)[ \t]*$", body[:m.start()], flags=re.M)
    by_head = {h.strip().lower(): t.strip() for h, t in zip(parts[1::2], parts[2::2])}
    for key, head in V_SECTIONS:
        val = by_head.get(head.lower())
        if val:
            v[key] = [re.sub(r"^[-*]\s+", "", l).strip() for l in val.splitlines() if l.strip()] if key == "changes" else val
    v.setdefault("changes", [])
    labels = [l["name"] for l in issue.get("labels", [])]
    pl = [l.split(":", 1)[1] for l in labels if l.startswith("placement:")]
    cf = [l.split(":", 1)[1] for l in labels if l.startswith("confidence:")]
    v["verdict"] = pl[0] if pl else "open"
    v["confidence"] = cf[0] if cf else "medium"
    v["title"] = re.sub(r"^\[V\d+\]\s*", "", issue["title"])
    v["issue"] = {"number": issue["number"], "url": issue["html_url"], "state": issue["state"], "comments": []}
    return v


# ---------------------------------------------------------------- GitHub API

def token():
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not t:
        try:
            t = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
        except FileNotFoundError:
            t = ""
    return t


def api(path, accept="application/vnd.github+json", method="GET", data=None):
    """GET with pagination (returns the concatenated list), or a single non-GET call."""
    url = path if path.startswith("http") else "https://api.github.com" + path
    out = []
    while url:
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(data).encode() if data is not None else None)
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        t = token()
        if t:
            req.add_header("Authorization", f"Bearer {t}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as r:
            payload = json.loads(r.read() or b"null")
            if method != "GET":
                return payload
            out.extend(payload if isinstance(payload, list) else [payload])
            nxt = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
            url = nxt.group(1) if nxt else None
    return out


def fetch_items(repo):
    """All comparison issues of `repo` (open and closed) with their comment threads, as items."""
    items, verdicts, problems = fetch_all(repo)
    return items, problems


def fetch_all(repo):
    """(diff items, EU verdicts, problems) from the issues of `repo`, each with its comment thread."""
    issues = api(f"/repos/{repo}/issues?state=all&per_page=100", accept="application/vnd.github.full+json")
    items, verdicts, problems = {}, {}, []
    for iss in issues:
        if "pull_request" in iss:
            continue
        labels = {l["name"] for l in iss.get("labels", [])}
        try:
            if ITEM_LABEL in labels:
                items[iss["number"]] = parse_issue(iss)
            elif VERDICT_LABEL in labels:
                verdicts[iss["number"]] = parse_verdict(iss)
        except (ValueError, json.JSONDecodeError) as e:
            problems.append(str(e))
    comments = api(f"/repos/{repo}/issues/comments?per_page=100&sort=created&direction=asc",
                   accept="application/vnd.github.full+json")
    for c in comments:
        n = int(c["issue_url"].rsplit("/", 1)[1])
        target = items.get(n) or verdicts.get(n)
        if target:
            target["issue"]["comments"].append({
                "author": c["user"]["login"], "avatar": c["user"]["avatar_url"] + "&s=48",
                "url": c["html_url"], "created": c["created_at"], "updated": c["updated_at"],
                "body_html": c.get("body_html", ""), "body": c.get("body", ""),
            })
    return [items[n] for n in sorted(items)], [verdicts[n] for n in sorted(verdicts)], problems
