# HL7 Austria vs HL7 International: AI Transparency IG comparison

**Page:** https://hl7austria.github.io/AT-INTL-AI-Transparency-IG-Comparison/

Side-by-side comparison of the two AI Transparency FHIR IGs:

| | IG | FHIR |
|---|---|---|
| left | [HL7Austria/AIST-TrustworthyAI-R5](https://github.com/HL7Austria/AIST-TrustworthyAI-R5) | R5 |
| right | [HL7/aitransparency-ig](https://github.com/HL7/aitransparency-ig) | R4 |

The compared commits are pinned in [`compare.yaml`](compare.yaml).

## Every difference is an issue

Each of the differences in the middle column of the page is an
[issue labelled `comparison-item`](../../issues?q=label%3Acomparison-item). The page renders the issue text
and its comments, and is rebuilt automatically (GitHub Action, ~2–3 min) whenever an issue or comment changes.

- **Discuss** an item: comment on its issue (the page links to it from every item).
- **Edit** the texts: edit the issue. Keep the headings (`### What differs`, `### What should change`,
  `### Uncertain`, `### Evidence`) and the `> **Talking point:**` line; the hidden `comparison-item`
  block at the end holds the anchors into the IGs. Change it only to re-point an item.
- **Reclassify**: change the labels (`unreconcilable`, `reconcilable-with-change`, `at-only-derivable`,
  `equivalent`, `severity:*`). Labels win over the hidden block.
- **Resolved** items: close the issue; the page keeps showing them, marked *closed*.

## Build locally

```bash
python3 build.py --issues HL7Austria/AT-INTL-AI-Transparency-IG-Comparison --out site   # needs sushi + PyYAML
python3 build.py --no-sushi --issues HL7Austria/AT-INTL-AI-Transparency-IG-Comparison --out site --selftest
```

The IGs are read from `igs/at` and `igs/intl` (as checked out by CI) or from sibling folders
`../AIST-TrustworthyAI-R5` and `../aitransparency-ig`. GitHub access uses `GITHUB_TOKEN` or `gh auth token`.

| File | Purpose |
|---|---|
| `build.py` | renders both IGs from their FSH/markdown sources and the items into one self-contained `index.html` |
| `template.html` | page shell (CSS + JS) |
| `issues.py` | item ⇄ issue format, GitHub API |
| `comparison.json` | summary, EU scenarios and the commits the items were reviewed against (no items) |
| `compare.yaml` | IG commits rendered by CI |
| `import_issues.py` | one-time import of the items from the earlier `diffs.json` |
