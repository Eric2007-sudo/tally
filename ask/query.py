"""ask.query · 提问 → answer.json 全链路（契约 §2 / §5 / §6.5 / §7）

一条命令跑完：读问题 → 读数据（schema 推断）→ D1/D2/D3 → 生成 SQL → 只读静态检查 →
执行 → 追溯链 → D4 → 按 §2.1 字段序装答案 → 按 §6.5 序列化落盘。

```bash
python -m ask.query --question-file ask/fixtures/question.first.txt \
    --decide d1=inject:ask/inject/d1.json --out ask/answer_out/smoke.json
```

**契约没写清、本模块自己定的三条（报告里要报）**
1. **§7.3 的自动回退重试没做**：契约要求静态检查/执行失败后「回退 D3/D2/D1 重来」（上限 3 次）。
   P0 离线（没有模型回调）时，回退 D3 只会拿到同一份兜底输出、生成同一条 SQL，
   重试三轮得到的错误一模一样——那正是 §7.3 明说要刹车避免的 `SQL_RETRY_LOOP`。
   所以本实现**如实记录第 1 次尝试**（`error.attempts` 一条，带 `d3_snapshot`），
   直接报 `SQL_EXEC_ERROR` / `SQL_STATIC_REJECT`，不假装重试过。
2. **`meta.model` 的完整性**：§2.12 要求 D1–D4 各一条，而 `cache_key`（§5.3）也要四个决策点。
   问题停机（`clarify` / `error`）时 D2/D3/D4 根本没跑，没有哈希可填。
   本实现在这些位置填一个**固定占位**：`{"source":"fallback","model":null,"injected":false,
   "latency_ms":0,"output_sha256":sha256("")}`，并只用它算 `cache_key`（不新增字段、不留空串）。
3. **`cache.hit` / `cache.written` 一律 `false`**：P0 没有缓存存储（契约 §2.12 只定义字段，
   没定义放在哪），所以「命中」永远不可能成立。`cache.key` 仍按 §5.3 算出来，
   将来接上缓存时键不用改。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import decide as decide_mod
from .decide import (
    CONTRACT_VERSION,
    DECISIONS,
    DEFAULT_OPTIONS,
    DELTA_SUFFIX,
    PCT_SUFFIX,
    DecisionBundle,
    DecisionError,
    DecisionRunner,
    build_d4_input,
    caveat,
    decide_all,
    merge_caveats,
    normalize_options,
    parse_decide_args,
    sha256_text,
    sort_caveats,
)
from .execute import NUMERIC_TYPES, build_connection, build_traces, check_traces, finalize_sql, run_step
from .ingest import IngestError, read_source
from .metric_dict import load_metric_dict
from .plan_rules import derive as derive_shape
from .schema import Table, tables_for_file
from .sqlgen import build_step_plans
from .static_check import StaticReject, check_sql

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = "ask/fixtures/smoke.xlsx"
DEFAULT_METRIC_DICT = "dict/metrics.json"
MAX_SQL_ATTEMPTS = 3  # §7.3（本实现不做重试，见模块 docstring 第 1 条）


def _shape_for(question: str, d1: dict, metric_dict, runner):
    """D2 走兜底（没有模型）时，把问题语义补成 SQL 能表达的形状（见 `ask.plan_rules`）。

    D2 是**模型输出**时一律不用：排序方向、窗口、异常判据这些本来就该由 D2 决定，
    模型已经给过计划了，这里再加工一遍等于偷偷改它的口径。

    v1.1：D1 的 `time_grain_grouping` / `comparison` 是**真源**（§5.1），
    问题文本里猜出来的粒度/两期对比只作兜底——D1 说了就一律听 D1 的，不许被文本覆盖。
    """
    result = runner.results.get("D2")
    if result is None or getattr(result, "source", None) != "fallback":
        return None
    shape = derive_shape(
        question,
        d1,
        metric_dict,
        list(d1.get("metric_ids") or []),
        list(d1.get("dimensions") or []),
    )
    if d1.get("time_grain_grouping"):
        shape.grain = None
    if d1.get("comparison"):
        shape.grain = None
        shape.compare = None
        shape.window_compare = None
    return shape

# 没跑过的决策点用这个固定占位（见模块 docstring 第 2 条）
_NEVER_RAN = {
    "source": "fallback",
    "model": None,
    "injected": False,
    "latency_ms": 0,
    "output_sha256": sha256_text(""),
}


# --------------------------------------------------------------------------- #
# 序列化（§6.5）
# --------------------------------------------------------------------------- #


def _scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")  # 定点，禁科学计数法（§6.2）
    if isinstance(value, float):
        raise TypeError(f"answer.json 里不许出现浮点：{value!r}（§6.2 一律 Decimal）")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"不认识的标量类型：{type(value).__name__}")


def dump(obj, level: int = 0) -> str:
    """§6.5：2 空格缩进、`ensure_ascii=False`、键序 = 调用方 dict 的插入顺序、末尾恰好一个 `\\n`。"""
    pad = "  " * (level + 1)
    end = "  " * level
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = [f"{pad}{json.dumps(str(k), ensure_ascii=False)}: {dump(v, level + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + end + "}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        items = [f"{pad}{dump(v, level + 1)}" for v in obj]
        return "[\n" + ",\n".join(items) + "\n" + end + "]"
    return _scalar(obj)


def render(document: dict) -> str:
    return dump(document) + "\n"


# --------------------------------------------------------------------------- #
# 输入准备
# --------------------------------------------------------------------------- #


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass


def load_question(question: str | None, question_file: str | None) -> str:
    """§2.1：`question` 逐字保存，不清洗（归一化只发生在缓存键里）。"""
    if question_file:
        text = Path(question_file).read_text(encoding="utf-8")
        text = text.rstrip("\n")  # 文件末尾的换行不算问题的一部分
        if not text.strip():
            raise DecisionError("INTERNAL", f"问题文件是空的: {question_file}", stage="ingest")
        return text
    if question is None or not question.strip():
        raise DecisionError(
            "INTERNAL",
            "没有提问：要么 --question，要么 --question-file",
            stage="ingest",
            readable_hint="命令行里没给问题",
        )
    return question


def load_tables(paths) -> tuple:
    """读数据 → `(tables_by_name, files, sheets, schema, pool)`。

    物理表名 = sheet 名（CSV 用文件主名），与 `decide.fallback_d3` 的命名保持一致。
    极少数情况下两个文件的 sheet 同名 → 后者加 `#2` 后缀（不静默覆盖）。

    `pool`（#12 新增）= 与 `--data` **同目录**的其它数据文件里的表：只在跨表 JOIN 找不到
    直接键时，用来做一跳桥接（如 q039 的 `sales → product_dim → brand_dim`）。
    它**不进** `schema`（不参与 D1/D2/D3 的输入），也不进 `meta.files`，除非真的被用上。
    """
    tables_by_name: dict = {}
    files: list = []
    sheets: list = []
    order: list = []
    for path in paths:
        file_data = read_source(path, root=PROJECT_ROOT)
        files.append({"path": file_data.path, "sha256": file_data.sha256})
        for table in tables_for_file(file_data):
            name = table.sheet or Path(table.file).stem
            unique = name
            suffix = 2
            while unique in tables_by_name:
                unique = f"{name}#{suffix}"
                suffix += 1
            tables_by_name[unique] = table
            order.append(unique)
            if table.sheet:
                sheets.append(table.sheet)
    files.sort(key=lambda item: item["path"].encode("utf-8"))
    schema = [tables_by_name[name].as_schema_dict() for name in order]
    return tables_by_name, files, sorted(set(sheets), key=lambda s: s.encode("utf-8")), schema, _sibling_pool(paths, tables_by_name)


def _sibling_pool(paths, declared: dict) -> dict:
    """同目录其它数据文件里的表（只作桥接候选，按路径字节序）。读不了就跳过，不因为邻居坏掉而失败。"""
    seen: set = set()
    pool: dict = {}
    for path in paths:
        folder = (PROJECT_ROOT / str(path)).parent if not Path(path).is_absolute() else Path(path).parent
        for candidate in sorted(folder.glob("*"), key=lambda p: p.as_posix().encode("utf-8")):
            if candidate.is_dir() or candidate.suffix.lower() not in (".csv", ".xlsx"):
                continue
            key = candidate.resolve().as_posix()
            if key in seen:
                continue
            seen.add(key)
            try:
                file_data = read_source(str(candidate), root=PROJECT_ROOT)
                tables = tables_for_file(file_data)
            except (IngestError, OSError):
                continue
            for table in tables:
                name = table.sheet or Path(table.file).stem
                if name in declared or name in pool:
                    continue
                pool[name] = table
    return pool


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #


def _meta_model(runner: DecisionRunner) -> dict:
    """§2.12：D1–D4 各一条，没跑过的用固定占位（见模块 docstring 第 2 条）。"""
    ran = runner.meta_model()
    return {name: ran.get(name, dict(_NEVER_RAN)) for name in DECISIONS}


def _cache_key(bundle: DecisionBundle, meta_model: dict, options: dict) -> str:
    part = {
        name: {"source": entry["source"], "output_sha256": entry["output_sha256"]}
        for name, entry in meta_model.items()
    }
    return decide_mod.cache_key(
        question=bundle.question,
        files=bundle.files,
        sheets=bundle.sheets,
        metric_dict_sha256=bundle.metric_dict_sha256,
        decisions=part,
        options=options,
    )


def _meta(
    *,
    started: float,
    meta_model: dict,
    key: str,
    files,
    metric_dict_sha256: str,
    options: dict,
) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "model": meta_model,
        "cache": {"hit": False, "key": key, "source_answer_id": None, "written": False},
        "files": list(files),
        "metric_dict_sha256": metric_dict_sha256,
        "runtime": {
            "sqlite": sqlite3.sqlite_version,
            "python": platform.python_version(),
            "os": sys.platform,
        },
        "options": dict(options),
    }


# --------------------------------------------------------------------------- #
# 各状态的答案体
# --------------------------------------------------------------------------- #


def _body(**fields) -> dict:
    """§2.1 的字段顺序：一个字都不许多插，也不许缺。"""
    skeleton = {
        "question": None,
        "answer_id": None,
        "status": None,
        "plan": [],
        "plan_summary": "",
        "metrics_used": [],
        "sql": [],
        "columns": [],
        "rows": [],
        "trace": [],
        "charts": [],
        "narrative": [],
        "caveats": [],
        "clarify": None,
        "error": None,
        "meta": {},
    }
    unknown = set(fields) - set(skeleton)
    if unknown:
        raise DecisionError("INTERNAL", f"答案体里出现了契约没有的字段 {sorted(unknown)}", stage="export")
    skeleton.update(fields)
    missing = [k for k, v in skeleton.items() if v is None and k not in ("clarify", "error", "answer_id")]
    if missing:
        raise DecisionError("INTERNAL", f"答案体缺字段 {missing}", stage="export")
    return skeleton


def _clarify_metrics(d1: dict, metric_dict, route) -> list:
    """§2.13：反问时 `metrics_used` 只许放**已经唯一确定**的指标。"""
    unknown = set(route.unknown_metric_ids)
    ids = [mid for mid in d1.get("metric_ids") or [] if mid not in unknown and metric_dict.get(mid)]
    return metric_dict.snapshots(ids)


def _comparison_caveats(result, d1: dict, columns: list) -> list:
    """§2.9 v1.1：两期对比里两种「写 null」的格子要分开记账。

    - `COMPARISON_KEY_MISSING`：某个键只在其中一期出现 ⟹ 缺失侧写 `null`（**不许补 0**），
      该行的 `__delta` / `__pct` 也是 `null`；`detail` 要说清缺哪一期、缺了几个键。
    - `PCT_BASE_ZERO`：基期有值但是 0 ⟹ `__pct` 写 `null`（不许写 0% / ∞）。

    判据只看输出列（对比期的原值不进 `columns[]`——§2.4 的派生列是封闭三种）：
    `<指标>` 为 `null` ⟹ 基期没有这个键；`<指标>` 有值而 `__delta` 为 `null` ⟹ 对比期没有这个键。
    """
    pct_names = [col["name"] for col in columns if col["name"].endswith(PCT_SUFFIX)]
    if not pct_names:
        return []
    rows = result.rows
    spec = d1.get("comparison")
    if not spec:
        # 单期 SQL 里的「上一期对比」（字典 window 类）：算不出的格子已被 SQL 挡在结果外，
        # 真出现 null 只能是基期为 0，按 §2.9 记 PCT_BASE_ZERO。
        hit = [name for name in pct_names if any(row.get(name) is None for row in rows)]
        if not hit:
            return []
        return [
            caveat(
                "PCT_BASE_ZERO",
                f"{'、'.join(hit)} 里有基期为 0 的格子，变化率写 null（§5.1 不许写 0% / ∞）",
                fields=["rows"],
            )
        ]

    left_label = str(spec.get("label_left") or spec["left"]["start"])
    right_label = str(spec.get("label_right") or spec["right"]["start"])
    key_names = list(result.plan.group_by or result.plan.dimensions)
    missing = {left_label: {}, right_label: {}}  # 期标签 → {指标名: [键…]}
    zero_base: list = []
    bases = [name[: -len(DELTA_SUFFIX)] for name in (col["name"] for col in columns) if name.endswith(DELTA_SUFFIX)]
    for base_name in bases:
        delta_name, pct_name = f"{base_name}{DELTA_SUFFIX}", f"{base_name}{PCT_SUFFIX}"
        if pct_name not in pct_names:
            continue
        for row in rows:
            key = ";".join(f"{name}={row.get(name)}" for name in key_names)
            base, delta, pct = row.get(base_name), row.get(delta_name), row.get(pct_name)
            if base is None:
                # 基期没有这个键（只在对比期出现）
                missing[left_label].setdefault(base_name, []).append(key)
            elif delta is None:
                # 基期有、对比期没有这个键
                missing[right_label].setdefault(base_name, []).append(key)
            elif pct is None:
                zero_base.append(pct_name)

    caveats = []
    for side_label, by_metric in ((left_label, missing[left_label]), (right_label, missing[right_label])):
        if not by_metric:
            continue
        keys = sorted({key for keys_ in by_metric.values() for key in keys_}, key=lambda s: s.encode("utf-8"))
        names = "、".join(sorted(by_metric, key=lambda s: s.encode("utf-8")))
        caveats.append(
            caveat(
                "COMPARISON_KEY_MISSING",
                f"两期对比里有 {len(keys)} 个键只在「{side_label}」出现，另一期没有这些键："
                f"缺失侧的 {names} 及其 __delta / __pct 写 null（不拿 0 顶替，§5.1）",
                fields=["rows"],
            )
        )
    if zero_base:
        uniq = sorted(set(zero_base), key=lambda s: s.encode("utf-8"))
        caveats.append(
            caveat(
                "PCT_BASE_ZERO",
                f"{'、'.join(uniq)} 里有基期为 0 的格子，变化率写 null（§5.1 不许写 0% / ∞）",
                fields=["rows"],
            )
        )
    return caveats


def _plan_summary(plan: list) -> str:
    """§2.1：一句话计划，非空；§4.1：不许出现数据数字（只许步骤号这类配置常量）。"""
    titles = [str(step.get("title") or "").strip() for step in plan]
    if not titles:
        return "没有可执行的步骤"
    if len(titles) == 1:
        return f"单步：{titles[0]}"
    return "多步：" + " → ".join(titles)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def run(
    *,
    question: str,
    data_paths,
    metric_dict_path: str,
    injects: dict,
    options=None,
    now: str | None = None,
) -> dict:
    """跑一整条链，返回**已经装好的** answer 文档（状态由内容决定，不抛异常给调用方）。"""
    started = time.perf_counter()
    opts = normalize_options(options)
    runner = DecisionRunner(injects=injects, model=None, options=opts, root=PROJECT_ROOT)
    state = {
        "plan": [],
        "sql": [],
        "columns": [],
        "attempts": [],
        "tables_by_name": {},
        "files": [],
        "sheets": [],
        "schema": [],
        "pool": {},
        "metric_dict": None,
        "metric_dict_sha256": "",
        "bundle": None,
    }

    try:
        metric_dict = load_metric_dict(metric_dict_path, root=PROJECT_ROOT)
        state["metric_dict"] = metric_dict
        state["metric_dict_sha256"] = metric_dict.sha256

        tables_by_name, files, sheets, schema, pool = load_tables(data_paths)
        state.update(
            {"tables_by_name": tables_by_name, "files": files, "sheets": sheets, "schema": schema, "pool": pool}
        )

        bundle = decide_all(
            runner,
            question=question,
            schema=schema,
            # `ask.decide` 是按 §5.1 的「字典视图」（list[dict]）写的，这里给它那一份，
            # 而不是 `MetricDict` 对象本身（`MetricDict` 只给 sqlgen 用，能按 id 取条目）
            metric_dict=metric_dict.d1_view(),
            now=now or date.today().isoformat(),
            files=files,
            sheets=sheets,
            metric_dict_sha256=metric_dict.sha256,
            tables=tables_by_name,
            pool=pool,
        )
        state["bundle"] = bundle

        d1 = runner.results["D1"].output
        if bundle.status == "clarify":
            return _clarify_answer(
                bundle=bundle,
                d1=d1,
                metric_dict=metric_dict,
                runner=runner,
                started=started,
                options=opts,
            )

        d2, d3 = runner.results["D2"].output, runner.results["D3"].output
        state["plan"] = []
        shape = _shape_for(question, d1, metric_dict, runner)
        plans = build_step_plans(
            d1=d1,
            d2=d2,
            d3=d3,
            tables_by_name=tables_by_name,
            metrics=metric_dict,
            shape=shape,
            question=question,
            pool=pool,
        )
        state["plan"] = [p.plan_entry() for p in plans]

        # 桥接表只有真被 JOIN 用上才进导入与 meta.files（否则「加载了没用的文件」会污染溯源）
        used = [name for p in plans for name in p.join_tables]
        for name in dict.fromkeys(used):
            if name in tables_by_name:
                continue
            if name in pool:
                tables_by_name[name] = pool[name]
                files.append({"path": pool[name].file, "sha256": pool[name].file_sha256})
        files.sort(key=lambda item: item["path"].encode("utf-8"))
        bundle.files = list(files)  # 缓存键（§5.3）要包含真被用上的桥接表哈希
        state["files"] = files
        state["tables_by_name"] = tables_by_name

        allowed_tables = list(tables_by_name) + [t for m in metric_dict for t in m.requires_tables]
        for plan in plans:
            sql = finalize_sql(plan)
            try:
                check_sql(sql, tables=allowed_tables, require_select=True, where=f"step{plan.step['step_id']}")
            except StaticReject as exc:
                # 被静态检查拦下的那条也算「一次尝试」：它有 SQL 文本、有 sql_index
                state["attempts"].append(_attempt(_as_decision_error(exc), plan, d3))
                raise

        conn = build_connection(tables_by_name)
        try:
            results = []
            for plan in plans:
                table = tables_by_name[plan.table]
                try:
                    result = run_step(conn, plan, table=table, options=opts, tables=tables_by_name)
                except DecisionError as exc:
                    state["attempts"].append(_attempt(exc, plan, d3))
                    raise
                results.append(result)
                state["sql"].append(result.sql)
                state["columns"] = [c.as_answer() for c in result.columns]
        finally:
            conn.close()

        final = results[-1]
        _apply_rid_tiebreak(final)  # §6.3.3
        table = tables_by_name[final.plan.table]

        caveats = [dict(c) for c in table.caveats]
        if any(result.join_multiplied for result in results):
            # §2.9：JOIN 后行数比任一源表大 → 结果可能被放大（金额会被重复累加），必须显式声明
            caveats.append(
                caveat(
                    "JOIN_ROW_MULTIPLIED",
                    "多表 JOIN 之后行数比任一源表都大，结果可能被行放大（金额类指标尤其危险）",
                    fields=["rows"],
                )
            )
        if final.truncated_by_row_cap:
            caveats.append(
                caveat("TRUNCATED_BY_ROW_CAP", f"结果行数超过 row_cap={opts['row_cap']}，只保留前 {opts['row_cap']} 行", fields=["rows"])
            )

        traces = build_traces(final, table=table, options=opts)
        if final.row_ids_truncated:
            caveats.append(
                caveat(
                    "TRACE_ROW_IDS_TRUNCATED",
                    f"命中行超过 trace_row_id_cap={opts['trace_row_id_cap']}，只列了前 {opts['trace_row_id_cap']} 条（按 __rid 字节序）",
                    fields=["trace"],
                )
            )
        rows = _answer_rows(final)
        columns = [c.as_answer() for c in final.columns]
        state["columns"] = columns

        if not rows:
            caveats.append(caveat("RESULT_EMPTY", "SQL 跑通了但结果 0 行（不放宽条件重查，§7.4）", fields=["rows"]))

        # §2.9 v1.1：两期对比里两种「写 null」的情形要分开记账（见 `_comparison_caveats`）
        caveats.extend(_comparison_caveats(final, d1, columns))

        # D4：要等 traces 出来才能跑
        if traces:
            runner.run(
                "D4",
                build_d4_input(question, d1, d2, {"columns": columns, "rows": _preview(rows, opts)}, _d4_traces(traces, final), opts),
                d1=d1,
                d2=d2,
                traces=_d4_traces(traces, final),
            )
        else:
            # §7.4：没有数就没有结论，D4 不必跑（注入的 D4 会引用不存在的 trace）
            runner.results["D4"] = runner._fallback("D4", reason="结果为空，没有可解读的数字（§7.4）")

        narrative = runner.results["D4"].output["narrative"]
        if not narrative:
            # §5.1：没有结论时必须显式标注。D4 兜底自己已经挂了一条 NO_INTERPRETATION（带原因），
            # 那就别再补第二条同 code 的（同一个 code 说两遍只会让下游以为出了两件事）
            known = [c["code"] for c in merge_caveats(bundle.caveats(), caveats)]
            if "NO_INTERPRETATION" not in known:
                caveats.append(
                    caveat("NO_INTERPRETATION", "只给数、不说话：这次没有挂着证据链的结论", fields=["narrative"])
                )

        check_traces(
            traces,
            sql_statements=state["sql"],
            columns=columns,
            rows=rows,
            files=files,
            tables_by_name=tables_by_name,
            options=opts,
            metrics_used=metric_dict.snapshots(
                {mid for plan in plans for mid in plan.metric_ids}
            ),
        )

        meta_model = _meta_model(runner)
        key = _cache_key(bundle, meta_model, opts)
        return _body(
            question=question,
            answer_id=decide_mod.answer_id(key),
            status="ok",
            plan=state["plan"],
            plan_summary=_plan_summary(state["plan"]),
            metrics_used=metric_dict.snapshots({mid for plan in plans for mid in plan.metric_ids}),
            sql=list(state["sql"]),
            columns=columns,
            rows=rows,
            trace=traces,
            charts=_build_charts(final, traces, columns),
            narrative=narrative,
            caveats=sort_caveats(merge_caveats(bundle.caveats(), caveats)),
            clarify=None,
            error=None,
            meta=_meta(
                started=started,
                meta_model=meta_model,
                key=key,
                files=files,
                metric_dict_sha256=metric_dict.sha256,
                options=opts,
            ),
        )
    except (DecisionError, StaticReject, IngestError) as exc:
        error = _as_decision_error(exc)
        return _error_answer(
            question=question,
            error=error,
            state=state,
            runner=runner,
            started=started,
            options=opts,
        )


def _as_decision_error(exc) -> DecisionError:
    if isinstance(exc, DecisionError):
        return exc
    if isinstance(exc, StaticReject):
        return DecisionError(
            "SQL_STATIC_REJECT",
            str(exc),
            stage="static_check",
            readable_hint=getattr(exc, "readable_hint", "") or "这条 SQL 没过只读静态检查，已拒绝执行",
        )
    return DecisionError(
        "FILE_PARSE_ERROR" if exc.code not in ("COLUMN_NOT_FOUND", "SHEET_NOT_FOUND", "FILE_NOT_FOUND") else exc.code,
        str(exc),
        stage="ingest",
        readable_hint=getattr(exc, "readable_hint", "") or "文件读不了",
    )


def _attempt(error: DecisionError, plan, d3) -> dict:
    """§2.11 `error.attempts[]`：**一次 SQL 尝试**一行。

    `d3_snapshot` 是这条 SQL 所依据的 D3 输出（§2.15 示例同义），所以 `source_decision` 记 `D3`
    （兜底重试也是 D3 改选列）；只有错误本身就出在 D1 时才记 D1。
    读文件 / 建计划阶段就失败的，**一行都不记**（`attempts: []`）：那时没有任何 SQL 被尝试过，
    硬编一条会让 `sql_index` 指向不存在的 `sql[]`（§2.15 的判据是「sql 非空 ⟺ 至少跑成过一条」）。
    """
    return {
        "attempt": 1,
        "source_decision": "D1" if error.stage == "D1" else "D3",
        "sql_index": int(plan.sql_index),
        "error_code": error.code,
        "error_message": error.message,
        "d3_snapshot": dict(d3 or {}),
    }


def _answer_rows(result) -> list:
    """§2.5：每行 = `__rid` + 全部输出列（键序与 `columns[]` 一致）。"""
    rows = []
    for row, rids in zip(result.rows, result.rids):
        rid = row.get("__rid") or (rids[0] if rids else "")
        ordered = {"__rid": rid}
        for col in result.columns:
            ordered[col.name] = row.get(col.name)
        rows.append(ordered)
    return rows


def _apply_rid_tiebreak(result) -> None:
    """§6.3.3：`rows` 最终按 `__rid` 做最后一道 tiebreak（只在「排序键全等的相邻行」内部生效）。"""
    names = [c.name for c in result.columns]

    def key_of(row):
        return tuple((type(row.get(n)).__name__, row.get(n)) for n in names)

    pairs = list(zip(result.rows, result.rids))
    index = 0
    while index < len(pairs):
        end = index + 1
        while end < len(pairs) and key_of(pairs[end][0]) == key_of(pairs[index][0]):
            end += 1
        if end - index > 1:
            pairs[index:end] = sorted(pairs[index:end], key=lambda item: _rid_of(item).encode("utf-8"))
        index = end
    result.rows = [row for row, _rids in pairs]
    result.rids = [rids for _row, rids in pairs]


def _rid_of(pair) -> str:
    row, rids = pair
    return row.get("__rid") or (rids[0] if rids else "")


def _preview(rows, options) -> list:
    return rows[: int(options["rows_preview_limit"])]


def _d4_traces(traces, result) -> list:
    """§5.1 D4 输入里的 traces 形状：只要 id / 值 / 指标 / 单位 / 位数。"""
    columns = {c.name: c for c in result.columns}
    out = []
    for trace in traces:
        name = trace["value_path"].split(".", 1)[-1]
        col = columns.get(name)
        out.append(
            {
                "trace_id": trace["trace_id"],
                "value": trace["value"],
                "metric_id": trace["metric_id"],
                "unit": col.unit if col is not None else None,
                "digits": col.digits if col is not None else 0,
            }
        )
    return out


def _build_charts(result, traces, columns) -> list:
    """§2.7：只给「画什么」，不给数据值；每个点都必须有 trace，否则不画。"""
    by_path = {trace["value_path"]: trace["trace_id"] for trace in traces}
    x = next((c for c in result.columns if c.type in ("text", "date")), None)
    if x is None or not result.rows:
        return []
    y_items = []
    for col in result.columns:
        if col.type not in NUMERIC_TYPES:
            continue
        paths = [f"rows[{index}].{col.name}" for index in range(len(result.rows))]
        if not all(path in by_path for path in paths):
            continue  # 有格子没 trace → 这个点不许画，干脆不画这条序列
        y_items.append((col, paths))
    if not y_items:
        return []
    ids = []
    for _col, paths in y_items:
        for path in paths:
            if by_path[path] not in ids:
                ids.append(by_path[path])
    step_title = str(result.plan.step.get("title") or "").strip()
    return [
        {
            "chart_id": "c-01",
            "type": "line" if x.type == "date" else "bar",
            "title": step_title or f"{x.name} 一览",
            "x": {"field": x.name, "label": x.name, "type": x.type},
            "y": [
                {
                    "field": col.name,
                    "label": col.metric.name if col.metric is not None else col.name,
                    "unit": col.unit,
                    "digits": col.digits,
                }
                for col, _paths in y_items
            ],
            "sort": {"by": "__row", "desc": False},
            "source_trace_ids": ids,
            "note": None,
        }
    ]


def _clarify_answer(*, bundle, d1, metric_dict, runner, started, options) -> dict:
    meta_model = _meta_model(runner)
    key = _cache_key(bundle, meta_model, options)
    plan = [step for step in (runner.results["D2"].output["steps"] if "D2" in runner.results else [])]
    return _body(
        question=bundle.question,
        answer_id=decide_mod.answer_id(key),
        status="clarify",
        plan=plan,
        plan_summary="先确认口径，再算数（反问，不出数）",
        metrics_used=_clarify_metrics(d1, metric_dict, bundle.route),
        sql=[],
        columns=[],
        rows=[],
        trace=[],
        charts=[],
        narrative=[],
        caveats=sort_caveats(merge_caveats(bundle.caveats(), [])),
        clarify=bundle.route.clarify,
        error=None,
        meta=_meta(
            started=started,
            meta_model=meta_model,
            key=key,
            files=bundle.files,
            metric_dict_sha256=bundle.metric_dict_sha256,
            options=options,
        ),
    )


def _error_answer(*, question, error: DecisionError, state, runner, started, options) -> dict:
    meta_model = _meta_model(runner)
    bundle = state.get("bundle")
    if bundle is not None:
        key = _cache_key(bundle, meta_model, options)
        files, dict_sha = bundle.files, bundle.metric_dict_sha256
    else:  # 连 D1 都没跑到（读文件/字典就失败了）→ 用空清单算键
        key = decide_mod.cache_key(
            question=question,
            files=state.get("files") or [],
            sheets=state.get("sheets") or [],
            metric_dict_sha256=state.get("metric_dict_sha256") or "",
            decisions={
                name: {"source": entry["source"], "output_sha256": entry["output_sha256"]}
                for name, entry in meta_model.items()
            },
            options=options,
        )
        files, dict_sha = state.get("files") or [], state.get("metric_dict_sha256") or ""
    body = error.as_error_body()
    body["attempts"] = state.get("attempts") or []
    body["partial"] = {
        "plan": list(state.get("plan") or []),
        "sql": list(state.get("sql") or []),
        "columns": list(state.get("columns") or []),
    }
    caveats = merge_caveats(bundle.caveats() if bundle is not None else [], [])
    return _body(
        question=question,
        answer_id=decide_mod.answer_id(key),
        status="error",
        plan=list(state.get("plan") or []),
        plan_summary=_plan_summary(state.get("plan") or []) + "（执行失败）",
        metrics_used=[],
        sql=list(state.get("sql") or []),
        columns=list(state.get("columns") or []),
        rows=[],
        trace=[],
        charts=[],
        narrative=[],
        caveats=sort_caveats(caveats),
        clarify=None,
        error=body,
        meta=_meta(
            started=started,
            meta_model=meta_model,
            key=key,
            files=files,
            metric_dict_sha256=dict_sha,
            options=options,
        ),
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


USAGE = """ask.query · 提问 → answer.json（契约 §2 / §5 / §6.5）

  python -m ask.query --question-file ask/fixtures/question.first.txt \\
      --decide d1=inject:ask/inject/d1.json --out ask/answer_out/smoke.json

  --question TEXT          直接给问题（与 --question-file 二选一）
  --question-file PATH     从文件读问题（逐字保存，末尾换行不算）
  --data PATH              数据文件，可重复（默认 ask/fixtures/smoke.xlsx）
  --metric-dict PATH       指标字典（默认 dict/metrics.json）
  --decide dN=inject:PATH  注入某个决策点，可重复（§5.2）
  --out PATH               写到哪（默认打到 stdout）
  --now YYYY-MM-DD         D1 输入里的「今天」（默认本地今天）
  --options k=v,k=v        row_cap / trace_row_id_cap / rows_preview_limit
  --selftest               跑一遍自带 fixture 的自检
