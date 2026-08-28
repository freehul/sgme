# -*- coding: utf-8 -*-
"""Live self-retrieval recall eval against running NAS SGME.

For each of the 403 canonical skills (parsed from the coldstart report), we
use the skill's own description as the query and check whether the skill's
own name surfaces in the top-k of scopes=["skills"] results. This measures
whether an agent phrasing a need can find the right skill.

Pure-ASCII source (skill names are ASCII; report read with utf-8).
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

REPORT = Path(r"D:\Projects\SGME\tmp\sgme-coldstart-skills-20260828.md")
BASE = "http://192.168.10.10:9910"
ENV = Path(r"D:\Projects\SGME\.env")

LINE_RE = re.compile(r"^- \*\*(.+?)\*\*.+? - (.+?) {2}\(tags:", re.S | re.M)


def load_env() -> str:
    key = "dev-agent-key-change-me"
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SGME_AGENT_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


def parse_report():
    skills = []
    if not REPORT.exists():
        return skills
    text = REPORT.read_text(encoding="utf-8")
    for m in LINE_RE.finditer(text):
        name, desc = m.group(1), m.group(2)
        skills.append((name, desc.strip()))
    return skills


def search(query, key, limit=10, timeout=15):
    body = json.dumps({"query": query, "scopes": ["skills"], "limit": limit}).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/v1/search", data=body,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    key = load_env()
    skills = parse_report()
    if not skills:
        print("ERR: no skills parsed from report", file=sys.stderr)
        return 2
    print(f"parsed {len(skills)} skills from report")

    tops = [1, 3, 5, 10]
    hit = {k: 0 for k in tops}
    misses = []
    total = len(skills)

    for i, (name, desc) in enumerate(skills, 1):
        # query from description: drop markdown/long tails, keep first sentence-ish
        q = desc.split("\u3002")[0].split(".")[0][:80].strip()
        if not q:
            q = name
        try:
            res = search(q, key)
            names = [x.get("name") for x in res.get("results", [])]
        except Exception as e:
            print(f"  query err [{name}]: {e}", file=sys.stderr)
            names = []
        for k in tops:
            if name in names[:k]:
                hit[k] += 1
        if name not in names[: max(tops)]:
            misses.append((name, q, names[:5]))
        if i % 50 == 0:
            print(f"  progress {i}/{total}")

    print("\n=== Self-retrieval recall (live NAS, scopes=skills) ===")
    print(f"{'top-k':>6} {'hit':>5} {'/':>1} {'total':>5} {'rate':>8}")
    for k in tops:
        print(f"{k:>6} {hit[k]:>5} {total:>5} {hit[k]/total*100:>7.1f}%")

    print(f"\nMISSES (self not in top-10): {len(misses)}")
    for name, q, top5 in misses[:60]:
        t5 = ",".join(str(n) for n in top5)
        print(f"  X {name} | q='{q[:50]}' | top5={t5}")

    # dump misses for later fixing
    out = Path(r"D:\Projects\SGME\tmp\skills_recall_misses.json")
    out.write_text(json.dumps(
        [{"name": n, "query": q, "top5": t} for n, q, t in misses],
        ensure_ascii=False, indent=2,
    ), encoding="utf-8")
    print(f"\nmisses written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
