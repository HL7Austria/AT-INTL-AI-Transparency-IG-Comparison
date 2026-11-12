#!/usr/bin/env python3
"""One-time import: create one GitHub issue per comparison item from a diffs.json.

    python3 import_issues.py ../ai-transparency-reports/ig-comparison/diffs.json            # dry run
    python3 import_issues.py <diffs.json> --execute --only fhir-version                      # one item
    python3 import_issues.py <diffs.json> --execute                                          # the rest

Idempotent: items whose id already exists as a comparison issue are skipped. Labels are
created/updated first. Uses GITHUB_TOKEN / GH_TOKEN or `gh auth token`.
"""
import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import issues as I  # noqa: E402

REPO = "HL7Austria/AT-INTL-AI-Transparency-IG-Comparison"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("diffs")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--execute", action="store_true", help="actually create labels and issues (default: dry run)")
    ap.add_argument("--only", action="append", help="item id(s) to import")
    a = ap.parse_args()

    items = json.loads(Path(a.diffs).read_text())["items"]
    if a.only:
        items = [i for i in items if i["id"] in a.only]
    existing, _ = I.fetch_items(a.repo)
    have = {i["id"]: i["issue"]["number"] for i in existing}
    todo = [i for i in items if i["id"] not in have]
    print(f"{len(items)} items selected, {len(items) - len(todo)} already imported, {len(todo)} to create in {a.repo}")

    if a.execute:
        current = {l["name"] for l in I.api(f"/repos/{a.repo}/labels?per_page=100")}
        for name, (color, desc) in I.LABELS.items():
            if name not in current:
                I.api(f"/repos/{a.repo}/labels", method="POST", data={"name": name, "color": color, "description": desc})
                print(f"  label + {name}")
    for it in todo:
        labels = I.issue_labels(it)
        print(f"  {'create' if a.execute else 'would create'}: [{it['id']}] {it['title'][:70]}  {labels}")
        if not a.execute:
            continue
        for attempt in range(4):
            try:
                r = I.api(f"/repos/{a.repo}/issues", method="POST",
                          data={"title": it["title"], "body": I.issue_body(it), "labels": labels})
                print(f"    -> #{r['number']} {r['html_url']}")
                break
            except urllib.error.HTTPError as e:  # secondary rate limit on bursts of issue creation
                if e.code in (403, 429) and attempt < 3:
                    time.sleep(30 * (attempt + 1))
                    continue
                raise
        time.sleep(2)
    if not a.execute:
        print("dry run: nothing was created (add --execute)")


if __name__ == "__main__":
    main()
