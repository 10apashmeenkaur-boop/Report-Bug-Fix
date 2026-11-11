"""Score out/results.json against our hand-solved labels (eval/expected.json).

    python eval/eval.py [path/to/results.json]
"""
import json
import sys
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
res = {r["reportId"]: r for r in json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "results.json").read_text(encoding="utf-8"))}
exp = json.loads((ROOT / "eval/expected.json").read_text(encoding="utf-8"))["labels"]

score = Counter()
rows = []
for rid, e in exp.items():
    r = res.get(rid)
    if not r:
        rows.append((rid, "MISSING", "", "")); continue
    v = r["verdict"] == e["verdict"]
    rp = r["repair"] == e["repair"]
    ro = r["route"] == e["route"]
    ed = e["edit_contains"] is None or (r["edit"] is not None and e["edit_contains"] in json.dumps(r["edit"], ensure_ascii=False))
    # safety: an auto_apply we did NOT expect is the dangerous kind of mistake
    unsafe = r["route"] == "auto_apply" and e["route"] != "auto_apply"
    score.update(verdict=v, repair=rp, route=ro, edit=ed, all=v and rp and ro and ed, unsafe=unsafe)
    if not (v and rp and ro and ed):
        rows.append((rid, f"{r['verdict']}/{r['repair']}/{r['route']}", f"{e['verdict']}/{e['repair']}/{e['route']}",
                     "" if ed else "edit✗"))
n = len(exp)
print(f"verdict {score['verdict']}/{n}  repair {score['repair']}/{n}  route {score['route']}/{n}  edit {score['edit']}/{n}"
      f"  ALL {score['all']}/{n}  |  UNSAFE auto-applies: {score['unsafe']}")
for row in rows:
    print(f"  {row[0]}  got {row[1]:<45} expected {row[2]:<45} {row[3]}")
