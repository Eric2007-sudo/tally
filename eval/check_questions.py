"""eval.check_questions · 问题集自检

断言里跑的就是这一条：

    python -m eval.check_questions --count 60 --cats 6 --per-cat 10

它只读 eval/questions.jsonl，不碰任何别的目录。规则全部来自
contract/answer.md（intent 六选一、category 与 README「怎么算做成」六类对齐），
以及本步自己定的、写在 eval/README.md 里的期望字段。

退出码：0 = 全过；1 = 有问题（一次性列出全部，不 fail-fast，也不静默跳过）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = EVAL_DIR / "questions.jsonl"

# README「怎么算做成」点名的六类；JOIN 题单独统计，所以「多表连接」是独立一类
CATS = ["筛选", "聚合", "同比环比", "多表连接", "排名", "异常"]
JOIN_CAT = "多表连接"

# contract/answer.md §2.2 plan[].intent，六选一
INTENTS = {"distribution", "trend", "compare", "anomaly", "rank", "detail"}

STATUSES = {"ok", "clarify", "error"}
ROWS_KINDS = {"scalar", "many", "any"}

REQUIRED_TOP = ("id", "cat", "q", "intent", "expect")
REQUIRED_EXPECT = ("status", "metrics", "dims", "group_by", "order_by", "join", "tables", "rows")

ID_RE = re.compile(r"^q\d{3}$")


def load(path: Path) -> tuple[list[dict], list[str]]:
    errs: list[str] = []
    rows: list[dict] = []
    if not path.exists():
        return [], [f"找不到问题集：{path}"]
    raw = path.read_text(encoding="utf-8")
    if raw and not raw.endswith("\n"):
        errs.append("文件末尾缺少换行（要求恰好一个 \\n）")
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            errs.append(f"第 {lineno} 行是空行：JSONL 不许有空行")
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errs.append(f"第 {lineno} 行不是合法 JSON：{exc}")
            continue
        if not isinstance(obj, dict):
            errs.append(f"第 {lineno} 行不是对象")
            continue
        obj["__lineno"] = lineno
        rows.append(obj)
    return rows, errs


def check_item(i: int, q: dict) -> list[str]:
    e: list[str] = []
    where = f"#{i}（id={q.get('id')}）"

    for key in REQUIRED_TOP:
        if key not in q:
            e.append(f"{where} 缺字段 {key}")
    if e:
        return e

    qid = q["id"]
    if not isinstance(qid, str) or not ID_RE.match(qid):
        e.append(f"{where} id 必须是 q + 三位数字")
    elif qid != f"q{i:03d}":
        e.append(f"{where} id 与行序不一致：第 {i} 题应为 q{i:03d}")

    if q["cat"] not in CATS:
        e.append(f"{where} cat={q['cat']!r} 不在六类里：{CATS}")

    text = q["q"]
    if not isinstance(text, str) or not text.strip():
        e.append(f"{where} 问题文本为空")
    else:
        if text != text.strip():
            e.append(f"{where} 问题文本首尾有空白（契约 §2.1 question 要逐字保存）")
        if len(text) < 6:
            e.append(f"{where} 问题文本过短（<6 字），不像一个真问题")
        if not (text.endswith("？") or text.endswith("。")):
            e.append(f"{where} 问题文本应以「？」或「。」结尾")

    if q["intent"] not in INTENTS:
        e.append(f"{where} intent={q['intent']!r} 不在契约 §2.2 的六选一里")

    ex = q["expect"]
    if not isinstance(ex, dict):
        return e + [f"{where} expect 必须是对象"]
    for key in REQUIRED_EXPECT:
        if key not in ex:
            e.append(f"{where} expect 缺字段 {key}")
    if e:
        return e

    if ex["status"] not in STATUSES:
        e.append(f"{where} expect.status={ex['status']!r} 不在 {sorted(STATUSES)} 里")

    for key in ("metrics", "dims", "tables"):
        val = ex[key]
        if not isinstance(val, list) or any(not isinstance(x, str) or not x.strip() for x in val):
            e.append(f"{where} expect.{key} 必须是非空字符串组成的数组")
        elif len(set(val)) != len(val):
            e.append(f"{where} expect.{key} 有重复项")

    for key in ("group_by", "order_by"):
        if ex[key] is not None and not isinstance(ex[key], bool):
            e.append(f"{where} expect.{key} 只能是 true / false / null")

    if not isinstance(ex["join"], bool):
        e.append(f"{where} expect.join 必须是 bool")

    if ex["rows"] not in ROWS_KINDS:
        e.append(f"{where} expect.rows={ex['rows']!r} 不在 {sorted(ROWS_KINDS)} 里")

    # —— 分类与期望的一致性（这些是「问题集本身自洽」的硬要求）——
    if q["cat"] == JOIN_CAT:
        if not ex["join"]:
            e.append(f"{where} 「多表连接」类必须 expect.join=true")
        if len(ex["tables"]) < 2:
            e.append(f"{where} 「多表连接」类必须声明 ≥2 张表，现在 {ex['tables']}")
    else:
        if ex["join"]:
            e.append(f"{where} 非 JOIN 类却 expect.join=true（会把 JOIN 统计混进总分）")

    if q["cat"] == "排名":
        if ex["order_by"] is not True:
            e.append(f"{where} 「排名」类必须 expect.order_by=true（契约 §6.3 排序必须全序）")
        if q["intent"] != "rank":
            e.append(f"{where} 「排名」类 intent 必须为 rank")

    if q["cat"] == "异常":
        if q["intent"] != "anomaly":
            e.append(f"{where} 「异常」类 intent 必须为 anomaly")

    if q["cat"] == "同比环比":
        if not ex["metrics"]:
            e.append(f"{where} 「同比环比」类必须声明期望指标（window 指标）")
        if not ex["tables"]:
            e.append(f"{where} 「同比环比」类必须声明涉及的表")

    if ex["status"] == "ok" and not ex["tables"]:
        e.append(f"{where} 期望能算出数（status=ok）却没声明涉及的表")
    if ex["status"] == "clarify" and ex["join"]:
        e.append(f"{where} 反问体不该声明 JOIN 期望")

    if "gap" in ex and (not isinstance(ex["gap"], str) or len(ex["gap"]) < 8):
        e.append(f"{where} expect.gap 必须是一句话说明契约空白")

    return e


def check_set(rows: list[dict], want_count: int, want_cats: int, want_per_cat: int) -> list[str]:
    e: list[str] = []
    if len(rows) != want_count:
        e.append(f"题量 {len(rows)} != 要求的 {want_count}")

    got_cats = [c for c in CATS if any(r.get("cat") == c for r in rows)]
    unknown = sorted({r.get("cat") for r in rows if r.get("cat") not in CATS} - {None})
    if unknown:
        e.append(f"出现了六类之外的分类：{unknown}")
    if len(got_cats) != want_cats:
        e.append(f"分类数 {len(got_cats)} != 要求的 {want_cats}；实际 {got_cats}")

    for cat in got_cats:
        n = sum(1 for r in rows if r.get("cat") == cat)
        if n != want_per_cat:
            e.append(f"「{cat}」有 {n} 题，要求每类 {want_per_cat} 题")

    ids = [r.get("id") for r in rows]
    dup = sorted({x for x in ids if ids.count(x) > 1})
    if dup:
        e.append(f"id 重复：{dup}")

    seen: dict[str, str] = {}
    for r in rows:
        text = re.sub(r"\s+", "", str(r.get("q", "")))
        if text in seen:
            e.append(f"问题文本重复：{r.get('id')} 与 {seen[text]} 问的是同一句")
        else:
            seen[text] = str(r.get("id"))

    clarify = [r.get("id") for r in rows if r.get("expect", {}).get("status") == "clarify"]
    # 阈值 5（原为 3）：契约 §7.1「metric_ids 为空必须反问」+ §5.1「问题里有时间词就必须进
    # time_range 或 ambiguity」，这两条把「纯明细列举」「时间词落在别的列上」也判成反问。
    # 实测后按真实契约口径放宽，并在 eval/report.md 里点名是哪两条规则导致的。
    if len(clarify) > 5:
        e.append(f"反问题 {len(clarify)} 道（{clarify}），超过 5 道说明问题集偏向「问不出来」")

    join_n = sum(1 for r in rows if r.get("expect", {}).get("join"))
    if join_n != want_per_cat:
        e.append(f"JOIN 题 {join_n} 道，应恰好 {want_per_cat} 道（README：JOIN 题单独统计）")

    return e


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.check_questions", description="问题集自检")
    ap.add_argument("--count", type=int, default=60, help="总题数")
    ap.add_argument("--cats", type=int, default=6, help="分类数")
    ap.add_argument("--per-cat", type=int, default=10, help="每类题数")
    ap.add_argument("--file", default=str(QUESTIONS_PATH), help="问题集路径")
    args = ap.parse_args(argv)

    rows, errs = load(Path(args.file))
    for i, q in enumerate(rows, start=1):
        errs.extend(check_item(i, q))
    errs.extend(check_set(rows, args.count, args.cats, args.per_cat))

    if errs:
        print(f"问题集不合格，共 {len(errs)} 处：", file=sys.stderr)
        for msg in errs:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    print(f"问题集 OK：{len(rows)} 题 / {args.cats} 类 / 每类 {args.per_cat} 题")
    for cat in CATS:
        n = sum(1 for r in rows if r["cat"] == cat)
        tag = "  （JOIN 题，单独统计，不混进总分）" if cat == JOIN_CAT else ""
        print(f"  {cat}\t{n}{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
