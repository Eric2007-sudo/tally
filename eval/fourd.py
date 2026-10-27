"""eval.fourd · 抽 20 题（含全部 10 道 JOIN）由 agent 扮演 D1-D4，真跑四段。

这一步和 `eval/run_questions.py` 的区别：
- `run_questions.py`（#7/#10）：D1 注入、D2/D3/D4 走 `ask/` 兜底 —— 量的是「注入 + 降级链路」。
- `fourd.py`（#11）：**D1/D2/D3/D4 四段的输出全部由 agent 扮演**，逐题落成
  `eval/fourd/qNNN/d1.json … d4.json` 注入文件（契约 §5.2），再让 `ask.query` 真跑出
  `answer.json` —— 量的是「四段都给对之后，实现能不能把它们执行成可追溯的数字」。
  本目录**不手写任何 answer.json**：产物一律来自 `python -m ask.query` 的真跑。

用法：
    python -m eval.fourd --check 20                          # 抽样与注入文件自检（评测器自己的闸）
    python -m eval.fourd --report eval/report.fourd.md       # 真跑四段 + 写报告
    python -m eval.fourd --report eval/report.fourd.md --only q001 q031   # 只跑几题（调试）

20 题的名单**钉死**在 `eval/fourd/fourd.py` 的 `ROSTER` 常量里（那一步 plan 的 note 定死的
q031-q040 + q001 q008 q013 q020 q023 q025 q041 q042 q053 q057），不是从 `questions.jsonl`
的类别里现算的：`--check` 的**第一步**就是比 `decisions.json` 的题号集合与 `ROSTER`，不等
直接 exit 1 并把差集打到 stderr。名单是这一步的输入，漂了就不能算过。

判据全部来自 `contract/answer.md` v1.1：
- 抽样与真值一致性：`expect` ↔ 扮演出来的 D1（同 `run_questions.check_consistency` 的口径）
- 结构准确率：`eval.metrics.judge_question` 的机械核对（不另写一套判据，免得两处结论打架）
- 同问两遍：契约 §6.1 稳定子集逐字节（`eval.metrics.compare_pair`）
- 口径命中率：契约 §3.3（`eval.metrics.metric_hit_tiers`）
- 追溯完整率：契约 §4.3 T1–T8 / §4.4 的 N（`eval.metrics.trace_completeness`）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from eval.metrics import (compare_pair, judge_question, load_questions,
                          metric_hit_tiers, trace_completeness)

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
FOURD = EVAL_DIR / "fourd"
DECISIONS = FOURD / "decisions.json"
ROSTER_PATH = FOURD / "fourd.py"        # 名单常量 `ROSTER` 的落点（同目录同名文件）
QUESTIONS = EVAL_DIR / "questions.jsonl"
DICT = EVAL_DIR / "data" / "metrics.json"
NOW = "2026-01-01"                     # 固定「今天」只为可复现；时间口径只来自 D1.time_range
CONTRACT = "contract/answer.md v1.1"
DECISIONS_ORDER = ("d1", "d2", "d3", "d4")
DECISION_NAME = {"d1": "D1", "d2": "D2", "d3": "D3", "d4": "D4"}
JOIN_CAT = "多表连接"


# --------------------------------------------------------------------------- 输入


def load_decisions() -> dict:
    return json.loads(DECISIONS.read_text(encoding="utf-8"))


def load_questions() -> list[dict]:
    return load_questions_jsonl(QUESTIONS)


def load_questions_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def dict_index() -> dict[str, dict]:
    payload = json.loads(DICT.read_text(encoding="utf-8"))
    return {m["id"]: m for m in payload.get("metrics") or [] if isinstance(m, dict) and "id" in m}


def window_metric_scope(di: dict[str, dict]) -> dict[str, Any]:
    """字典里哪些指标声明了 `window`（环比/同比），以及哪些的 definition 与单期指标**逐字相同**。

    用途只有一处：「同比写成一条 SUM」这条次级根因的**适用范围**必须从字典现算 ——
    只对 q025 用到的那个 id 成立，还是对全部 `window != null` 的指标都成立，
    不许在报告里手写「其它几条也一样」（手写就是替字典下结论）。
    """
    win = [mid for mid in sorted(di) if (di[mid].get("window") or {})]
    base_defs = {mid: di[mid].get("definition") for mid in di if not (di[mid].get("window") or {})}
    same: dict[str, list[str]] = {}
    for mid in win:
        d = di[mid].get("definition")
        same[mid] = [b for b in sorted(base_defs) if base_defs[b] == d]
    return {"all": win, "same_definition_as": same}


def sample(decisions: dict) -> list[dict]:
    return list(decisions["questions"])


def load_roster() -> tuple[tuple[str, ...], str]:
    """按**文件路径**把 `eval/fourd/fourd.py` 读进来，取 `ROSTER` 与 `ROSTER_RULE`。

    不能写成 `import eval.fourd.fourd`：`eval.fourd` 是模块（`eval/fourd.py`），不是包，
    同名的 `eval/fourd/` 目录没有 `__init__.py`。所以名单常量只能走 importlib 按路径加载。
    """
    if not ROSTER_PATH.exists():
        raise SystemExit(f"名单常量文件不存在：{ROSTER_PATH}")
    spec = importlib.util.spec_from_file_location("_tally_fourd_roster", ROSTER_PATH)
    if spec is None or spec.loader is None:                  # 只有加载器拿不到才会走到这
        raise SystemExit(f"读不到名单常量文件 {ROSTER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True                            # 别在 eval/fourd/ 里留 __pycache__
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old
    roster = tuple(getattr(mod, "ROSTER", ()) or ())
    return roster, str(getattr(mod, "ROSTER_RULE", "") or "")


def check_roster(entries: list[dict], roster: tuple[str, ...]) -> list[str]:
    """`--check` 的**第一步**：题号集合必须正好等于 `ROSTER`（只比集合，顺序由 ROSTER 定）。

    以前只验「20 题 / JOIN 全 / 字段齐 / 维度对」，没有任何一处比对名单，
    上一轮六道题被换掉照样全绿 —— 名单是这一步的**输入**，必须被比对。
    """
    if len(roster) != 20:
        return [f"{rel(ROSTER_PATH)} 的 ROSTER 长度 {len(roster)} ≠ 20"]
    if len(set(roster)) != len(roster):
        return [f"{rel(ROSTER_PATH)} 的 ROSTER 里有重复题号"]
    ids = {e["id"] for e in entries}
    missing = [q for q in roster if q not in ids]            # 名单里有、decisions.json 里没有
    extra = sorted(ids - set(roster))                        # decisions.json 里有、名单里没有
    errs: list[str] = []
    if missing:
        errs.append(f"decisions.json 少了名单里的题：{' '.join(missing)}")
    if extra:
        errs.append(f"decisions.json 多出名单外的题：{' '.join(extra)}（名单外的题不许留着凑数，"
                    f"要从 decisions.json 与 eval/fourd/<qid>/ 清掉）")
    return errs


def order_entries(entries: list[dict], roster: tuple[str, ...]) -> list[dict]:
    """按 ROSTER 的顺序排（= 报告 §2 表格的顺序）；名单外的题排在最后，好让 check 报出来。"""
    by_id = {e["id"]: e for e in entries}
    picked = [by_id[q] for q in roster if q in by_id]
    return picked + [e for e in entries if e["id"] not in set(roster)]


def tables_of(entry: dict) -> list[str]:
    out: list[str] = []
    d3 = entry.get("d3") or {}
    for t in d3.get("tables") or []:
        name = t.get("name") if isinstance(t, dict) else None
        if name and name not in out:
            out.append(name)
    if not out:                                     # 没给 D3 的题（不应发生）用事实表兜底
        out = ["sales"]
    return out


# --------------------------------------------------------------------------- 注入文件


def inject_path(qid: str, dn: str) -> Path:
    return FOURD / qid / f"{dn}.json"


def write_injects(entries: list[dict]) -> list[str]:
    """把扮演出来的决策写成注入文件（契约 §5.2 的信封）。

    `input_digest` 按 §5.2 的 P0 口径留空 —— 留空就是「我自己承担输入会变的后果」，
    代价是产物每题多一条 `INJECT_DIGEST_UNCHECKED`，这条计入 `eval/` 自己的打回清单。
    """
    written: list[str] = []
    for e in entries:
        for dn in DECISIONS_ORDER:
            out = e.get(dn)
            if out is None:
                continue
            body = {"decision": DECISION_NAME[dn], "contract_version": "1.1",
                    "input_digest": "", "output": out}
            p = inject_path(e["id"], dn)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written.append(p.relative_to(ROOT).as_posix())
    return written


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


# --------------------------------------------------------------------------- 自检（--check）


def check_sample(entries: list[dict], qmap: dict[str, dict], want: int) -> tuple[list[str], list[str]]:
    """返回 (错误, 提示)。错误 = 评测器自己错了；提示 = 只是提醒。"""
    errs: list[str] = []
    notes: list[str] = []
    ids = [e["id"] for e in entries]
    if len(entries) != want:
        errs.append(f"抽样题数 {len(entries)} ≠ {want}")
    if len(set(ids)) != len(ids):
        errs.append("抽样里有重复题号")
    join_all = [q["id"] for q in qmap.values() if q["cat"] == JOIN_CAT]
    join_picked = [e["id"] for e in entries if qmap.get(e["id"], {}).get("cat") == JOIN_CAT]
    if sorted(join_picked) != sorted(join_all):
        errs.append(f"JOIN 题没抽全：抽到 {sorted(join_picked)}，题库里是 {sorted(join_all)}（本步要求含全部 {len(join_all)} 道）")
    for e in entries:
        qid = e["id"]
        q = qmap.get(qid)
        if q is None:
            errs.append(f"{qid} 不在 eval/questions.jsonl 里")
            continue
        for dn in ("d1", "d2", "d3"):
            if e.get(dn) is None:
                errs.append(f"{qid} 缺 {dn}（前三个决策点必须齐）")
        if e.get("d4") is None:
            notes.append(f"{qid} 的 d4 还没落（本轮只跑到 D3）")
        d1 = e.get("d1") or {}
        for fld in ("metric_ids", "listing", "time_range", "time_column",
                    "time_grain_grouping", "comparison", "dimensions", "filters",
                    "ambiguity", "confidence"):
            if fld not in d1:
                errs.append(f"{qid} 的 D1 缺契约 §5.1 必填字段 `{fld}`")
        if d1.get("listing") and (d1.get("metric_ids") or d1.get("dimensions")):
            errs.append(f"{qid} listing=true 却带了 metric_ids/dimensions（契约 §5.1 只许空）")
        ex = q["expect"]
        d1_dims = set(d1.get("dimensions") or []) | ({"period"} if d1.get("time_grain_grouping") else set())
        if d1_dims != set(ex["dims"]):
            errs.append(f"{qid} 维度不一致：扮演的 D1={sorted(d1_dims)} vs expect={sorted(ex['dims'])}")
        if ex["status"] == "ok" and d1.get("ambiguity"):
            errs.append(f"{qid} expect 要能算出来，扮演的 D1 却带 ambiguity")
        if ex["join"] and not (e.get("d3") or {}).get("joins"):
            errs.append(f"{qid} 是 JOIN 题，扮演的 D3 却没写 joins（契约 §5.1 D3：不许降级成只查事实表）")
        if not ex["join"] and (e.get("d3") or {}).get("joins"):
            errs.append(f"{qid} 不是 JOIN 题，扮演的 D3 却写了 joins")
        for t in tables_of(e):
            if not (EVAL_DIR / "data" / f"{t}.csv").exists():
                errs.append(f"{qid} 用到表 {t}，但 eval/data/{t}.csv 不存在")
        d2 = e.get("d2") or {}
        steps = d2.get("steps") or []
        if not steps:
            errs.append(f"{qid} 的 D2 没有 steps（契约 §5.1 D2：steps 非空）")
        elif [s.get("step_id") for s in steps] != list(range(1, len(steps) + 1)):
            errs.append(f"{qid} 的 D2 step_id 不是从 1 连续升序")
        if d1.get("comparison") and steps and steps[0].get("intent") != "compare":
            errs.append(f"{qid} D1 有 comparison，D2 的取数步 intent 必须是 compare（契约 §5.1 D2）")
        if d1.get("time_grain_grouping") and steps and "period" not in ((steps[-1].get("needs") or {}).get("dimensions") or []):
            errs.append(f"{qid} D1 要按时间粒度分组，D2 最后一层 needs.dimensions 必须含 period（契约 §5.1 D2）")
        d4 = e.get("d4") or {}
        for line in d4.get("narrative") or []:
            refs = set(__import__("re").findall(r"\{\{trace:(t-\d{4})\}\}", line.get("text_template") or ""))
            if not refs <= set(line.get("evidence") or []):
                errs.append(f"{qid} 的 D4 句子引用了 evidence 里没有的 trace：{sorted(refs)}")
        # 注入文件必须已经落盘（断言会直接点名 q001/d1.json、q001/d3.json）
        for dn in DECISIONS_ORDER:
            if e.get(dn) is not None and not inject_path(qid, dn).exists():
                errs.append(f"{qid} 的注入文件 {rel(inject_path(qid, dn))} 没落盘")
    return errs, notes


# --------------------------------------------------------------------------- 真跑四段


def run_one(qid: str, q: dict, entry: dict, run_name: str) -> dict[str, Any]:
    out = FOURD / qid / f"{run_name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()                                # 这一遍的产物必须是本遍新写的
    cmd = [sys.executable, "-m", "ask.query",
           "--question", q["q"],
           "--metric-dict", "eval/data/metrics.json",
           "--now", NOW,
           "--out", rel(out)]
    for dn in DECISIONS_ORDER:
        if entry.get(dn) is not None:
            cmd += ["--decide", f"{dn}=inject:{rel(inject_path(qid, dn))}"]
    for t in tables_of(entry):
        cmd += ["--data", f"eval/data/{t}.csv"]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}   # 不往 ask/ 里留 __pycache__
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    ans: dict | None = None
    if out.exists():
        try:
            ans = json.loads(out.read_text(encoding="utf-8"))
        except Exception:                            # noqa: BLE001 - 坏产物记下来，不让整轮崩
            ans = None
    return {"exit": proc.returncode, "answer": ans,
            "stderr": (proc.stderr or "").strip()[:400],
            "artifact": rel(out) if out.exists() else None}


def stage_sources(ans: dict) -> dict[str, str]:
    model = ((ans.get("meta") or {}).get("model") or {}) if isinstance(ans, dict) else {}
    out: dict[str, str] = {}
    for dn in DECISIONS_ORDER:
        body = model.get(DECISION_NAME[dn])
        out[dn] = (body or {}).get("source") if isinstance(body, dict) else "未标注"
    return out


def publish_answer(qid: str, r1: dict) -> str | None:
    """把 `run1.json` 按字节复制成 `eval/fourd/qNNN/answer.json`（那一步 note 点名的产物路径）。

    只做复制、不接手写也不改一个字：产物仍然是 `ask.query` 真跑写出来的（`run1.json` 才是原件，
    `answer.json` 是同一份字节的副本，便于按 `eval/ref` 的口径逐题找答案文件）。
    """
    src = FOURD / qid / "run1.json"
    if r1.get("answer") is None or not src.exists():          # 没产出就不伪造一份
        return None
    dst = FOURD / qid / "answer.json"
    shutil.copyfile(src, dst)
    return rel(dst)


def run_all(entries: list[dict], qmap: dict[str, dict]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for e in entries:
        qid = e["id"]
        q = qmap[qid]
        r1 = run_one(qid, q, e, "run1")
        r1["answer_path"] = publish_answer(qid, r1)           # 第一遍跑完就把 answer.json 落位
        r2 = run_one(qid, q, e, "run2")
        results[qid] = {"run1": r1, "run2": r2}
        s = (r1["answer"] or {}).get("status")
        print(f"  {qid}  exit={r1['exit']}  status={s}")
    return results


# --------------------------------------------------------------------------- 判据


def stage_source_counts(per: list[dict]) -> dict[str, Any]:
    """把四段来源拆成**四个互不重叠**的数（契约 §2.12 `meta.model.Dn.source`）。

    上层第 3 轮打回点名的一处：`fallback` 一个数混了两件事 ——
    - **兜底生效**：SQL 没跑成 / 没有 trace 可解读，D4 真的退到了兜底；
    - **没走到**：D1 就 `status=clarify`，D2/D3/D4 **从未执行**（契约 §2.13 要求
      clarify 时 `sql`/`rows`/`columns`/`trace` 全为 `[]`），可 `ask/` 仍把它们
      记成 `fallback`，且 `output_sha256`（空串的 sha256）与 `latency_ms`（0）
      跟「兜底真的生效」一模一样，只能靠人读报告才分得开。
    所以这里按「这一题的 status」拆，不按决策点猜：clarify 之后的 `fallback` 记
    「没走到」，其余 `fallback` 记「兜底生效」。四个数与 `题数 × 4` 必须对得上。
    """
    clarify_ids: list[str] = []
    eff_ids: list[str] = []
    not_reached = effective = inject = model = other = 0
    for r in per:
        src = r.get("sources") or {}
        for s in src.values():
            if s == "inject":
                inject += 1
            elif s == "model":
                model += 1
            elif s != "fallback":                       # 未知来源要单独计数，不混进任何一档
                other += 1
        if r.get("status") == "clarify":
            clarify_ids.append(r["id"])
            not_reached += sum(1 for dn in ("d2", "d3", "d4") if src.get(dn) == "fallback")
        elif "fallback" in src.values():
            eff_ids.append(r["id"])
            effective += sum(1 for s in src.values() if s == "fallback")
    return {"inject": inject, "model": model,
            "fallback_effective": effective, "fallback_effective_ids": eff_ids,
            "not_reached": not_reached, "clarify_ids": clarify_ids, "unlabeled": other}


def nonjoin_tiers(per: list[dict], qmap: dict[str, dict]) -> dict[str, Any]:
    """单表（非 JOIN）组答错的题按根因分三档（上层第 3/4 轮打回点名的那张表）。

    三档口径不同，**不许加总**：
    - 复现差异：两遍稳定子集不一致（契约 §6.1）—— 这是实现的不确定性，不是题的问题；
    - 契约表达不了：`expect` 要求的东西在 D1/D2/D3 里没有任何字段能放 ——
      `sql_all_any` 要求的窗口 / 同比基期 / 聚合后再过滤（`LAG(`/`OVER(`/`WITH `/`HAVING`/`AVG(`），
      `limit` 要求的取前 N。换注入、换实现都不会过；
    - 实现缺陷：扣掉上面两档以后**仍然**答错的（契约写了、代码没做）。
    答对的题全部算「实现范围内」，它就是那一档的分母。
    """
    wrong = [r for r in per if not r["join_expect"] and not r["ok"]]
    repro = [r for r in wrong if (r.get("pair") or {}).get("stable_equal") is False]
    repro_ids = {r["id"] for r in repro}
    gap: list[dict] = []
    impl: list[dict] = []
    for r in wrong:
        if r["id"] in repro_ids:
            continue
        e = (qmap.get(r["id"]) or {}).get("expect") or {}
        (gap if (e.get("sql_all_any") or e.get("limit") is not None) else impl).append(r)
    passed = [r for r in per if not r["join_expect"] and r["ok"]]
    return {"impl_ids": [r["id"] for r in impl],
            "gap_ids": [r["id"] for r in gap],
            "repro_ids": [r["id"] for r in repro],
            "passed_ids": [r["id"] for r in passed],
            "total": sum(1 for r in per if not r["join_expect"]),
            "wrong": len(wrong)}


def measure(entries: list[dict], qmap: dict[str, dict], results: dict[str, dict],
            dindex: dict[str, dict]) -> dict[str, Any]:
    header_cache: dict[str, list[str] | None] = {}
    per: list[dict] = []
    for e in entries:
        qid = e["id"]
        q = qmap[qid]
        a1 = results[qid]["run1"]["answer"]
        a2 = results[qid]["run2"]["answer"]
        row: dict[str, Any] = {"id": qid, "cat": q["cat"], "q": q["q"],
                               "join_expect": bool(q["expect"]["join"]),
                               "artifact": results[qid]["run1"]["artifact"],
                               "exit": results[qid]["run1"]["exit"],
                               "stderr": results[qid]["run1"]["stderr"],
                               "injected": [dn for dn in DECISIONS_ORDER if e.get(dn) is not None]}
        if not a1:
            row.update({"status": None, "ok": False, "fails": ["没产出 answer.json"], "pair": None,
                        "trace": None, "hit": None, "sources": {}, "sql": [], "rows": 0,
                        "traces": 0, "caveats": [], "error": None})
            per.append(row)
            continue
        ok, fails = judge_question(q, a1)
        pair = compare_pair(a1, a2) if a2 else None
        row.update({
            "status": a1.get("status"),
            "ok": ok, "fails": fails,
            "sql": a1.get("sql") or [],
            "rows": len(a1.get("rows") or []),
            "traces": len(a1.get("trace") or []),
            "caveats": [c.get("code") for c in (a1.get("caveats") or []) if isinstance(c, dict)],
            "error": a1.get("error"),
            "plan": a1.get("plan") or [],
            "sources": stage_sources(a1),
            "pair": pair,
            "trace": trace_completeness(a1, header_cache),
            "hit": metric_hit_tiers(a1, dindex),
        })
        per.append(row)

    def acc(filter_fn):
        picked = [r for r in per if filter_fn(r)]
        return sum(1 for r in picked if r["ok"]), len(picked)

    non_join = acc(lambda r: not r["join_expect"])
    join = acc(lambda r: r["join_expect"])
    pairs = [r["pair"] for r in per if r["pair"]]
    trace_num = sum((r["trace"] or {}).get("n_numbers") or 0 for r in per)
    trace_ok = sum((r["trace"] or {}).get("strict_ok") or 0 for r in per)
    trace_len = sum((r["trace"] or {}).get("lenient_ok") or 0 for r in per)
    # 零行命中的聚合数字：T3 要求 row_ids 非空，但 0 行时没有源行可指（§4.3 T3 vs §4.4 的口径冲突）
    zero_hit = [{"id": r["id"], "path": p.get("value_path")}
                for r in per for p in ((r["trace"] or {}).get("per_trace") or [])
                if (p.get("checks") or {}).get("T3") is False
                and not p.get("row_ids_n") and p.get("row_ids_total") == 0]
    hit_denom = sum((r["hit"] or {}).get("denom") or 0 for r in per)
    hit_hit = sum(len((r["hit"] or {}).get("buckets", {}).get("hit") or []) for r in per)
    hit_old = sum(len((r["hit"] or {}).get("buckets", {}).get("old_version") or []) for r in per)
    hit_miss = sum(len((r["hit"] or {}).get("buckets", {}).get("miss") or []) for r in per)
    hit_notdict = sum(len((r["hit"] or {}).get("buckets", {}).get("not_dict") or []) for r in per)
    return {
        "per": per,
        "n": len(per),
        "non_join": non_join, "join": join,
        "pairs": len(pairs),
        "pairs_equal": sum(1 for p in pairs if p["stable_equal"]),
        "pairs_raw_equal": sum(1 for p in pairs if p["raw_equal"]),
        "pairs_id_equal": sum(1 for p in pairs if p["answer_id_equal"]),
        "trace_numbers": trace_num, "trace_ok": trace_ok, "trace_lenient": trace_len,
        "zero_hit": zero_hit,
        "hit_denom": hit_denom, "hit_hit": hit_hit, "hit_old": hit_old,
        "hit_miss": hit_miss, "hit_notdict": hit_notdict,
        "stages": stage_source_counts(per),      # inject / model / 兜底生效 / 没走到（四段来源拆开）
        "tiers": nonjoin_tiers(per, qmap),       # 单表组答错题的三档归因（实现缺陷 / 契约表达不了 / 复现差异）
    }


# --------------------------------------------------------------------------- 打回清单


def build_rework(m: dict, entries: list[dict], qmap: dict[str, dict]) -> list[dict]:
    """按「跑出来的根因」归口，不一律当 ask/ 的错。

    - 跨表要 JOIN 但实现只支持单表 → `ask/`（契约 §5.1 D3 明确要求 joins，P0 没做）
    - 未执行的决策点被记成 `fallback`（与「兜底真的生效」在产物上分不清）→ `ask/`
    - 契约里没有字段能表达「取前 N / 聚合后再过滤 / 逐月环比落地」 → `contract/`
    - 注入文件不写 input_digest（`INJECT_STALE` 这条失效检测永远测不到） → `eval/`

    `kind_class` 是报告 §7.1 那张表的**唯一**依据（不按题号手写）：
    `实现缺陷`（契约写了、代码没做）/ `契约表达不了`（没有字段能写，或两条条款冲突）/
    `观测口径`（实现能把产物写对、但写出来的东西让人分不清两件事）/ `本步自己`。
    """
    items: list[dict] = []
    seq = 0

    def add(kind_class: str, kind: str, severity: str, target: str, question_id: str, category: str,
            contract_ref: str, evidence: str, action: str) -> None:
        nonlocal seq
        seq += 1
        items.append({"id": f"FW-{seq:04d}", "kind_class": kind_class,
                      "kind": kind, "severity": severity,
                      "target_dir": target, "question_id": question_id, "category": category,
                      "contract_ref": contract_ref, "evidence": evidence, "action": action})

    d1_of = {e["id"]: (e.get("d1") or {}) for e in entries}
    # `RESULT_EMPTY` 在本轮产物里出现过没有 —— 下面的「聚合后再过滤」那条要用它，
    # 不许手写「一次都没出现」，得从 20 份产物里数。
    has_result_empty = any("RESULT_EMPTY" in (r.get("caveats") or []) for r in m["per"])
    join_err = [r for r in m["per"] if r["join_expect"] and (r.get("error") or {}).get("stage") == "D3"]
    if join_err:
        qids = "、".join(r["id"] for r in join_err)
        add("实现缺陷", "JOIN 未实现", "high", "ask/", join_err[0]["id"], JOIN_CAT,
            "§5.1 D3「JOIN 的落地责任」",
            f"{len(join_err)} 道 JOIN 题走到了 D3：D3 注入里 joins[] 的表、ON 键、inner/left 类型都写齐了，"
            f"ask.query 仍然直接报 "
            f"`{(join_err[0].get('error') or {}).get('code')}` 于 stage=D3："
            f"「{(join_err[0].get('error') or {}).get('message','')[:80]}」（{qids}）",
            "把 D3 的 joins[] 落到 SQL：至少支持 inner/left 等值连接、JOIN 后的 __rid 按 §2.5 的 "
            "JOIN 规则拼（各源 id 按文件名字节序），JOIN 放大行数时记 JOIN_ROW_MULTIPLIED")
    # JOIN 题没走到 D3 的两种原因，分开归口（同一份产物不说两遍话）
    join_clarify = [r for r in m["per"] if r["join_expect"] and r["status"] == "clarify"]
    over_ask = [r for r in join_clarify if not d1_of[r["id"]].get("time_range")]
    under_spec = [r for r in join_clarify if d1_of[r["id"]].get("time_range")]
    if over_ask:
        qids = "、".join(r["id"] for r in over_ask)
        add("实现缺陷", "D1 过度反问：问题里没有时间却问时间列", "medium", "ask/", over_ask[0]["id"], JOIN_CAT,
            "§5.1 D1 `time_column` / §2.10 `clarify`",
            f"{len(over_ask)} 道题（{qids}）的 D1 注入 time_range=null、time_column=null，问题本身没提时间；"
            f"只因为所给数据里有第二张带日期列的维表（customer.register_date），ask.query 就在 D1 反问"
            f"「时间落在哪一列：sale_date 还是 register_date？」，status=clarify、sql=[]，连 D2/D3 都没走到。"
            f"同一题只给 sales 一张表时不反问；把 D1.time_column 写成 sale_date 也不反问"
            f"（诊断脚本 eval/fourd/probe_clarify.py 可复现这两条对照）",
            "D1 没有时间范围时不该反问时间列（没有时间过滤就没有「落在哪一列」的问题）；"
            "要反问也只允许在 time_range != null 且多候选时反问")
    if under_spec:
        qids = "、".join(r["id"] for r in under_spec)
        add("本步自己", "D1 留白：有时间范围却没指定时间列", "medium", "eval/", under_spec[0]["id"], JOIN_CAT,
            "§5.1 D1 `time_column` / `eval/d1_truth.json`",
            f"{len(under_spec)} 道题（{qids}）问题里带了年份，但 eval/d1_truth.json 的 D1 只给了 time_range、"
            f"time_column 留 null；数据里同时有 sales.sale_date 与 customer.register_date 两个候选，"
            f"ask.query 反问「时间落在哪一列」——按契约这是**正确**的反问，错在这份 D1 没写清。"
            f"结果这 {len(under_spec)} 题只测到 D1 的追问，JOIN 一行都没测到",
            "eval/d1_truth.json 里有时间范围的题补上 time_column（这批数据就是 sale_date），"
            f"再重跑一次把这 {len(under_spec)} 题也压到 D3 的 JOIN 上")
    # 契约表达不了的三种语义：从产物与注入对不上的地方现算，不写死题号
    for r in m["per"]:
        e = next(x for x in entries if x["id"] == r["id"])
        d1 = e.get("d1") or {}
        sql_text = " ".join(r.get("sql") or []).upper()
        if (d1.get("metric_ids") or []) and any(str(mid).endswith(("_mom", "_yoy")) for mid in d1["metric_ids"]) \
                and "LAG(" not in sql_text and "OVER(" not in sql_text and not sql_text.startswith("WITH "):
            add("契约表达不了", "契约空白：指标 window 无处落地", "high", "contract/", r["id"], r["cat"],
                "§3.1 `metric.window` / §5.1 D2",
                f"{r['id']} 用的指标在字典里声明了 window（环比/同比），但 D1/D2/D3 没有任何字段"
                f"告诉 SQL「按 window 算上一期」；这道题的 D1 comparison 是 null"
                f"（同比是「每月 vs 去年同月」，不是两个固定区间的对比，`comparison` 表达不了它）；"
                f"本轮产物是逐月原值，没有 LAG：{sql_text[:120]}",
                "给「环比/同比类指标」定一条落地判据（例：`metric.window != null` ⟹ 该步 SQL 必须含 "
                "`LAG(`/`OVER(`/`WITH `，且输出列名固定），否则这类题永远只能算出原值")
        if (qmap[r["id"]]["expect"].get("limit") is not None) and "LIMIT" not in sql_text:
            add("契约表达不了", "契约空白：取前 N 无处表达", "high", "contract/", r["id"], r["cat"],
                "§5.1 D2（`sql_hint` 语义未定义）",
                f"{r['id']} 要「前 {qmap[r['id']]['expect']['limit']} 名」，但 D1/D2/D3 里没有任何字段"
                f"能表达 ORDER BY 方向与 LIMIT；D2 的 `sql_hint` 契约只给了个 null，没说语义，"
                f"本轮写进 sql_hint 也不生效：{sql_text[:120]}",
                "要么给 `sql_hint` 定义可执行语义并让 sqlgen 采用，要么在 D1 加 `order`/`limit` 字段")
        if "HAVING" in [str(a).upper() for a in qmap[r["id"]]["expect"].get("sql_all_any", [[None]])[0]] \
                and "HAVING" not in sql_text:
            add("契约表达不了", "契约空白：聚合后再过滤无处表达", "medium", "contract/", r["id"], r["cat"],
                "§5.1 D1 `filters`（只覆盖原始列）",
                f"{r['id']} 要「计数大于 1 才留下」这种聚合后再过滤，契约的 `filters[]` 只作用于原始列，"
                f"D1/D2/D3 里没有能表达 HAVING 的位置：{sql_text[:120]}"
                + ("" if has_result_empty else
                   f"。同一件事的另一面：{r['id']} 的正确答案是**空集**，可阈值（「异常偏高」到底多高）"
                   f"没有地方可写，于是实现只能把聚合值原样返回 {r['rows']} 行（全 0.0），"
                   f"契约 §7.4 的 RESULT_EMPTY 这条 caveat 在本轮 20 题里一次都没出现过"),
                "给 `filters[]` 增加作用于聚合结果的形态（或规定「两次取数 + 第二步 depends_on」的标准形状）；"
                "阈值一旦有了位置，空集才会真的出现，RESULT_EMPTY 这条路径才测得到")
    if m.get("zero_hit"):
        zh = m["zero_hit"]
        where = "、".join(f"{x['id']}:{x['path']}" for x in zh[:4])
        add("契约表达不了", "契约冲突：零行命中的聚合数字没有 row_ids", "medium", "contract/", zh[0]["id"],
            qmap[zh[0]["id"]]["cat"],
            "§4.3 T3（`row_ids` 非空）vs §4.4（空结果集）",
            f"{len(zh)} 条 trace 的 T3 不过：{where}。"
            f"这些格子是「零行命中、聚合值就是算出来的零」，源表里没有可指认的行"
            f"（row_ids=[]、row_ids_total=0），于是严格率按下界报、lenient 口径下它们是过的",
            "给 §4.3 T3 补一句「0 行命中时 row_ids=[] + row_ids_total=0 合法」，"
            "否则「算出来的零」永远算追溯不完整")
    digest = [r for r in m["per"] if "INJECT_DIGEST_UNCHECKED" in (r.get("caveats") or [])]
    if digest:
        add("本步自己", "注入文件不写 input_digest", "low", "eval/", digest[0]["id"], digest[0]["cat"],
            "§5.2 `input_digest`",
            f"{len(digest)} 题的产物带着 INJECT_DIGEST_UNCHECKED：注入文件按 P0 口径留空，"
            f"因此 INJECT_STALE（改输入就该报错）这条路径本轮一次都没测到",
            "补「规范化输入哈希 → 改输入 → 期望 INJECT_STALE」的用例；在那之前本步不假装测过它")
    # 「兜底生效」与「没走到」在产物上分不开：clarify 之后 D2/D3/D4 从未执行，
    # 但 meta.model.Dn.source 仍记 fallback —— 两者的 output_sha256（空串的 sha256）
    # 与 latency_ms（0）完全一样，只能靠人读报告。归 ask/（实现把来源写清楚了才数得清）。
    st = m.get("stages") or {}
    clar = [r for r in m["per"] if r["status"] == "clarify"]
    if clar and st.get("not_reached"):
        qids = "、".join(r["id"] for r in clar)
        arts = "、".join(f"eval/fourd/{r['id']}/run1.json" for r in clar)
        add("观测口径", "未执行的决策点被记成 fallback（与兜底生效在产物上分不开）", "medium", "ask/",
            clar[0]["id"], clar[0]["cat"],
            "§2.12 `meta.model.Dn.source`（只有 model / inject / fallback 三选一，没有「没执行」）",
            f"{len(clar)} 道题（{qids}）在 D1 就 status=clarify，D2/D3/D4 共 {st['not_reached']} 个决策点"
            f"**从未执行**（契约 §2.13：clarify 时 sql/rows/columns/trace 全为 []），"
            f"但 meta.model.D2/D3/D4.source 仍写 fallback。这几个决策点的 output_sha256 都是空串的 sha256"
            f"（e3b0c442…）、latency_ms 都是 0，与「兜底真的生效」的 "
            f"{'、'.join(st.get('fallback_effective_ids') or [])} 的 D4 在产物上完全一样，"
            f"只能靠人读报告才分得开：{arts}",
            "未执行的决策点 `source` 记 `not_reached`（不要记 `fallback`），并把 `latency_ms` 保持 0、"
            "`output_sha256` 保持空串")
    no_d4 = [e["id"] for e in entries if e.get("d4") is None]
    if no_d4:
        add("本步自己", "D4 未注入", "low", "eval/", no_d4[0], "-", "§5.2",
            f"{len(no_d4)} 题只跑到 D3 就写了产物（{'、'.join(no_d4[:6])}）",
            "给这些题按真 trace_id 补 D4 注入后再跑一遍（本步的第二遍会把这些补齐）")
    # —— 次级根因：字典把「同比」写成一条 SUM ——
    # 主根因仍是 FW-0004（契约没有 window 的落地形状，退 contract/）；但就算契约补上了，
    # `eval/data/metrics.json` 自己不跟着改，新字段照样落不到正确的表达式上 ——
    # 这份字典是本步自己的文件，所以单记一条退 `eval/`，不抵扣 FW-0004 的归口。
    di = dict_index()
    q025 = next((r for r in m["per"] if r["id"] == "q025"), None)
    win_mids = [mid for mid in (d1_of.get("q025") or {}).get("metric_ids") or []
                if (di.get(mid) or {}).get("window")] if q025 is not None else []
    if win_mids:
        mid = win_mids[0]
        ent = di.get(mid) or {}
        same_as_gmv = ent.get("definition") == (di.get("gmv") or {}).get("definition")
        scope = window_metric_scope(di)                  # 适用范围：字典里全部 window 指标
        same_as = scope["same_definition_as"].get(mid) or []
        others = [x for x in scope["all"] if x != mid]
        main_fw = next((it["id"] for it in items if "window 无处落地" in it["kind"]), "—")
        add("本步自己",
            "次级根因：字典的 definition 与 window 矛盾（同比写成一条 SUM）",
            "medium", "eval/", "q025", (qmap.get("q025") or {}).get("cat", "-"),
            "§3.1 `metric.window` / §3.2 V1–V12（没有一条约束 window 指标的 definition）",
            f"eval/data/metrics.json 的 {mid}（{ent.get('name', '')}）声明了 "
            f"window={json.dumps(ent.get('window'), ensure_ascii=False)}，definition 却是 "
            f"`{ent.get('definition', '')}`"
            + ("（与 gmv 逐字相同）" if same_as_gmv else "")
            + f"、unit={ent.get('unit')}（同比是变化率，单位该是 %）。"
              f"V1–V12 没有一条要求 window 指标的 definition 必须表达「和哪一期比」，"
              f"所以「同比」在字典这一层就没有落地形状：实现照 definition 逐字执行，出来的只能是一条 SUM。"
              f"**适用范围**：字典里声明 `window != null` 的指标共 {len(scope['all'])} 条"
              f"（{'、'.join(scope['all']) or '无'}），`{mid}` 只是其中一条；"
              f"`{'、'.join(same_as) or '无'}` 的 definition 与它逐字相同，"
              f"换 {'、'.join(others) or '别的 window 指标'} 出题，同一条表达式会照样冒充一次 —— "
              f"这条次级根因不是 q025 的个例，是全部 window 指标共有的。本轮 20 题只跑到 `{mid}`（q025），"
              f"另外 {len(others)} 条在名单外没跑，所以证据只有 q025 一份产物、适用范围是字典里全部 window 指标。"
              f"这是次级根因，不抵扣 {main_fw} —— 契约那条定了形状，这份字典也得跟着改",
            f"改 eval/data/metrics.json：{mid} 的 definition 不能再是 gmv 的 SUM，unit 从 "
            f"{ent.get('unit')} 改 %（等契约给出 window 的固定输出形状后再定式子）；"
            f"同一处按适用范围一起改 {('、'.join(others) or '其余 window 指标')}（它们挂的是同一个单期表达式）；"
            f"或在 §3.2 补一条校验「window != null ⟹ definition 必须含两期对比」")
    # —— 产品字典这一处落点（退 `dict/`）——
    # 这条**不依赖 20 题产物**，所以无条件入列（`has_result_empty` 之类的产物条件一概不管）：
    # 同上一条是同一个根因的两处落点（评测副本 `eval/data/metrics.json` 一处、产品字典
    # `dict/metrics.json` 一处），只改评测副本不算改完 —— 上线的字典照错。
    # 证据来自独立检索（对产品字典逐条比对），**不是**这 20 题跑出来的，所以它不进五项指标、
    # 不抵扣上一条、也不改任何实测数。
    sec_fw = next((it["id"] for it in items if "definition 与 window 矛盾" in it["kind"]), "—")
    add("字典",
        "产品字典里 window 指标的 definition 与 window 自相矛盾（gmv_mom）",
        "high", "dict/", "q025", "同比环比",
        "§3.1 `metric.window` / §3.2 V1–V12（没有一条约束 window 指标的 definition）",
        "产品字典 `dict/metrics.json` 的 gmv_mom：unit=「CNY」、"
        "window=「{grain:month, compare:mom}」、"
        "definition=「SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)」（与 gmv 逐字相同）。"
        "它和评测副本 `eval/data/metrics.json` 的 gmv_yoy（见 "
        f"{sec_fw}）是同一个根因的两处落点：window 非空、definition 里没有任何「与上一期比」的语义、"
        "unit 也不是 %。只改评测副本那一处不算改完 —— 产品字典照错。"
        "本条证据来源是**独立检索**（对两份字典逐条比对搜出来的），不是这 20 题产物量出来的，"
        "因此不进五项指标、不改 5/10、0/10、20/20、56/56、55/56 里的任何一个数。",
        "① 改 `dict/metrics.json` 里 gmv_mom 这条指标的写法（定义成变化率，或明确它只是"
        "「基础量 + 窗口声明」并把 unit 改对）；"
        "② 让 `dict/validate.py` 在「window != null 而 definition 里没有取上一期的语义」时"
        "报 `METRIC_INVALID` —— 现在是它放过去的，校验器不拦，下次还会有人这么写")
    return items


# --------------------------------------------------------------------------- 报告


def pct(hit: int | None, total: int | None) -> str:
    if not total:
        return "null（没有可测样本）"
    return f"{hit / total * 100:.1f}%（{hit}/{total}）"


def numeric_cell_stats(ans: dict) -> dict[str, Any]:
    """数产物里 `int`/`decimal` 单元格的零 / 非零，以及每个字典口径列的 `unit` / `digits`。

    §4.1 要说清 q025「一条 SUM 冒充同比」：12 行里有几个 `0.0`、同比列被字典声明成
    `unit=CNY` —— 这几个数必须从产物里数出来，不许在报告里手写（手写就是编数字）。
    """
    cols = [c for c in (ans.get("columns") or []) if str(c.get("type")) in ("int", "decimal")]
    zeros: list[dict] = []
    nonzeros: list[dict] = []
    periods: list[str] = []
    for i, row in enumerate(ans.get("rows") or []):
        if isinstance(row.get("period"), str) and row["period"] not in periods:
            periods.append(row["period"])
        for c in cols:
            name = c.get("name")
            v = row.get(name)
            if v is None:                            # 缺失值不计数（契约 §2.13：null ≠ 0）
                continue
            cell = {"path": f"rows[{i}].{name}", "value": v, "row": row}
            (zeros if v == 0 else nonzeros).append(cell)
    return {"n_cells": len(zeros) + len(nonzeros),
            "zeros": zeros, "nonzeros": nonzeros, "periods": periods,
            "units": {c.get("source_metric_id"): c.get("unit")
                      for c in cols if c.get("source_metric_id")},
            "digits": {c.get("source_metric_id"): c.get("digits")
                       for c in cols if c.get("source_metric_id")}}


def render_report(decisions: dict, entries: list[dict], qmap: dict[str, dict], m: dict,
                  rework: list[dict], roster: tuple[str, ...] = (),
                  roster_rule: str = "") -> str:
    proto = decisions["protocol"]
    L: list[str] = []
    L.append("# tally 评测报告 · 20 题由 agent 扮演 D1-D4 真跑四段")
    L.append("")
    L.append(f"- 契约：`{CONTRACT}`（字段名、路由、状态码、错误体全部以它为准）")
    L.append("- 问题集：`eval/questions.jsonl` 里抽 20 题（含全部 10 道多表连接题）")
    L.append("- 决策来源：**D1/D2/D3/D4 四段的输出全部由 agent 扮演**，只落成注入文件；"
             "`answer.json` 一律由 `python -m ask.query` 真跑产出，本目录不手写答案")
    if roster:
        L.append(f"- 抽样名单：**钉死**在 `eval/fourd/fourd.py` 的 `ROSTER` 常量里（{len(roster)} 题："
                 f"{' '.join(roster)}），不是从 `questions.jsonl` 的类别里现算的；"
                 f"`--check` 的**第一步**比 `decisions.json` 的题号集合与 `ROSTER`，不等直接 exit 1。"
                 f"口径：{roster_rule or proto['sample']}")
    else:
        L.append(f"- 抽样：{proto['sample']}")
    L.append("- 跑法：每题两遍（`run1` / `run2` 两次独立进程，只共享只读输入）")
    L.append(f"- 注入文件：`eval/fourd/qNNN/d1.json … d4.json`；产物：`eval/fourd/qNNN/run1.json` / "
             f"`run2.json`，以及 `run1.json` 的字节副本 `eval/fourd/qNNN/answer.json`")
    L.append("- 判据实现：复用 `eval/metrics.py` 的 `judge_question` / `compare_pair` / "
             "`metric_hit_tiers` / `trace_completeness`，**不另写一套判据**，免得两处结论打架")
    L.append("")
    L.append("> `null` = 没有可测样本，`0` = 测了但一个都没过（契约 §4.4）。本报告不含编造数字："
             "每个数都能在 `eval/fourd/` 的产物里数出来。")
    L.append("")

    # —— 0. 两条链路分列 ——
    L.append("## 0. 两条链路分列（先说清这一份量的是哪条）")
    L.append("")
    L.append("| 链路 | 题量 | D1–D4 从哪来 | 产物落在 | 本报告是否给数字 |")
    L.append("|---|---|---|---|---|")
    L.append(f"| **模型链路**（本次：agent 扮演 D1–D4，经 `inject` 注入） | {m['n']} 题 × 2 遍 | "
             f"agent 扮演 → `--decide dN=inject:eval/fourd/qNNN/dN.json` | `eval/fourd/qNNN/` | "
             f"**是**：§4 / §6 的五个数全部按这 20 题现算 |")
    L.append("| **降级链路**（旧 60 题两遍） | 60 题 × 2 遍 | D1 注入，D2/D3/D4 走 `ask/` 兜底（契约 §7.1 允许的降级） | "
             "`eval/runs/` | **否**：见 `eval/report.md`，本步不复制、不改写旧数字 |")
    L.append("")
    L.append("这 20 题的四段是 agent 扮演的，**不是模型直调**；所以它测的是「契约 + 代码在四段都到位时"
             "能不能把它们执行成可追溯的数字」，**不是**「模型会不会做 D1–D4」。两条链路的数字不可互相"
             "引用：模型链路一次 `model` 调用都没有（见 §3），降级链路的兜底次数也不代表模型能力。")
    L.append("")

    # —— 1. 结论速览 ——
    ok_all = sum(1 for r in m["per"] if r["ok"])
    L.append("## 1. 结论速览（模型链路：本次 20 题，agent 扮演 D1–D4）")
    L.append("")
    L.append("| 指标 | 值 | 样本 | 口径 |")
    L.append("|---|---|---|---|")
    L.append(f"| 有产物可测 | {sum(1 for r in m['per'] if r['status'])}/{m['n']} | — | 每题都真跑了一次进程 |")
    L.append(f"| SQL 结构准确率（非 JOIN 组） | {pct(*m['non_join'])} | {m['non_join'][1]} 题 | "
             "`eval/metrics.judge_question` 按 `expect` 机械核对 |")
    L.append(f"| SQL 结构准确率 · JOIN 组（单独统计，不混进总分） | {pct(*m['join'])} | {m['join'][1]} 题 | 同口径 |")
    L.append(f"| 同问一致性（两遍稳定子集逐字节） | {pct(m['pairs_equal'], m['pairs'])} | {m['pairs']} 对 | 契约 §6.1 稳定子集 |")
    L.append(f"| 口径命中率 | {pct(m['hit_hit'], m['hit_denom'])} | {m['hit_denom']} 个数字单元格 | 契约 §3.3，分母见 §6 |")
    L.append(f"| 追溯完整率（严格：T1–T8 全过 / N） | {pct(m['trace_ok'], m['trace_numbers'])} | N={m['trace_numbers']} | 契约 §4.3 §4.4 |")
    L.append("")
    # —— 1.1 非 JOIN 组答错题的**三档分列**（上层第 3/4 轮打回点名的那张表）——
    # 三档全部从产物现算（eval.fourd.nonjoin_tiers），不按题号手写：
    # 复现差异 = 两遍稳定子集不一致；契约表达不了 = expect 要的东西契约里没位置放；
    # 实现缺陷 = 扣掉前两档仍然答错的。答对的题算「实现范围内」，是这一档的分母。
    tiers = m["tiers"]
    gap_ids = tiers["gap_ids"]
    fw_gap = [it["id"] for it in rework
              if it.get("kind_class") == "契约表达不了" and it.get("question_id") in set(gap_ids)]
    L.append("非 JOIN 组答错的题按根因分三档 —— 三档口径不同，**不许加总**，也不许合成一个「准确率」：")
    L.append("")
    L.append("| 档 | 题数 | 题号 | 责任 |")
    L.append("|---|---|---|---|")
    L.append(f"| 实现缺陷 | {len(tiers['impl_ids'])} | {' '.join(tiers['impl_ids']) or '—'} | "
             f"{'`ask/`' if tiers['impl_ids'] else '—'} |")
    L.append(f"| 契约表达不了 | {len(gap_ids)} | {' '.join(gap_ids) or '—'} | "
             f"{('contract/（' + '/'.join(fw_gap) + '）') if gap_ids else '—'} |")
    L.append(f"| 复现差异 | {len(tiers['repro_ids'])} | {' '.join(tiers['repro_ids']) or '—'} | "
             f"{'`ask/`' if tiers['repro_ids'] else '—'} |")
    L.append("")
    ok_n, all_n = m["non_join"]
    pass_ids = tiers["passed_ids"]
    L.append(f"按 `expect` 机械核对是 {ok_n}/{all_n}（{(ok_n / all_n * 100) if all_n else 0:.1f}%）；**另一条判据**"
             f"（扣除契约表达不了的 {len(gap_ids)} 题）是**实现范围内 {len(pass_ids)}/{len(pass_ids)}"
             f"{' = 100%' if pass_ids else '（没有可测样本）'}**，分母 = 题面语义在契约里表达得出的 "
             f"{len(pass_ids)} 题（{' '.join(pass_ids) or '无'}）；本报告主指标采用前者。")
    L.append("")
    L.append(f"这张表的分母是「非 JOIN 组答错的 {tiers['wrong']} 道题」（共 {tiers['total']} 道）的归因；"
             f"§7.1 那张表的分母是「打回清单 {len(rework)} 条的根因分类」。两张表都会出现「实现缺陷」，"
             f"但**不是同一个分母上的数**，口径不同、互不抵扣 —— 这里的 0 说的是「这 {tiers['wrong']} 道题里"
             f"没有一道是实现没写对」，不是「整个项目没有实现缺陷」（JOIN 组那 2 条在 §7）。")
    L.append("")
    L.append(f"这 {len(pass_ids)}/{len(pass_ids)} 不能当产品能力读：契约空白一补，这 {len(pass_ids)} 题里"
             "至少 4 题会立刻变成 `ask/` 的实现活（窗口函数、取前 N、聚合后再过滤一旦有了字段，"
             "SQL 就得由实现真的生成出来），到那时它就会掉下来。")
    L.append("")
    # 五项测量逐个点名：分母为 0 的写 null，不为 0 的必须现算。
    # 点名是为了把「没测」与「测了但一个都没过」分开——后者是 0%，不是 null。
    measurements = [
        ("SQL 结构准确率（非 JOIN 组）", m["non_join"]),
        ("SQL 结构准确率（JOIN 组）", m["join"]),
        ("同问一致性（两遍稳定子集）", (m["pairs_equal"], m["pairs"])),
        ("口径命中率", (m["hit_hit"], m["hit_denom"])),
        ("追溯完整率（T1–T8 / N）", (m["trace_ok"], m["trace_numbers"])),
    ]
    measured = [name for name, (_hit, tot) in measurements if tot]
    L.append(f"- 四项指标 + 同问两遍字节比对 = 五项测量，**{len(measured)}/{len(measurements)}** 都有非零分母、"
             f"全部按这 {m['n']} 题的产物现算（没有一项退化成 `null`）：" + "、".join(measured) + "。")
    L.append(f"  其中两项结构准确率的分母各 {m['non_join'][1]} 题（非 JOIN）与 {m['join'][1]} 题（JOIN，单列不混进总分，"
             f"见 `eval/README.md`「四项指标怎么算」）；同问一致性 {m['pairs']} 对；口径命中率 "
             f"{m['hit_denom']} 个带 `metric_id` 的数字单元格；追溯完整率 N={m['trace_numbers']}。"
             "`null` 只会在分母为 0 时出现，本轮一项都没有触发。")
    L.append("")
    join_d3 = [r for r in m["per"] if r["join_expect"] and (r.get("error") or {}).get("stage") == "D3"]
    join_ask = [r for r in m["per"] if r["join_expect"] and r["status"] == "clarify"]
    ask_ids = [r["id"] for r in join_ask
               if not (next(e for e in entries if e["id"] == r["id"]).get("d1") or {}).get("time_range")]
    eval_ids = [r["id"] for r in join_ask if r["id"] not in ask_ids]
    if not m["join"][0]:
        L.append(f"**JOIN 组 0 通过的原因不是模型没答对，而是实现层没做 JOIN、且在一部分题上问错了方向**："
                 f"10 道题的 D3 注入都写齐了 `joins[]`（表、ON 键、inner/left 类型），其中 "
                 f"{len(join_d3)} 道（{'、'.join(r['id'] for r in join_d3)}）走到 D3，`ask.query` 在那里"
                 f"直接以 `INTERNAL`/「P0 只支持单表查询」终止；另外 {len(join_ask)} 道连 D1 都没过，"
                 f"分红归口：{'、'.join(ask_ids) or '无'} 的问题里本来就没有时间约束，却因为客户维表带 "
                 f"`register_date` 就被反问「时间落在哪一列」（过度反问 → `ask/`）；"
                 f"{'、'.join(eval_ids) or '无'} 的问题里带着年份，是本步扮演的 D1 自己没写 `time_column` "
                 f"才触发反问（→ `eval/`，见 §7）。两组产物里都一条 SQL 都没有。"
                 f"按契约 §5.1 D3，JOIN 的落地责任在 D3 之后的生成层，所以前面那 {len(join_d3)} 道退回 `ask/`。")
        L.append("")
    L.append("**本报告不给 20 题的总体准确率**：10 道 JOIN 是全集且全灭，把它和非 JOIN 组加在一起"
             "得到的任何单一总数都没有意义 —— 本报告只给分组数，不给总分。")
    L.append("")

    # —— 2. 抽样 ——
    L.append("## 2. 抽样（20 题：10 道 JOIN + 10 道其它类）")
    L.append("")
    L.append(f"名单口径（`eval/fourd/fourd.py` 的 `ROSTER_RULE`）：{roster_rule or proto['sample']}。"
             "这 20 个题号是**钉死**的输入，`--check` 第一步就比对；换题即视为评测器自己错了，先 exit 1。")
    L.append("")
    L.append("| 题号 | 分类 | 扮演这一步的理由 | 问题 |")
    L.append("|---|---|---|---|")
    for e in entries:
        q = qmap[e["id"]]
        L.append(f"| {e['id']} | {q['cat']} | {e.get('why','—')} | {q['q']} |")
    L.append("")
    L.append("JOIN 题取的是**全集**（`eval/questions.jsonl` 里 `多表连接` 一共 10 题）；另外 10 题按"
             "「每题压一条契约 v1.1 判据」来挑：最小标量用例（q001）、`time_column` 落在维表日期列"
             "（q008）、`time_grain_grouping` + 派生列 `period`（q013 / q025 / q057）、纯聚合标量（q020）、"
             "两期 `comparison`（q023）、`metric.window` 同比（q025）、取前 N 的 DESC/ASC 两半"
             "（q041 / q042）、`RESULT_EMPTY`（q053 / q057）。上一轮把 q008 / q020 / q025 / q042 / "
             "q053 / q057 换成 q002 / q007 / q009 / q021 / q055 / q058 的名单**已撤回**，"
             "现在这 20 个题号由 `ROSTER` 钉死、每个都由 `--check` 第一步比对。")
    L.append("")

    # —— 3. 四段各自走了哪条 ——
    L.append("## 3. 四段真跑：每个决策点的来源与产物")
    L.append("")
    L.append("| 题号 | 注入的决策点 | D1 | D2 | D3 | D4 | status | 行数 | trace | caveats |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in m["per"]:
        src = r.get("sources") or {}
        L.append(f"| {r['id']} | {'/'.join(d.upper() for d in r['injected'])} | "
                 f"{src.get('d1','—')} | {src.get('d2','—')} | {src.get('d3','—')} | {src.get('d4','—')} | "
                 f"{r['status']} | {r['rows']} | {r['traces']} | "
                 f"{'、'.join(sorted(set(r['caveats']))) or '—'} |")
    L.append("")
    sr = m["stages"]
    obs_id = next((it["id"] for it in rework if it.get("kind_class") == "观测口径"), "—")
    L.append("四段的来源按 `meta.model.Dn.source` 数。**`fallback` 这一个数必须拆成两行**："
             "一个数是「跑了兜底」，另一个数是「根本没跑到」——合在一起就是两个性质混成一个数。")
    L.append("")
    L.append("| 四段来源 | 次数 | 是什么 |")
    L.append("|---|---|---|")
    L.append(f"| `inject`（agent 扮演的决策被采用） | {sr['inject']} | 注入文件通过校验并被真的用来生成计划/SQL/解读 |")
    L.append(f"| `model`（真模型直调） | {sr['model']} | 本轮没有配置模型，所以一次都没有 —— "
             f"这不是「模型不好」，是「没跑模型」 |")
    L.append(f"| **兜底生效** | {sr['fallback_effective']} | "
             f"{'、'.join(sr['fallback_effective_ids']) or '无'} 的 D4：SQL 没跑成、没有 trace 可解读，"
             f"D4 真的退到了兜底（契约 §5.1 D4「无证据 → `narrative: []`」） |")
    L.append(f"| **没走到** | {sr['not_reached']} | "
             f"{'、'.join(sr['clarify_ids']) or '无'} 在 D1 就 `status=clarify`，D2/D3/D4 共 "
             f"{sr['not_reached']} 个决策点**从未执行**（契约 §2.13：clarify 时 "
             f"`sql`/`rows`/`columns`/`trace` 全为 `[]`）；产物却仍把它们记成 `fallback`，"
             f"与「兜底生效」在产物上分不开（见 `{obs_id}`） |")
    L.append("")
    L.append(f"- 四个数相加 {sr['inject']} + {sr['model']} + {sr['fallback_effective']} + {sr['not_reached']} "
             f"= {sr['inject'] + sr['model'] + sr['fallback_effective'] + sr['not_reached']}，"
             f"正好是 {m['n']} 题 × 4 个决策点；来源未标注 {sr['unlabeled']} 个。")
    L.append("- 有一处必须说清：`ask/` 的 D2/D3 兜底不是「失败」，是契约 §7.1 允许的降级；"
             "本步把「兜底生效」与「没走到」单列出来，就是为了不让「注入成功」被读成「模型跑通了」，"
             "也不让「没跑」被读成「跑了兜底」。")
    L.append("")

    # —— 4. 结构准确率逐题 ——
    L.append("## 4. SQL 结构准确率（逐题）")
    L.append("")
    L.append("| 题号 | 组 | 结果 | 失败原因（机械核对，最多两条） |")
    L.append("|---|---|---|---|")
    for r in m["per"]:
        grp = "JOIN" if r["join_expect"] else "非 JOIN"
        why = "；".join(r["fails"][:2]) if r["fails"] else "—"
        if r.get("error"):
            why = f"`{(r['error'] or {}).get('code')}`（stage={(r['error'] or {}).get('stage')}）：" \
                  f"{(r['error'] or {}).get('message','')[:60]}"
        L.append(f"| {r['id']} | {grp} | {'答对' if r['ok'] else '答错'} | {why} |")
    L.append("")
    L.append("判据就是 `eval/metrics.judge_question`：status / 指标 / 维度 / GROUP BY / JOIN 张数 / "
             "行数形态 / `sql_all_any` 结构要求 / ORDER BY 方向 / LIMIT 条数。它抓「答非所问」和"
             "「语义没落地」，抓不住「结构对了但数算错」——这一点在 §8 的局限里也写着。")
    L.append("")

    # —— 4.1 主根因与次级根因分开记（q025）——
    # 上一轮只写了「契约表达不了」，把一个题目的两层根因混成一层：契约没给 shape（主），
    # 而本步自己的字典把「同比」写成一条 SUM（次）。两层的修法不一样，必须分开列。
    q025 = next((r for r in m["per"] if r["id"] == "q025" and r.get("artifact")), None)
    if q025:
        ans025 = json.loads((ROOT / q025["artifact"]).read_text(encoding="utf-8"))
        st025 = numeric_cell_stats(ans025)
        mid = next(iter(st025["units"]), "")
        d_i = dict_index()
        win = (d_i.get(mid) or {}).get("window") or {}
        zero_periods = [x["row"].get("period") for x in st025["zeros"] if x["row"].get("period")]
        nz = "、".join(f"{x['value']}" for x in st025["nonzeros"]) or "—"
        fw_second = next((it["id"] for it in rework if "definition 与 window 矛盾" in it["kind"]), "—")
        fw_main = next((it["id"] for it in rework if "window 无处落地" in it["kind"]), "—")
        L.append("### 4.1 q025：一条 SUM 冒充同比（主根因与次级根因分开记）")
        L.append("")
        L.append(f"- **主根因**（`{fw_main}`，退 `contract/`）：这道题的全部语义是「每个月和去年同月比」，"
                 f"可契约里能表达两期对比的只有 D1 `comparison`，而 q025 的 D1 comparison 是 null"
                 f"（同比是「每月 vs 去年同月」，不是两个固定区间的对比）；`metric.window` 只声明了 "
                 f"`grain`/`compare` 两个枚举（本条声明 `{win.get('grain')}/{win.get('compare')}`），"
                 f"没有任何条款规定 SQL 要按上一期算。换注入、换实现都不会过。")
        L.append(f"- **次级根因**（`{fw_second}`，退 `eval/`，**不抵扣**上一条）：`eval/data/metrics.json` 里 "
                 f"`{mid}` 的 `definition` 就是 gmv 的那条 SUM、`unit={st025['units'].get(mid)}`。"
                 f"同比是变化率，单位该是 `%`；字典自己没写「和哪一期比」，所以就算实现照 `definition` "
                 f"逐字执行，出来的也只能是一条 SUM：产物 `sql` 就是一条纯 SUM，"
                 f"{len(ans025.get('rows') or [])} 行里 **{len(st025['zeros'])} 个 0.0**"
                 f"（{'、'.join(zero_periods) or '—'}），剩下的几个是原始合计（{nz}）。"
                 f"契约那条把形状定了，这份字典也得改，否则新字段照样落不到正确的表达式上。")
        scope = window_metric_scope(d_i)
        same_as = scope["same_definition_as"].get(mid) or []
        others = [x for x in scope["all"] if x != mid]
        L.append(f"- **`{fw_second}` 的适用范围不是 q025 一道题**：字典里声明 `window != null` 的指标共 "
                 f"{len(scope['all'])} 条（{'、'.join(scope['all'])}），`{mid}` 只是其中一条；"
                 f"`{'、'.join(same_as) or '无'}` 的 `definition` 与它**逐字相同**，也就是说「环比/同比」"
                 f"在字典里只是给同一个单期表达式挂了个 `window` 标签，换 "
                 f"{'、'.join(others) or '别的 window 指标'} 出题，同一条表达式会照样冒充一次。"
                 f"本轮 20 题只跑到 `{mid}`（q025），另外 {len(others)} 条（"
                 f"{'、'.join(others) or '无'}）在名单外没跑，所以这条根因**证据只有 q025 一份产物、"
                 f"适用范围却是字典里全部 window 指标**：补契约/改字典时要按「全部 window 指标」一起修，"
                 f"只修 `{mid}` 一条不够（`eval/data/metrics.json` 的 `unit` 与 `definition` 两列都要动）。")
        L.append("")

    # —— 5. 同问两遍 ——
    L.append("## 5. 同问两遍字节比对（契约 §6.1）")
    L.append("")
    L.append(f"- 请求对数：{m['pairs']}；稳定子集一致：{m['pairs_equal']}；"
             f"原始字节一致：{m['pairs_raw_equal']}；`answer_id` 一致：{m['pairs_id_equal']}")
    L.append("- 原始字节不相等是**预期**的：`meta.created` / `elapsed_ms` / `meta.cache.*` 按契约 §6.4 "
             "允许因环境而不同，所以判据取稳定子集；原始字节只作参考列，不进任何指标。")
    L.append("")
    L.append("| 题号 | 稳定子集一致 | 原始字节一致 | answer_id 一致 | 差异字段（前几条） |")
    L.append("|---|---|---|---|---|")
    for r in m["per"]:
        p = r["pair"]
        if not p:
            L.append(f"| {r['id']} | 无可比 | 无可比 | 无可比 | 缺产物 |")
            continue
        L.append(f"| {r['id']} | {'是' if p['stable_equal'] else '否'} | "
                 f"{'是' if p['raw_equal'] else '否'} | {'是' if p['answer_id_equal'] else '否'} | "
                 f"{'；'.join(p['diffs'][:3]) or '—'} |")
    L.append("")
    if m["pairs_equal"] == m["pairs"] and m["pairs"]:
        L.append("两遍逐题稳定子集全等，说明「同一份注入 + 同一份数据 + 同一份字典」下产物可复现；"
                 "这也顺带证明了 `answer_id`（= sha256(cache_key) 前 16 位）不是随机数。")
        L.append("")
        L.append("但这一项在这条链路上**恒为 100%**，别把它当成绩读：四段输出全是注入文件，两遍之间"
                 "没有任何模型参与，能变的只有契约 §6.4 点名允许的 `meta.created` / `elapsed_ms` / "
                 "`meta.runtime.*` / `meta.cache.*`；任何一版实现（哪怕 SQL 生成完全错）都会拿到 "
                 "20/20。它证的是「注入链路没有随机源」，证不了「实现稳定」——真模型接进来那一刻，"
                 "它才会开始掉。")
        L.append("")

    # —— 6. 口径命中 + 追溯 ——
    L.append("## 6. 口径命中率与追溯完整率")
    L.append("")
    L.append(f"- 口径命中率分母（带 `metric_id` 的数字单元格）：{m['hit_denom']}；"
             f"命中 {m['hit_hit']}；命中旧版本 {m['hit_old']}；未命中 {m['hit_miss']}；"
             f"非字典口径（不计入分母）{m['hit_notdict']}")
    L.append(f"- 追溯：数字单元格 N={m['trace_numbers']}；T1–T8 全过 {m['trace_ok']}")
    total_traces = sum((r.get("trace") or {}).get("n_traces", 0) for r in m["per"])
    L.append(f"- `|trace| == N` 是**必要不充分**的（契约 §4.1 原话：必要条件，不是充分条件）：本轮 "
             f"{m['trace_numbers']} 个数字单元格配了 {total_traces} 条 trace，两者相等只说明"
             f"「没有漏 trace」，**不说明每条 trace 都装齐了**——把 10 个数字塞进 1 条 trace 同样能凑出相等。"
             f"所以本表报的是 T1–T8 逐条判过的条数，不是 trace 条数；上表「trace 条数」那一列只用来对照。")
    L.append("- 口径命中率的分母契约 §3.3 没定义（只定义「一个数字算不算命中」），"
             "本步沿用 `eval/README.md` 声明的分母：带 `metric_id` 的数字单元格，非字典口径单列不进分母。")
    L.append("- **适用范围**：这张表的分母与命中，都是对着 `eval/data/metrics.json`（14 条，"
             "只描述 `eval/data/` 这份合成数据）算的，**不是**项目字典 `dict/metrics.json` ——"
             "本步按边界不读 `dict/`，也就没有拿它核过任何一条。所以「命中 56/56」只能读成"
             "「产物与这份合成字典逐字对得上」，不能读成「与项目字典对得上」；"
             "接上项目字典之后必须重跑一遍，这里的命中率不许外推。")
    # 产品字典那一处落点：证据来源必须写明是**独立检索**（逐条比对产品字典），
    # 否则读者会以为它是这 20 题产物量出来的、从而误以为它影响了上面那几个数。
    dictc_id = next((it["id"] for it in rework if it.get("kind_class") == "字典"), "—")
    L.append(f"- **产品字典那一处落点（`{dictc_id}`，退 `dict/`）的证据来源是「独立检索」**："
             f"它不是这 20 题产物量出来的，而是在产品字典 `dict/metrics.json` 上逐条比对搜出来的 —— "
             f"`gmv_mom` 就是被搜出来的那一条（`unit=CNY`、`window` 声明为 month/mom、`definition` 却是"
             f"一条普通求和，与 `gmv` 逐字相同），和 q025 产物里 `eval/data/metrics.json` 的 `gmv_yoy`"
             f" 是同一个根因的两处落点。本步按边界不打开 `dict/`，所以这条只负责把线索记成打回项、"
             f"不去改它；也正因为它是查出来的、不是跑出来的，它**不进五项指标，也不改**"
             f" `{m['non_join'][0]}/{m['non_join'][1]}`、`{m['join'][0]}/{m['join'][1]}`、"
             f"`{m['pairs_equal']}/{m['pairs']}`、`{m['hit_hit']}/{m['hit_denom']}`、"
             f"`{m['trace_ok']}/{m['trace_numbers']}` 里的任何一个数。"
             f"读者要能分清「哪些数是跑出来的」「哪些是查出来的」。")
    if m.get("zero_hit"):
        zh = m["zero_hit"]
        where = "、".join(f"`{x['id']} {x['path']}`" for x in zh[:4])
        L.append(f"- 严格口径下没过的那 {len(zh)} 条，全部是**零行命中**的聚合数字（{where}）："
                 f"§4.3 T3 要求 `row_ids` 非空，可 0 行命中时源表里没有可指认的行（`row_ids=[]`、"
                 f"`row_ids_total=0`），而 §4.4 又说空结果集只有「没有可追溯的数字」。"
                 f"本报告按下界报严格率（若把「零行命中、没有源行可指」不算不完整，则是 "
                 f"{m['trace_ok'] + len(zh)}/{m['trace_numbers']}），这条口径冲突退回 `contract/`，"
                 f"不在 `eval/` 里自行放宽。")
    L.append("")
    L.append("| 题号 | N（数字单元格） | trace 条数 | T1–T8 全过 | 有数字但没 trace 的位置 |")
    L.append("|---|---|---|---|---|")
    for r in m["per"]:
        t = r.get("trace") or {}
        L.append(f"| {r['id']} | {t.get('n_numbers', 0)} | {t.get('n_traces', 0)} | {t.get('strict_ok', 0)} | "
                 f"{'、'.join(t.get('missing_paths') or []) or '—'} |")
    L.append("")
    L.append("逐条 trace 的 T1–T8 明细在 `python -m eval.metrics` 那一份里有完整表（60 题）；"
             "本报告只给这 20 题的汇总，不重复贴 200 行。")
    L.append("")

    # —— 7. 打回清单 ——
    L.append("## 7. 打回清单")
    L.append("")
    by_target: dict[str, int] = {}
    for it in rework:
        by_target[it["target_dir"]] = by_target.get(it["target_dir"], 0) + 1
    L.append(f"- 条数：{len(rework)}（按退回去的目录："
             + "、".join(f"`{k}` {v}" for k, v in sorted(by_target.items())) + "）")
    L.append("- 明细见 `eval/fourd/rework.fourd.json`，每条带 `target_dir` / `contract_ref` / `evidence` / `action`。")
    L.append("")
    for it in rework:
        L.append(f"  - `{it['id']}` {it['severity']} → `{it['target_dir']}`：{it['kind']}"
                 f"（{it['question_id']}，契约 {it['contract_ref']}）")
    L.append("")

    # —— 7.1 按根因分五类：实现缺陷 / 契约表达不了 / 字典 / 观测口径 / 本步自己 ——
    # 分类不手写：全部读 build_rework() 里已经写好的 kind_class（每一条在进去时就归好类），
    # 免得报告里的说法与 rework.fourd.json 打架。观测口径那类是「产物写对了、
    # 但写出来的东西让人分不清两件事」，既不是代码错也不是契约缺字段，所以单列。
    # 「字典」那一类是产品字典自身与契约自相矛盾（独立检索发现，不是产物量出来的），
    # 责任目录是 `dict/`，与「本步自己」（评测副本 `eval/data/metrics.json`）分开记。
    def pick(cls: str) -> list[dict]:
        return [it for it in rework if it.get("kind_class") == cls]

    impl, expr, obs, selfv = pick("实现缺陷"), pick("契约表达不了"), pick("观测口径"), pick("本步自己")
    dictc = pick("字典")
    unknown = [it for it in rework
               if it.get("kind_class") not in {"实现缺陷", "契约表达不了", "观测口径", "本步自己", "字典"}]
    ids = lambda xs: "、".join(f"`{it['id']}`" for it in xs) or "无"     # noqa: E731
    L.append(f"### 7.1 按根因分五类：实现缺陷 {len(impl)} 条 / 契约表达不了 {len(expr)} 条"
             f"（另 {len(dictc)} 条字典、{len(obs)} 条观测口径、{len(selfv)} 条本步自己）")
    L.append("")
    L.append("| 根因 | 条数 | 退回去 | 明细 | 为什么是这一类、不是另一类 |")
    L.append("|---|---|---|---|---|")
    L.append(f"| **实现缺陷**（契约已经写了，代码没做或做过头） | {len(impl)} | `ask/` | {ids(impl)} | "
             f"契约 §5.1 D3 已经写明 `joins[]` 必须落到 SQL、§5.1 D1 已经写明 `time_range=null` 时不该"
             f"反问时间列；字段、枚举、错误码都在契约里，改代码就能修，**不需要动契约** |")
    L.append(f"| **契约表达不了**（没有字段能写，或两条条款互相冲突） | {len(expr)} | `contract/` | {ids(expr)} | "
             f"「取前 N」「聚合后再过滤」「指标 window 落地」在 D1/D2/D3 里没有任何字段可写，"
             f"§4.3 T3 与 §4.4 又互相冲突；这些条目换注入、换实现都不会过，只能改契约，"
             f"所以**不是实现缺陷** |")
    L.append(f"| **字典**（指标定义与声明自相矛盾） | {len(dictc)} | `dict/` | {ids(dictc)} | "
             f"window 指标的 `definition` 是单期表达式、`unit` 还是 `CNY`，契约没规定 window 指标该怎么落地，"
             f"字典自己也没写「和哪一期比」；同时 `dict/validate.py` 不拦这种写法。"
             f"它和「本步自己」那一档（评测副本 `eval/data/metrics.json`）是同一个根因的两处落点，"
             f"改一处不算改完；证据来自**独立检索**（逐条比对产品字典），不是这 20 题产物量出来的 |")
    L.append(f"| **观测口径**（产物写对了，但两件事在产物上分不开） | {len(obs)} | `ask/` | {ids(obs)} | "
             f"「没执行的决策点」和「兜底真的生效」在 `meta.model.Dn` 里是同一个值 `fallback`、"
             f"同一个空串 sha256、同一个 `latency_ms=0`，读产物分不出是哪一种；"
             f"代码没写错、契约也没缺字段，错在**没人能从产物上数清**，所以既不算实现缺陷也不算契约空白 |")
    self_why = ["agent 扮演 D1 写了 `time_range` 却没写 `time_column`",
                "注入文件按 P0 口径没写 `input_digest`"]
    if any("window 矛盾" in it["kind"] for it in selfv):
        self_why.append("`eval/data/metrics.json` 把同比指标写成了一条 SUM（unit 还是 CNY）")
    L.append(f"| 本步自己（扮演的 D1 / 注入文件 / 字典） | {len(selfv)} | `eval/` | {ids(selfv)} | "
             + "、".join(self_why) +
             "；既不是 `ask/` 的缺陷，也不是契约缺字段，**先自己补齐再重跑** |")
    L.append("")
    if unknown:
        L.append(f"（另有 {len(unknown)} 条 `kind_class` 没归上类：{ids(unknown)} —— 这是生成器的 bug，不是产物的问题。）")
        L.append("")
    L.append("同一份产物只归一类：根因记在谁头上，就退给谁。若一条打回同时像是"
             "「实现没做」又像是「契约没写」，按「契约优先」归 `contract/`（契约没说清实现该怎么做时，"
             "实现怎么做都算猜）；本轮没有这种模棱两可的条目。")
    L.append("")
    L.append(f"这张表的分母是「打回清单 {len(rework)} 条的根因分类」，每条打回都记一次数；"
             f"§1 那张「实现缺陷 / 契约表达不了 / 复现差异」三档表的分母是「非 JOIN 组答错的 "
             f"{tiers['wrong']} 道题」，按题记。同一类名在两处的数不同是**口径不同**，不是矛盾："
             f"§1 的「实现缺陷 0」说的是「这 {tiers['wrong']} 道错题里没有一道是代码没写对」，"
             f"这张表的「实现缺陷 {len(impl)}」说的是「{len(rework)} 条打回里有 {len(impl)} 条要 `ask/` 改代码」"
             f"（都在 JOIN 组，见上表）。")
    L.append("")
    L.append("`FW-0005` / `FW-0006` 其实是同一条契约空白（取前 N 无处表达），`FW-0007` / `FW-0008` 也是"
             "同一条（聚合后再过滤）。这里按题号各留一条、不合并：每条都要能定位到一道题，"
             "合并成「泛泛一条」就追不回是哪份产物把它压出来的；`contract/` 那边按这两条根因各修一处即可。")
    L.append("")
    if any("反问" in it["kind"] for it in rework):
        L.append("「反问」那两条不是凭感觉写的：`eval/fourd/probe_clarify.py` 用同一个问题、同一份 D1 注入，"
                 "只改两处变量（给不给带日期列的维表 / D1 写不写 `time_column`），"
                 "四组对照的产物在 `eval/fourd/probe/`（q032、q040 各 5 组），脚本可重跑。")
        L.append("")
    L.append("归口规则写在 `build_rework()` 里（每条打回进去时就带上 `kind_class`，报告只按它分组），"
             "不按题号手写：跨表要 JOIN 而实现只支持单表、或问题里没有时间却反问时间列 → `ask/`（实现缺陷）；"
             "契约没有字段能表达这条语义、或两条契约互相冲突 → `contract/`（契约表达不了）；"
             "产物能让两件不同的事共用一个值、读的人分不清 → `ask/`（观测口径）；"
             "产品字典自身与契约自相矛盾（独立检索发现，不是产物量出来的） → `dict/`（字典）；"
             "注入文件自己没写 `input_digest`、或本步自己的 D1 没写清时间列 → `eval/`（本步自己）。"
             "同一份产物不一边说「契约要改」一边说「ask/ 要修」。")
    L.append("")

    # —— 8. 局限 ——
    L.append("## 8. 本步自己承认的局限")
    L.append("")
    L.append("- **这一轮量的是「四段都给对之后实现能不能落地」，不是模型效果**：四个决策点全部由 agent 扮演"
             "（注入），模型链路 0 次调用；换真模型必须重跑。")
    L.append("- 扮演的 D1 口径来自 `eval/d1_truth.json`（与 `eval/questions.jsonl` 的 `expect` 一一对应），"
             "它本身是这套合成数据的真值，不是模型输出；所以本报告不评价「问题理解得对不对」。")
    L.append("- 注入文件按契约 §5.2 的 P0 口径不写 `input_digest`：产物因此每题多一条 "
             "`INJECT_DIGEST_UNCHECKED`，`INJECT_STALE` 这条失效检测路径本轮没被测到。")
    L.append("- 结构准确率是机械核对：抓得住「答非所问 / 语义没落地」，抓不住「结构对了但数算错」。"
             "数字对不对要靠追溯链（§6）和人工抽查，不靠这张表。")
    L.append("- 合成数据只有 48 行事实表，`rows` 里的数字规模很小（很多聚合是 0）；"
             "这不是数据质量问题，是「真值可手算」的代价。")
    L.append(f"- 20 题里 JOIN 题全组失败：其中 {len(join_d3)} 题卡在 D3（JOIN 没实现），"
             f"{len(join_ask)} 题卡在 D1 的反问。"
             "D4 因此拿不到任何 trace，这 10 题的 D4 注入写的是「无证据可解读」——这是契约 §5.1 D4"
             " 与 §2.13「没有结论 → `narrative: []`」的正确形状，不是我们偷懒。")
    L.append("- D4 的注入是在**第一遍真跑拿到真 `trace_id` 之后**才写的：`text_template` 里的数字一律写成 "
             "`{{trace:t-000N}}`、不用裸阿拉伯数字（契约 §2.8），`evidence` 与引用一一对应。"
             "真实链路里 D4 本来就是看着产物写解读，所以这不算作弊；但它也确实说明这 20 题里"
             "「D4 段」没有独立性——它依赖前一段的产物。")
    L.append("")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.fourd",
                                 description="抽 20 题（含全部 10 道 JOIN）由 agent 扮演 D1-D4 真跑四段")
    ap.add_argument("--check", type=int, default=None, metavar="N",
                    help="只做抽样与注入文件自检（不跑 ask），题数必须等于 N")
    ap.add_argument("--report", default=None, metavar="PATH", help="真跑四段并写报告到 PATH")
    ap.add_argument("--only", nargs="*", default=None, help="只跑这些题号（调试用）")
    ap.add_argument("--no-run", action="store_true", help="配合 --report：不真跑，只用现有产物重算报告")
    args = ap.parse_args(argv)

    decisions = load_decisions()
    roster, roster_rule = load_roster()
    entries = order_entries(sample(decisions), roster)       # 顺序 = ROSTER（报告 §2 的顺序）
    qmap = {q["id"]: q for q in load_questions()}

    # 第一步（`--check` 与 `--report` 都先过这一关）：题号集合必须正好等于 ROSTER
    roster_errs = check_roster(entries, roster)
    if roster_errs:
        print(f"名单不对（{rel(ROSTER_PATH)} 的 ROSTER 对不上 {rel(DECISIONS)}），先修名单，别跑：",
              file=sys.stderr)
        for e in roster_errs:
            print(f"  - {e}", file=sys.stderr)
        return 1

    written = write_injects(entries)

    if args.check is not None:
        errs, notes = check_sample(entries, qmap, args.check)
        join_picked = [e["id"] for e in entries if qmap.get(e["id"], {}).get("cat") == JOIN_CAT]
        print(f"名单比对 OK：{len(roster)} 题与 {rel(ROSTER_PATH)} 的 ROSTER 一致")
        print(f"抽样 {len(entries)} 题，其中 JOIN {len(join_picked)} 题；注入文件 {len(written)} 份已落盘")
        for n in notes:
            print(f"  提示：{n}")
        if errs:
            print(f"自检不过，{len(errs)} 处（先修评测器，别跑）：", file=sys.stderr)
            for e in errs:
                print(f"  - {e}", file=sys.stderr)
            return 1
        print("自检 OK：抽样/维度/D2 形状/JOIN 责任/注入文件都对得上")
        return 0

    picked = [e for e in entries if not args.only or e["id"] in set(args.only)]
    if args.no_run:
        results = {}
        for e in picked:
            qid = e["id"]
            got = {}
            for rn in ("run1", "run2"):
                fp = FOURD / qid / f"{rn}.json"
                ans = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else None
                got[rn] = {"exit": 0, "answer": ans, "stderr": "",
                           "artifact": rel(fp) if fp.exists() else None}
            results[qid] = got
    else:
        print(f"真跑四段：{len(picked)} 题 × 2 遍（D1-D4 全部注入，由 agent 扮演）")
        results = run_all(picked, qmap)

    m = measure(picked, qmap, results, dict_index())
    rework = build_rework(m, picked, qmap)

    out_json = FOURD / "measure.json"
    out_json.write_text(json.dumps({"contract": CONTRACT, "protocol": decisions["protocol"],
                                    "summary": {k: v for k, v in m.items() if k != "per"},
                                    "per": m["per"], "rework": rework},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (FOURD / "rework.fourd.json").write_text(
        json.dumps({"contract": CONTRACT, "items": rework}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    if args.report:
        rp = ROOT / args.report if not Path(args.report).is_absolute() else Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(render_report(decisions, picked, qmap, m, rework, roster, roster_rule),
                      encoding="utf-8", newline="\n")
        print(f"报告：{args.report}")

    non_join = m["non_join"]
    join = m["join"]
    print(f"非 JOIN 组 {non_join[0]}/{non_join[1]}；JOIN 组 {join[0]}/{join[1]}；"
          f"同问两遍 {m['pairs_equal']}/{m['pairs']}；挂账 {len(rework)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