"""


def parse_options(text: str | None) -> dict:
    out = {}
    for chunk in (text or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"--options 的每一项都应是 k=v，拿到 {chunk!r}")
        key, value = chunk.split("=", 1)
        key = key.strip()
        if key not in DEFAULT_OPTIONS:
            raise ValueError(f"--options 只认 {sorted(DEFAULT_OPTIONS)}，拿到 {key!r}")
        try:
            out[key] = int(value.strip())
        except ValueError:
            raise ValueError(f"--options {key} 必须是整数，拿到 {value!r}")
    return out


def main(argv: list | None = None) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(prog="ask.query", usage=USAGE, add_help=True)
    parser.add_argument("--question")
    parser.add_argument("--question-file")
    parser.add_argument("--data", action="append")
    parser.add_argument("--metric-dict", default=DEFAULT_METRIC_DICT)
    parser.add_argument("--decide", action="append", default=[])
    parser.add_argument("--out")
    parser.add_argument("--now")
    parser.add_argument("--options")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    try:
        injects = parse_decide_args(args.decide)
        options = parse_options(args.options)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    try:
        question = load_question(args.question, args.question_file)
    except (DecisionError, OSError) as exc:
        print(f"读不到问题：{exc}", file=sys.stderr)
        return 2

    document = run(
        question=question,
        data_paths=list(args.data or [DEFAULT_DATA]),
        metric_dict_path=args.metric_dict,
        injects=injects,
        options=options,
        now=args.now,
    )
    text = render(document)
    if args.out:
        target = Path(args.out)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(f"{document['status']} → {target.as_posix()}（answer_id={document['answer_id']}）")
    else:
        sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


def _selftest() -> int:
    """跑一遍自带 fixture：真读文件、真执行、真校验 trace。"""
    results = []

    def check(name, fn):
        try:
            fn()
            results.append((True, name, ""))
        except Exception as exc:  # noqa: BLE001
            results.append((False, name, f"{type(exc).__name__}: {exc}"))

    def pipeline():
        doc = run(
            question="2025 年各区域的成交额是多少",
            data_paths=[DEFAULT_DATA],
            metric_dict_path=DEFAULT_METRIC_DICT,
            injects={"D1": "ask/inject/d1.json"},
        )
        assert doc["status"] == "ok", doc.get("error")
        assert doc["trace"], "没有 trace"
        codes = [c["code"] for c in doc["caveats"]]
        assert "NOT_PRUNED" in codes and "PLAN_MINIMAL" in codes, codes
        assert doc["clarify"] is None and doc["error"] is None
        # 问的是聚合（「各区域的成交额」）→ 产物必须自己就是聚合：走 gmv 口径、按 region 分组
        assert [m["id"] for m in doc["metrics_used"]] == ["gmv"], doc["metrics_used"]
        assert "GROUP BY" in doc["sql"][0] and "gmv" in doc["sql"][0], doc["sql"]
        assert [c["name"] for c in doc["columns"]] == ["region", "gmv"], doc["columns"]
        assert len(doc["rows"]) == 3, doc["rows"]
        assert doc["plan"][0]["needs"]["metric_ids"] == ["gmv"], doc["plan"]
        assert len(doc["trace"]) == sum(
            1
            for row in doc["rows"]
            for col in doc["columns"]
            if col["type"] in ("int", "decimal") and row.get(col["name"]) is not None
        ), "trace 条数与数值单元格数量不等"
        text = render(doc)
        assert text.endswith("\n") and not text.endswith("\n\n")
        assert "\\u" not in text
        # 稳定子集：同样输入再跑一次，除允许变动的字段外必须一致
        again = run(
            question="2025 年各区域的成交额是多少",
            data_paths=[DEFAULT_DATA],
            metric_dict_path=DEFAULT_METRIC_DICT,
            injects={"D1": "ask/inject/d1.json"},
        )
        for key in ("plan", "sql", "columns", "rows", "trace", "charts", "caveats", "narrative"):
            assert render(doc[key]) == render(again[key]), f"{key} 不稳定"
        assert doc["answer_id"] == again["answer_id"], "answer_id 不稳定"

    def clarify_case():
        doc = run(
            question="各区域的成交额是多少",  # 没有时间范围也没有时间词 → D1 注入不适用，用空注入集合触发兜底
            data_paths=[DEFAULT_DATA],
            metric_dict_path=DEFAULT_METRIC_DICT,
            injects={},
        )
        assert doc["status"] == "error", doc["status"]  # 没配模型 → D1 没有兜底，如实报错
        assert doc["error"]["code"] == "INTERNAL", doc["error"]
        assert list(doc["error"]) == ["code", "message", "readable_hint", "stage", "attempts", "partial"]
        assert doc["error"]["partial"] == {"plan": [], "sql": [], "columns": []}, doc["error"]["partial"]
        # 没走到 SQL 就失败了 → 一行 attempt 都不记（§2.11：attempts 是「每次 SQL 尝试」）
        assert doc["error"]["attempts"] == [], doc["error"]["attempts"]
        assert doc["clarify"] is None and doc["sql"] == [] and doc["narrative"] == []
        render(doc)

    def clarify_body():
        # 字典里查不到的指标 → METRIC_NOT_IN_DICT + 反问。为此临时写一份注入（自检结束就删）
        payload = {
            "decision": "D1",
            "contract_version": CONTRACT_VERSION,
            "input_digest": "",
            "output": {
                "metric_ids": ["not_a_metric"],
                "listing": False,
                "time_range": {"start": "2025-01-01", "end": "2025-12-31", "grain": "year"},
                "time_column": None,
                "time_grain_grouping": None,
                "comparison": None,
                "dimensions": ["region"],
                "filters": [],
                "ambiguity": [],
                "confidence": 0.4,
            },
        }
        handle, path = tempfile.mkstemp(prefix="_selftest_d1_", suffix=".json", dir=str(Path(__file__).parent))
        os.close(handle)
        try:
            Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            doc = run(
                question="2025 年各区域的成交额是多少",
                data_paths=[DEFAULT_DATA],
                metric_dict_path=DEFAULT_METRIC_DICT,
                injects={"D1": path},
            )
        finally:
            Path(path).unlink(missing_ok=True)
        assert doc["status"] == "clarify", doc["status"]
        assert doc["clarify"]["asked_at_step"] == "D1" and doc["clarify"]["missing"], doc["clarify"]
        for key in ("sql", "columns", "rows", "trace", "charts", "narrative"):
            assert doc[key] == [], f"{key} 在反问体里必须是空数组"
        assert doc["metrics_used"] == [], "字典里查不到的指标不许进 metrics_used"
        assert doc["error"] is None
        assert "METRIC_NOT_IN_DICT" in [c["code"] for c in doc["caveats"]], doc["caveats"]
        render(doc)

    def grain_monthly():
        """§5.1 D1 v1.1 `time_grain_grouping`：派生列 `period` + `GROUP BY period`（§2.4）。"""
        doc = run(
            question="2025 年每个月的成交额是多少",
            data_paths=[DEFAULT_DATA],
            metric_dict_path=DEFAULT_METRIC_DICT,
            injects={"D1": "ask/inject/d1_monthly.json"},
        )
        assert doc["status"] == "ok", doc.get("error")
        assert [c["name"] for c in doc["columns"]] == ["period", "gmv"], doc["columns"]
        period = doc["columns"][0]
        assert period["type"] == "text" and period["digits"] == 0 and period["unit"] is None, period
        assert period["source_metric_id"] is None and period["source_column"] is None, period
        assert period["derived"] is True and period["from"] == "sale_date" and period["grain"] == "month", period
        assert 'GROUP BY "period"' in doc["sql"][0], doc["sql"]
        assert [row["period"] for row in doc["rows"]] == ["2025-01", "2025-02", "2025-03"], doc["rows"]
        assert [row["gmv"] for row in doc["rows"]] == [
            Decimal("1200.50"),
            Decimal("800.00"),
            Decimal("510.25"),
        ], doc["rows"]
        assert len(doc["trace"]) == 3, doc["trace"]

    def period_compare():
        """§5.1 D1 v1.1 `comparison`：两期在同一 SQL 里算完，多两列 `__delta` / `__pct`。"""
        doc = run(
            question="2025 年 2 月相比 1 月，各区域成交额变化多少",
            data_paths=[DEFAULT_DATA],
            metric_dict_path=DEFAULT_METRIC_DICT,
            injects={"D1": "ask/inject/d1_compare.json"},
        )
        assert doc["status"] == "ok", doc.get("error")
        assert [c["name"] for c in doc["columns"]] == ["region", "gmv", "gmv__delta", "gmv__pct"], doc["columns"]
        delta, pct = doc["columns"][2], doc["columns"][3]
        assert delta["derived"] is True and delta["source_metric_id"] == "gmv" and delta["source_column"] is None, delta
        assert (delta["type"], delta["unit"], delta["digits"]) == ("decimal", "CNY", 2), delta
        assert (pct["type"], pct["unit"], pct["digits"]) == ("decimal", "%", 2), pct
        # 两期必须在一条 SQL 里（§5.1：含 LAG( / OVER( / WITH 之一，不许分两条再口算）
        assert "WITH " in doc["sql"][0] and doc["sql"][0].count('FROM "Sheet1"') == 2, doc["sql"]
        # §5.1 v1.1：键集是两期键集的**并集**（华南只在 2 月出现，照样出一行，缺的一侧写 null）
        assert [row["region"] for row in doc["rows"]] == ["华东", "华南"], doc["rows"]
        row, only_right = doc["rows"][0], doc["rows"][1]
        assert (row["gmv"], row["gmv__delta"], row["gmv__pct"]) == (
            Decimal("1200.50"),
            Decimal("-400.50"),
            Decimal("-33.36"),
        ), row
        assert (only_right["gmv"], only_right["gmv__delta"], only_right["gmv__pct"]) == (None, None, None), only_right
        codes = [c["code"] for c in doc["caveats"]]
        assert len(doc["trace"]) == 3 and "PCT_BASE_ZERO" not in codes, doc["caveats"]
        assert "COMPARISON_KEY_MISSING" in codes, doc["caveats"]  # 缺失侧不补 0

    def pct_base_zero():
        """§2.9 / §5.1 v1.1：基期为 0 → 变化率写 null 并记 `PCT_BASE_ZERO`（不许写 0% / ∞）。"""
        doc = run(
            question="2025 年 3 月相比 2 月，各区域成交额变化多少",
            data_paths=[DEFAULT_DATA],
            metric_dict_path=DEFAULT_METRIC_DICT,
            injects={"D1": "ask/inject/d1_compare_zero.json"},
        )
        assert doc["status"] == "ok", doc.get("error")
        # 华南：2 月只有一条未成交行（合计 0），3 月有成交 → 基期为 0
        assert [row["region"] for row in doc["rows"]] == ["华东", "华北", "华南"], doc["rows"]
        row = doc["rows"][2]
        assert row["gmv"] == Decimal("0.00"), row
        assert row["gmv__delta"] == Decimal("450.25"), row
        assert row["gmv__pct"] is None, row
        codes = [c["code"] for c in doc["caveats"]]
        assert "PCT_BASE_ZERO" in codes, doc["caveats"]
        # 华东 / 华北 是「只在一期出现」的键，与基期为 0 分开记账
        assert "COMPARISON_KEY_MISSING" in codes, doc["caveats"]
        # 空值格不发 trace（见 ask/_selfcheck/notes.md 第 4 条）：华东.gmv + 华南.gmv + 华南.__delta
        assert len(doc["trace"]) == 3, doc["trace"]

    def attempt_entry():
        class FakePlan:
            sql_index = 0

        entry = _attempt(
            DecisionError("SQL_EXEC_ERROR", "no such column: amout", stage="execute"),
            FakePlan(),
            {"tables": [], "columns": [], "joins": [], "pruned": False, "prune_strategy": "all", "token_estimate": 0},
        )
        assert list(entry) == ["attempt", "source_decision", "sql_index", "error_code", "error_message", "d3_snapshot"]
        assert entry["attempt"] == 1 and entry["source_decision"] == "D3" and entry["sql_index"] == 0

    def serialize_case():
        text = render({"a": Decimal("1.20"), "b": [1, 2], "c": None, "d": "中"})
        assert "1.20" in text and "中" in text and "e+" not in text, text
        try:
            render({"x": 1.5})
        except TypeError:
            return
        raise AssertionError("浮点没被挡住")

    for name, fn in (
        ("pipeline", pipeline),
        ("error_body", clarify_case),
        ("clarify_body", clarify_body),
        ("grain_monthly", grain_monthly),
        ("period_compare", period_compare),
        ("pct_base_zero", pct_base_zero),
        ("attempt_entry", attempt_entry),
        ("serialize", serialize_case),
    ):
        check(name, fn)

    failed = [r for r in results if not r[0]]
    for ok, name, error in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {error}" if error else ""))
    print(f"{len(results) - len(failed)}/{len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
