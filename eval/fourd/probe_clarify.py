"""诊断：JOIN 题在 D1 就 `status=clarify` 的触发条件（不是判据，只是取证）。

本步 20 题里有 3 题（q032 / q035 / q040）没走到 D3，产物是
`{"clarify": {"question": "时间落在哪一列：sale_date 还是 register_date？", "asked_at_step": "D1"}}`。
这个脚本用同一个问题、同一份注入，只改两处变量，把结论钉死：

  1. 只给 sales.csv                        → 反问消失（说明触发条件与「第二张带日期列的表」有关）
  2. 给 sales.csv + customer.csv，D1 原样   → 反问出现
  3. 同上，但 D1.time_column 写死 sale_date → 反问消失
  4. 同上，D1 再加 time_range               → 反问照样出现（说明与「有没有时间范围」无关）

用法（仓库根目录；它不 import `eval.*`，按路径直接跑）：
    python eval/fourd/probe_clarify.py            # 默认探 q032
    python eval/fourd/probe_clarify.py q040

产物写到 `eval/fourd/probe/`，只读输入不动。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "eval" / "fourd" / "probe"


def load(qid: str) -> tuple[dict, dict]:
    decisions = json.loads((ROOT / "eval" / "fourd" / "decisions.json").read_text(encoding="utf-8"))
    entry = next(e for e in decisions["questions"] if e["id"] == qid)
    q = next(json.loads(x) for x in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines()
             if x.strip() and json.loads(x)["id"] == qid)
    return entry, q


def run(tag: str, question: str, d1: dict, tables: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    inject = OUT / f"{tag}.d1.json"
    inject.write_text(json.dumps({"decision": "D1", "contract_version": "1.1",
                                  "input_digest": "", "output": d1},
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    ans = OUT / f"{tag}.answer.json"
    if ans.exists():
        ans.unlink()
    cmd = [sys.executable, "-m", "ask.query", "--question", question,
           "--metric-dict", "eval/data/metrics.json", "--now", "2026-01-01",
           "--out", ans.relative_to(ROOT).as_posix(),
           "--decide", f"d1=inject:{inject.relative_to(ROOT).as_posix()}"]
    for t in tables:
        cmd += ["--data", f"eval/data/{t}.csv"]
    subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                   env={"PYTHONDONTWRITEBYTECODE": "1", "SYSTEMROOT": str(Path("C:/Windows"))})
    got = json.loads(ans.read_text(encoding="utf-8")) if ans.exists() else {}
    print(f"{tag:<28} status={got.get('status'):<8} "
          f"error={(got.get('error') or {}).get('code')} "
          f"clarify={(got.get('clarify') or {}).get('question')}")


def main() -> int:
    qid = sys.argv[1] if len(sys.argv) > 1 else "q032"
    entry, q = load(qid)
    d1 = entry["d1"]
    with_range = dict(d1, time_range={"start": "2025-01-01", "end": "2025-12-31", "grain": "year"})
    print(f"题目 {qid}：{q['q']}")
    run(f"{qid}_sales_only", q["q"], d1, ["sales"])
    run(f"{qid}_join_tcnull", q["q"], d1, ["sales", "customer"])
    run(f"{qid}_join_tcset", q["q"], dict(d1, time_column="sale_date"), ["sales", "customer"])
    run(f"{qid}_join_range_tcnull", q["q"], with_range, ["sales", "customer"])
    run(f"{qid}_join_range_tcset", q["q"], dict(with_range, time_column="sale_date"), ["sales", "customer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
