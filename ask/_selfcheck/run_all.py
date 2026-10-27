"""自测：按 eval/rework.json 的口径把 60 题跑一遍，用 eval/metrics 的判据打分。

只读 eval/（questions.jsonl、d1_truth 的 inject 快照、metrics.json、data/*.csv），
产物只写 ask/_selfcheck/out/，不碰 eval/ 输出目录。

用法：PYTHONPATH=. python ask/_selfcheck/run_all.py [qid ...]
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval.metrics import judge_question, metric_hit_tiers, trace_completeness  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "ask" / "_selfcheck" / "out"
OUT.mkdir(parents=True, exist_ok=True)

all_questions = [
    json.loads(line)
    for line in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
wanted = set(sys.argv[1:])
questions = [q for q in all_questions if not wanted or q["id"] in wanted]
dict_index = {}
for metric in json.load(open(ROOT / "eval" / "data" / "metrics.json", encoding="utf-8"))["metrics"]:
    dict_index[metric["id"]] = metric

env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
passed, failed = [], []

for q in questions:
    qid = q["id"]
    cmd = [
        sys.executable, "-m", "ask.query",
        "--question", q["q"],
        "--metric-dict", "eval/data/metrics.json",
        "--decide", f"d1=inject:eval/inject/{qid}.json",
        "--now", "2026-01-01",
        "--out", f"ask/_selfcheck/out/{qid}.json",
    ]
    for table in q["expect"]["tables"] or ["sales"]:
        cmd += ["--data", f"eval/data/{table}.csv"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    path = OUT / f"{qid}.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ok, fails = judge_question(q, doc)
    tc = trace_completeness(doc, {})
    tiers = metric_hit_tiers(doc, dict_index)["buckets"]
    n_cells = tc["n_numbers"]
    n_traces = tc["n_traces"]
    good_trace = n_cells == n_traces and not tc["missing_paths"] and tc["strict_ok"] == n_traces
    numeric_cols = [c["name"] for c in (doc.get("columns") or []) if c.get("type") in ("int", "decimal")]
    nulls = sum(1 for row in (doc.get("rows") or []) for name in numeric_cols if row.get(name) is None)
    line = (
        f"{qid} {'PASS' if ok else 'FAIL'} rows={len(doc.get('rows') or [])} "
        f"cells={n_cells} traces={n_traces} trace_ok={good_trace} nulls={nulls} "
        f"hit={len(tiers['hit'])} miss={len(tiers['miss'])}/{len(tiers['hit']) + len(tiers['miss'])}"
    )
    (passed if ok else failed).append(qid)
    print(line)
    if not ok:
        for reason in fails:
            print("     -", reason)
    if proc.returncode != 0:
        print("     ! exit", proc.returncode, (proc.stderr or "").strip().splitlines()[-1:])
    if not good_trace:
        print("     ! trace:", json.dumps(
            {k: v for k, v in tc.items() if k != "per_trace"}, ensure_ascii=False)[:300])

print()
print(f"PASS {len(passed)}/{len(questions)}  failed: {' '.join(failed) if failed else '-'}")

# 有失败就必须非 0 退出：这个脚本常被当成门禁断言跑（cmd:python ...），
# 而「完成」只认 exit code。早先它永远 return 0，于是 11 条题不过、门禁照样绿 ——
# 一个不会失败的自检，等于没有自检。
sys.exit(1 if failed else 0)
