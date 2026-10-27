"""eval.run_questions · 用合成数据把 60 题各跑两遍，产物落 eval/runs/run1|run2

测量协议（为什么不带模型也能跑，以及这轮到底在测什么）：

- 契约 §7.1 规定 **D1 没有兜底**：没有模型回调时 D1 直接失败，answer.json 只会是 error。
  契约 §5.2 给了唯一合法的离线跑法：`--decide d1=inject:<文件>` 把「问题理解」这一步的
  输入固定下来。所以本脚本把 `eval/d1_truth.json`（每题一份 D1 输出，真值口径）注入 D1，
  **D2/D3/D4 一律走 ask/ 自己的兜底**——也就是说：这一轮量的是
  「给定问题理解，ask/ 能不能建计划、选表选列、生成并执行 SQL、给出可追溯的数字」。
- 两遍必须独立：run1 与 run2 是两次**独立的进程调用**，各自 `--out` 到不同目录，
  互相之间不复制、不共享任何产物（唯一共享的是只读输入：数据、字典、注入文件）。
- `--now` 固定成一个常数，避免「今天」漂移污染 §6.1 的同问两遍比对；
  时间口径只允许来自 D1.time_range（契约 §6.1 最后一条）。

用法：
    python -m eval.run_questions                    # 60 题 × 2 遍
    python -m eval.run_questions --only q001 q011   # 只跑几题（调试）
    python -m eval.run_questions --check-only       # 只做真值一致性自检，不跑
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
QUESTIONS = EVAL_DIR / "questions.jsonl"
D1_TRUTH = EVAL_DIR / "d1_truth.json"
INJECT_DIR = EVAL_DIR / "inject"
DICT = EVAL_DIR / "data" / "metrics.json"
RUNS = {"run1": EVAL_DIR / "runs" / "run1", "run2": EVAL_DIR / "runs" / "run2"}
NOW = "2026-01-01"                      # 固定「今天」，只为可复现；真实时点由 D1.time_range 表达
LOG = EVAL_DIR / "runs" / "run_log.json"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def metric_names() -> dict[str, str]:
    payload = json.loads(DICT.read_text(encoding="utf-8"))
    return {m["id"]: m["name"] for m in payload["metrics"]}


def data_files(q: dict) -> list[str]:
    tables = q["expect"]["tables"] or ["sales"]      # 没声明表的题（反问体）用事实表兜底
    out: list[str] = []
    for t in tables:
        rel = f"eval/data/{t}.csv"
        if not (ROOT / rel).exists():
            raise SystemExit(f"{q['id']} 声明了表 {t}，但 {rel} 不存在")
        out.append(rel)
    return out


def check_consistency(questions: list[dict], truth: dict[str, dict],
                      names: dict[str, str]) -> list[str]:
    """d1_truth 与 questions.jsonl 的 expect 必须一一对上；对不上就是评测器自己错，先停。"""
    errs: list[str] = []
    qids = [q["id"] for q in questions]
    for qid in qids:
        if qid not in truth:
            errs.append(f"{qid} 在 d1_truth.json 里没有 D1 口径")
    for qid in truth:
        if qid not in qids:
            errs.append(f"d1_truth.json 多了 {qid}（问题集里没有这题）")
    for q in questions:
        d1 = truth.get(q["id"])
        if not d1:
            continue
        ex = q["expect"]
        want_ids = {names[i] for i in d1["metric_ids"] if i in names}
        bad_ids = [i for i in d1["metric_ids"] if i not in names]
        if bad_ids:
            errs.append(f"{q['id']} 的 D1 用了字典里没有的 id：{bad_ids}")
        if want_ids != set(ex["metrics"]):
            errs.append(f"{q['id']} 指标不一致：D1={sorted(want_ids)} vs expect={sorted(ex['metrics'])}")
        # v1.1：time_grain_grouping != null 时按时间粒度分组，D3 产出派生列 period（契约 §2.4）
        d1_dims = set(d1["dimensions"]) | ({"period"} if d1.get("time_grain_grouping") else set())
        if d1_dims != set(ex["dims"]):
            errs.append(f"{q['id']} 维度不一致：D1={sorted(d1_dims)} vs expect={sorted(ex['dims'])}")
        if ex["status"] == "ok" and d1["ambiguity"]:
            errs.append(f"{q['id']} 期望能算出来，D1 却带 ambiguity（会变成 clarify）")
        # v1.1 硬规则自检（契约 §5.1）：注入的 D1 自己不合契约就别跑，别让评测器背锅
        for fld in ("listing", "time_column", "time_grain_grouping", "comparison"):
            if fld not in d1:
                errs.append(f"{q['id']} D1 缺 v1.1 必填字段 `{fld}`")
        if d1.get("listing") and (d1["metric_ids"] or d1["dimensions"]):
            errs.append(f"{q['id']} listing=true 却带了 metric_ids/dimensions（契约 §5.1 只许空）")
        g = (d1.get("time_range") or {}).get("grain")
        tg = d1.get("time_grain_grouping")
        if tg and g not in (tg, None):
            errs.append(f"{q['id']} time_range.grain={g} 与 time_grain_grouping={tg} 冲突（契约 §5.1）")
        cmp_ = d1.get("comparison")
        if cmp_:
            left = cmp_.get("left") or {}
            tr = d1.get("time_range") or {}
            if (tr.get("start"), tr.get("end")) != (left.get("start"), left.get("end")):
                errs.append(f"{q['id']} comparison 非空但 time_range != comparison.left（契约 §5.1）")
    return errs


def write_inject(qid: str, d1: dict) -> str:
    """注入文件路径在两次运行之间必须一致（meta.model.D1.inject_path 进产物、进 §6.1 比对）。"""
    INJECT_DIR.mkdir(parents=True, exist_ok=True)
    path = INJECT_DIR / f"{qid}.json"
    body = {"contract_version": "1.1", "decision": "D1", "input_digest": "", "output": d1}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"eval/inject/{qid}.json"


def run_one(q: dict, inject_rel: str, out_path: Path) -> dict:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()                      # 保证这一遍的产物是本遍新写的
    cmd = [sys.executable, "-m", "ask.query",
           "--question", q["q"],
           "--metric-dict", "eval/data/metrics.json",
           "--decide", f"d1=inject:{inject_rel}",
           "--now", NOW,
           "--out", str(out_path.relative_to(ROOT)).replace("\\", "/")]
    for d in data_files(q):
        cmd += ["--data", d]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}     # 不往 ask/ 里留 __pycache__
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    ms = int((time.time() - t0) * 1000)
    status = None
    if out_path.exists():
        try:
            status = json.loads(out_path.read_text(encoding="utf-8")).get("status")
        except Exception:  # noqa: BLE001 - 产物坏了就让 metrics 那步去报
            status = "unreadable"
    return {"exit": proc.returncode, "ms": ms, "status": status,
            "stderr": (proc.stderr or "").strip()[:400]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.run_questions",
                                 description="用合成数据把 60 题各跑两遍")
    ap.add_argument("--only", nargs="*", default=None, help="只跑这些题号")
    ap.add_argument("--check-only", action="store_true", help="只做真值一致性自检")
    args = ap.parse_args(argv)

    questions = load_jsonl(QUESTIONS)
    truth = json.loads(D1_TRUTH.read_text(encoding="utf-8"))
    names = metric_names()

    errs = check_consistency(questions, truth, names)
    if errs:
        print(f"真值口径不一致，{len(errs)} 处（先修评测器，别跑）：", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"真值一致性 OK：{len(questions)} 题，D1 口径与 expect 一一对上")
    if args.check_only:
        return 0

    picked = [q for q in questions if not args.only or q["id"] in set(args.only)]
    log: dict = {"protocol": {
        "d1": "inject（契约 §5.2）：D1 无兜底（§7.1），输入由 eval/d1_truth.json 固定",
        "d2_d3_d4": "ask/ 兜底（模型未配置）：最小计划 / 全列选表 / 不解读",
        "now": NOW, "metric_dict": "eval/data/metrics.json",
        "data": "eval/data/*.csv（合成数据，真值由 make_data.py 的固定公式决定）",
        "independence": "run1 与 run2 为两次独立进程调用，产物互不复制",
    }, "runs": {}}

    for run_name, out_dir in RUNS.items():
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n=== {run_name} → {out_dir.relative_to(ROOT)} ===")
        entries: dict[str, dict] = {}
        for q in picked:
            inject_rel = write_inject(q["id"], truth[q["id"]])
            info = run_one(q, inject_rel, out_dir / f"{q['id']}.json")
            entries[q["id"]] = info
            print(f"  {q['id']}  exit={info['exit']}  status={info['status']}  {info['ms']}ms")
        log["runs"][run_name] = entries

    existing = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() and args.only else None
    if existing:
        for run_name in ("run1", "run2"):
            existing["runs"][run_name].update(log["runs"][run_name])
        log = existing
    LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for run_name, out_dir in RUNS.items():
        n = len(list(out_dir.glob("q*.json")))
        print(f"{run_name}: {n} 个产物")
    print(f"日志：{LOG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
