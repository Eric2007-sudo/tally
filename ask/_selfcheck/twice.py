"""#12 自检：q031–q040 连跑两遍，稳定子集必须逐字节一致（契约 §6.1）。"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "ask" / "_selfcheck" / "out"
TMP = ROOT / "ask" / "_selfcheck" / "_twice"
TMP.mkdir(parents=True, exist_ok=True)

questions = [
    json.loads(line)
    for line in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
wanted = {f"q0{i}" for i in range(31, 41)}
STABLE = ("question", "status", "plan", "plan_summary", "metrics_used", "sql", "columns", "rows", "trace", "charts", "narrative", "caveats", "clarify", "error")


def run_once(q, tag):
    cmd = [
        sys.executable, "-m", "ask.query",
        "--question", q["q"],
        "--metric-dict", "eval/data/metrics.json",
        "--decide", f"d1=inject:eval/inject/{q['id']}.json",
        "--now", "2026-01-01",
        "--out", f"ask/_selfcheck/_twice/{q['id']}.{tag}.json",
    ]
    for table in q["expect"]["tables"] or ["sales"]:
        cmd += ["--data", f"eval/data/{table}.csv"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, json.loads((ROOT / f"ask/_selfcheck/_twice/{q['id']}.{tag}.json").read_text(encoding="utf-8"))


bad = []
for q in questions:
    if q["id"] not in wanted:
        continue
    code1, first = run_once(q, "a")
    code2, second = run_once(q, "b")
    if code1 or code2:
        bad.append(f"{q['id']}: exit {code1}/{code2}")
        continue
    for key in STABLE:
        if json.dumps(first[key], ensure_ascii=False, sort_keys=False) != json.dumps(second[key], ensure_ascii=False, sort_keys=False):
            bad.append(f"{q['id']}: {key} 不稳定")
    if first["answer_id"] != second["answer_id"]:
        bad.append(f"{q['id']}: answer_id 不稳定")
    if first["sql"] and " JOIN " not in first["sql"][0]:
        bad.append(f"{q['id']}: SQL 里没有 JOIN")
    if first["status"] != "ok":
        bad.append(f"{q['id']}: status={first['status']}")

print("PASS" if not bad else "FAIL")
for item in bad:
    print("  -", item)
sys.exit(1 if bad else 0)
