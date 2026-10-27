"""show/export.py —— 导出层：answer.json 副本 + report.md + 每个图一份 SVG，
外加一份**不自动生效**的指标字典提案 `dict_proposal.json`（提案制）。

边界（contract/answer.md，别处不猜）：
  * 只吃一份 answer.json —— 不读源表格、不连数据库、不 import `ask/`、不读也不写 `dict/`。
  * **不重算任何数字**：报告里的每个数都原样取自 `rows[]` / `trace[]`，
    唯一发生的运算是「按 `columns[].digits` 格式化」（第 6.2 节）与「取 sha256」（第 2.3 节）。
    报告里的取数语句是**原文照抄** `answer.json` 的 `sql[]`，本文件既不生成也不改写它。
  * 不新增契约字段。必填字段缺失或体内自相矛盾 ⇒ 抛 `ShowError` 停下，不补默认值。
    唯一例外是 `_fixture_note`：那是 fixture 自己的键，有就原样摆出来当免责声明。
  * 导出物（第 8.4 节）：SVG 不含脚本、`on*` 属性、`javascript:` URL；report.md 是纯
    Markdown，不执行任何 HTML。导出只写 `--out` 目录，不写回源数据目录。

产物（`--out` 目录里）：
  answer.json          输入 answer.json 的**字节级副本**
  report.md            人读报告：计划 / 结论 / 结果表 / 图 / 追溯链 / 限制 / 自检
  <answer_id>-<chart_id>.svg   每个图一份（`charts[]` 里没有数据值，图上也不画新数）
  dict_proposal.json   「这次答案里用到、但字典里没有的口径」的提案（待人工审阅，不自动生效）

用法：
  python -m show.export --answer ask/answer_out/smoke.json --out show/out/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

from show import render as render_mod
from show import svg

# 契约 3.1：单位白名单（封闭集）与 id 正则
UNIT_WHITELIST = ("CNY", "%", "人", "次", "个", "天", "ratio", "none")
METRIC_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
ID_PATTERN_TEXT = "^[a-z][a-z0-9_]{1,31}$"

# 契约 3.1：default_aggregation 枚举 / trace aggregation 的 op 枚举（第 4.3 节 T6）
AGGREGATIONS = ("sum", "avg", "count", "count_distinct", "min", "max")
IDENTITY_OP = "identity"

# 契约 8.4：SVG 里不许出现的东西
SVG_FORBIDDEN = ("<script", "javascript:", "onload=", "onclick=", "onerror=", "onmouseover=")

PROPOSAL_NAME = "dict_proposal.json"
REPORT_NAME = "report.md"
ANSWER_NAME = "answer.json"


class ExportError(Exception):
    """导出无法进行（answer.json 不可渲染，或产物不安全）。"""


# --------------------------------------------------------------------------
# 小工具：Markdown 安全输出
# --------------------------------------------------------------------------


def md_prose(text: object) -> str:
    """正文转义：只挡住会被当成 HTML 起始的东西，别的原样保留。"""
    if text is None:
        return "null"
    out = str(text)
    out = out.replace("&", "&amp;").replace("<", "&lt;")
    return out


def md_cell(text: object) -> str:
    """表格单元转义：还要挡 `|` 与换行（一处 `|` 就能把整张表撕开）。"""
    out = md_prose(text).replace("|", "\\|")
    return out.replace("\r", " ").replace("\n", " ").strip()


def md_code(text: object) -> str:
    """行内代码：内部反引号换成撇号，避免提前闭合。"""
    if text is None:
        return "`null`"
    return "`" + str(text).replace("`", "'") + "`"


def md_json(value: object) -> str:
    """块级 JSON（只用于 d3_snapshot 这类结构化证据）。"""
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return "```json\n" + text + "\n```"


def number_text(value: object, column: dict | None) -> str:
    """按 columns[].digits 格式化一个数；不加单位（单位放表头），null 就是 null。"""
    if value is None:
        return "null"
    digits = int((column or {}).get("digits") or 0)
    text = render_mod.format_number(value, digits=digits, unit=None)
    return text


def cell_text(value: object, column: dict | None) -> str:
    """表格单元：数值列按 digits 走，其它原样。"""
    if value is None:
        return "null"
    kind = (column or {}).get("type")
    if kind in render_mod.NUMERIC_TYPES:
        return number_text(value, column)
    if kind == "bool":
        return "true" if value else "false"
    return str(value)


def column_header(column: dict) -> str:
    """表头：列名 + 类型/单位/口径，一行说清这一列是什么。"""
    bits = [str(column.get("name"))]
    kind = column.get("type")
    if kind in render_mod.NUMERIC_TYPES:
        bits.append(f"{kind} · {int(column.get('digits') or 0)} 位")
    else:
        bits.append(str(kind))
    if column.get("unit") is not None:
        bits.append(str(column.get("unit")))
    if column.get("source_metric_id"):
        bits.append(f"字典 {column.get('source_metric_id')}")
    elif column.get("source_column"):
        bits.append(f"源列 {column.get('source_column')}")
    return md_cell(" / ".join(bits))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 产物 1：answer.json 字节级副本
# --------------------------------------------------------------------------


def export_answer(source: Path, out_dir: Path) -> tuple[Path, bool]:
    """原样拷贝一份 answer.json（不改一个字节：改了就不是同一份答案了）。"""
    target = out_dir / ANSWER_NAME
    if source.resolve() == target.resolve():
        return target, False
    try:
        payload = source.read_bytes()
    except OSError as exc:  # pragma: no cover - 环境问题
        raise ExportError(f"读不了 {source.name}：{exc}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        handle.write(payload)
    return target, True


# --------------------------------------------------------------------------
# 产物 2：每个图一份 SVG（图上的点只来自 rows[]，本模块一个数字都不加）
# --------------------------------------------------------------------------


def chart_svg_name(answer_id: object, chart_id: object) -> str:
    safe_id = re.sub(r"[^0-9A-Za-z_-]+", "-", str(answer_id or "answer")).strip("-") or "answer"
    safe_chart = re.sub(r"[^0-9A-Za-z_-]+", "-", str(chart_id or "chart")).strip("-") or "chart"
    return f"{safe_id}-{safe_chart}.svg"


def svg_problems(markup: str) -> list[str]:
    """契约 8.4：SVG 里不许出现未转义的脚本 / 事件属性 / javascript: URL。"""
    lowered = markup.lower()
    return [token for token in SVG_FORBIDDEN if token in lowered]


def export_charts(answer: dict, plans: list[dict], out_dir: Path) -> list[dict]:
    """把每张图写成一份独立 SVG；画不出来的图**不占位**，只留一条说明。"""
    written: list[dict] = []
    answer_id = answer.get("answer_id")
    for plan in plans:
        chart = plan["chart"]
        chart_id = chart.get("chart_id")
        title = chart.get("title") or chart_id or "图"
        kind = chart.get("type")
        y_label = " / ".join(
            f"{spec.get('label') or spec.get('field')}"
            + (f"（{spec.get('unit')}）" if spec.get("unit") else "")
            for spec in plan["series"]
        )
        if kind == "bar":
            markup = svg.bar_chart(
                title=str(title), categories=plan["categories"], series=plan["series"],
                x_label=plan["x_label"], y_label=y_label,
            )
        elif kind == "line":
            markup = svg.line_chart(
                title=str(title), categories=plan["categories"], series=plan["series"],
                x_label=plan["x_label"], y_label=y_label,
            )
        elif kind == "scatter":
            markup = svg.scatter_chart(
                title=str(title), points=plan["points"],
                x_label=plan["x_label"], y_label=y_label,
            )
        else:
            written.append({
                "chart_id": chart_id,
                "file": None,
                "note": f"未知图型「{kind}」：契约 2.7 只有 bar / line / scatter，不画、也不给空图占位",
            })
            continue

        bad = svg_problems(markup)
        if bad:
            written.append({
                "chart_id": chart_id,
                "file": None,
                "note": "SVG 里出现契约 8.4 禁止的东西（" + "、".join(bad) + "），拒绝对外导出",
            })
            continue

        name = chart_svg_name(answer_id, chart_id)
        write_text(out_dir / name, markup + ("\n" if not markup.endswith("\n") else ""))
        written.append({"chart_id": chart_id, "file": name, "note": None})
    return written


# --------------------------------------------------------------------------
# 产物 3：字典提案（提案制：只提案，不落进字典，不自动生效）
# --------------------------------------------------------------------------


def _observation(traces: list[dict], column: dict) -> dict:
    """把「这列的数是怎么来的」照抄下来，供人判断该不该立成字典条目。"""
    first = traces[0]
    source = first.get("source") or {}
    aggregations = []
    for trace in traces:
        steps = [
            {key: value for key, value in (step or {}).items() if key in ("op", "over", "group_by")}
            for step in (trace.get("aggregation") or [])
            if isinstance(step, dict)
        ]
        aggregations.append({"trace_id": trace.get("trace_id"), "steps": steps})
    return {
        "value_paths": [str(trace.get("value_path")) for trace in traces],
        "trace_ids": [trace.get("trace_id") for trace in traces],
        "expression": first.get("expression"),
        "sql_index": first.get("sql_index"),
        "source": {
            "file": source.get("file"),
            "file_sha256": source.get("file_sha256"),
            "sheet": source.get("sheet"),
            "header_row": source.get("header_row"),
            "columns": list(source.get("columns") or []),
        },
        "filter": first.get("filter"),
        "aggregation": aggregations,
        "column": {
            "name": column.get("name"),
            "type": column.get("type"),
            "unit": column.get("unit"),
            "digits": column.get("digits"),
            "source_column": column.get("source_column"),
        },
    }


def _check(rule: str, status: str, detail: str) -> dict:
    return {"rule": rule, "status": status, "detail": detail}


def _proposal_checks(entry: dict, observations: dict, candidate_id: str | None) -> list[dict]:
    """只跑「在 show/ 里判得了」的那几条（契约 3.2）。判不了的一律 pending，不许假装绿。"""
    checks: list[dict] = []

    pattern = ID_PATTERN_TEXT
    metric_id = entry.get("id") if entry.get("id") is not None else candidate_id
    if metric_id is None:
        checks.append(_check("V1", "pending", f"id 待人工确定：契约 3.1 要求匹配 {pattern} 且全局唯一，"
                                              "show/ 看不到现有字典，不替你起名"))
    elif METRIC_ID.match(str(metric_id)):
        checks.append(_check("V1", "pass", f"候选 id「{metric_id}」符合 {pattern}（这是照抄 source_column 的候选，"
                                           "全局唯一性仍需在整份字典上判）"))
    else:
        checks.append(_check("V1", "fail", f"候选 id「{metric_id}」不符合 {pattern}"))

    name = entry.get("name")
    if not name:
        checks.append(_check("V2", "pending", "name 待人工填：契约 3.1 要求中文名非空、≤20 字、且同一 name 只许对应一个 id"))
    elif len(str(name)) > 20:
        checks.append(_check("V2", "fail", f"name「{name}」超过 20 字"))
    else:
        checks.append(_check("V2", "pending", f"name 候选「{name}」长度合规；与现有字典是否重名需要在整份字典上判"))

    unit = entry.get("unit")
    if unit is None:
        checks.append(_check("V6", "pending", "unit 待人工填：契约 3.1 的单位白名单是封闭集，不许猜"))
    elif str(unit) in UNIT_WHITELIST:
        checks.append(_check("V6", "pass", f"unit「{unit}」在白名单内"))
    else:
        checks.append(_check("V6", "fail", f"unit「{unit}」不在白名单 {'/'.join(UNIT_WHITELIST)} 内"))

    definition = entry.get("definition")
    if definition is None:
        checks.append(_check("V3/V9/V11", "pending",
                             "definition 待口径负责人填写：它是「可执行聚合表达式」，不是 show/ 能代拟的东西"
                             "（README 铁律 3：模型不许自创口径）。填好后 V3 静态检查、V9 禁 LIMIT/ORDER BY、"
                             "V11 与 default_aggregation 一致三条才有得判"))
    else:
        banned = [word for word in ("LIMIT", "ORDER BY") if word.lower() in str(definition).lower()]
        checks.append(_check("V9", "fail" if banned else "pass",
                             f"definition 里出现 {'、'.join(banned)}" if banned else "definition 里没有 LIMIT / ORDER BY"))

    checks.append(_check("V4/V5", "pending",
                         "需要 schema 快照与表名（契约 3.2）：definition 引用的列/表是否存在、requires_tables 是否"
                         "与实际引用的表集合**恰好相等**，show/ 都看不到，留给 dict/validate.py"))
    checks.append(_check("V8", "pending", "需要历史快照：同 id 同 version 的 definition 哈希必须一致，show/ 手上没有历史"))
    checks.append(_check("V12", "pending",
                         "契约 3.2 V12 要求**整份字典一次性校验**、任一条失败即整份加载失败；"
                         "所以合并动作只能在 dict/ 里做，show/ 只提案"))
    return checks


def _suggestions(column: dict, chart_labels: dict, observations: dict) -> dict:
    """从 answer.json 里**看得见的东西**给出建议值 —— 建议不是结论，人工可全推翻。"""
    label = chart_labels.get(column.get("name")) or {}
    ops: list[str] = []
    for item in observations.get("aggregation") or []:
        for step in item.get("steps") or []:
            op = step.get("op")
            if op and op not in ops:
                ops.append(op)
    aggregation_hint = None
    if len(ops) == 1 and ops[0] in AGGREGATIONS:
        aggregation_hint = ops[0]
    return {
        "name": label.get("label"),
        "unit": column.get("unit"),
        "default_aggregation": aggregation_hint,
        "note": "以上均照抄自 answer.json（id 候选取自 columns[].source_column，名字取自 charts[].y[].label，"
                "单位取自 columns[].unit，聚合取自 trace[].aggregation），不是字典结论；"
                "definition 与 owner 只能由人填",
    }


def build_proposal(answer: dict) -> dict:
    """生成提案文件。**只读** answer.json：不读 `dict/`、不写 `dict/metrics.json`。"""
    columns = list(answer.get("columns") or [])
    rows = list(answer.get("rows") or [])
    chart_labels: dict = {}
    for chart in answer.get("charts") or []:
        if not isinstance(chart, dict):
            continue
        for spec in chart.get("y") or []:
            if isinstance(spec, dict) and spec.get("field"):
                chart_labels.setdefault(spec["field"], {
                    "label": spec.get("label"),
                    "unit": spec.get("unit"),
                    "digits": spec.get("digits"),
                })
    _, by_path = render_mod.trace_maps(answer)

    proposals: list[dict] = []
    problems: list[str] = []
    for column in columns:
        if column.get("type") not in render_mod.NUMERIC_TYPES:
            continue  # 只有数字才需要「口径」；文本/日期列不立指标
        if column.get("source_metric_id") is not None:
            continue  # 已经是字典口径（契约 3.3 第 1 条）
        name = column.get("name")
        paths = [f"rows[{index}].{name}" for index in range(len(rows))]
        traces = [by_path[path] for path in paths if path in by_path]
        if not traces:
            problems.append(
                f"列「{name}」是非字典口径，但没有任何 trace 指向它 ⇒ 契约 4.1 要求一个输出数字一条 trace，"
                "这属于上游答案的问题，show/ 不代为补一条"
            )
            continue
        stray = [trace.get("trace_id") for trace in traces if trace.get("metric_id") is not None]
        if stray:
            problems.append(
                f"列「{name}」的 source_metric_id 是 null，但 trace {'、'.join(map(str, stray))} 却带了 metric_id "
                "⇒ 契约 3.3 第 1 条要求两者同 null 或同值，这里自相矛盾"
            )
            continue

        observations = _observation(traces, column)
        source_column = column.get("source_column")
        candidate_id = source_column if isinstance(source_column, str) and METRIC_ID.match(source_column) else None
        entry = {
            "id": None,
            "name": None,
            "definition": None,
            "unit": column.get("unit"),
            "owner": None,
            "version": 1,
            "requires_tables": None,
            "aliases": [],
            "time_column": None,
            "window": None,
            "default_aggregation": None,
            "deprecated": False,
        }
        suggested = _suggestions(column, chart_labels, observations)
        suggested = {"id": candidate_id, **suggested}
        proposals.append({
            "proposal_id": f"p-{len(proposals) + 1:02d}",
            "status": "pending_human_review",
            "applied": False,
            "action": "add_metric",
            "reason": (
                f"结果列「{name}」是字典里没有的口径（columns[].source_metric_id 与 trace[].metric_id 同为 null，"
                "答案里对应 caveat METRIC_NOT_IN_DICT）。这些数字能追溯，但没有口径定义可对 —— "
                "要不要立成字典条目、怎么定义，只能由口径负责人拍。"
            ),
            "metric": entry,
            "suggested": suggested,
            "observations": observations,
            "checks": _proposal_checks(entry, observations, candidate_id),
            "pending_fields": [
                key for key in ("id", "name", "definition", "owner", "requires_tables", "default_aggregation")
                if entry.get(key) in (None, [])
            ],
        })

    # 已用字典口径的自检：契约 2.3 要求 definition_sha256 == sha256(definition)；
    # 契约 3.3 第 3 条（与字典当前版本逐字相等）需要字典文件，show/ 不读 dict/，标 pending。
    used = []
    for item in answer.get("metrics_used") or []:
        if not isinstance(item, dict):
            continue
        definition = item.get("definition")
        claimed = item.get("definition_sha256")
        actual = sha256_text(str(definition)) if definition is not None else None
        if actual is None or claimed is None:
            matches = None
        else:
            # 允许占位/截断写法（fixture 里就是这么写的）：两侧任一是对方的前缀即算对得上
            left, right = actual.lower(), str(claimed).lower()
            matches = left.startswith(right) or right.startswith(left)
        if matches is False:
            problems.append(
                f"metrics_used 里「{item.get('id')}」的 definition_sha256 与 definition 的哈希对不上"
                "（契约 2.3 要求它必须是 definition 的 sha256；示意数据里的占位/截断哈希也会命中这一条）"
            )
        used.append({
            "id": item.get("id"),
            "version": item.get("version"),
            "name": item.get("name"),
            "unit": item.get("unit"),
            "definition_sha256_matches": matches,
            "sha256_of_definition": actual,
            "current_dict_compare": {
                "status": "pending",
                "detail": "契约 3.3 第 3 条要求与字典当前版本逐字相等（不等则须记 DICT_VERSION_DRIFT）；"
                          "show/ 不读 dict/，这一条留给人审",
            },
        })

    for column in columns:
        metric_id = column.get("source_metric_id")
        if metric_id is None:
            continue
        if metric_id not in {item.get("id") for item in answer.get("metrics_used") or [] if isinstance(item, dict)}:
            problems.append(
                f"列「{column.get('name')}」声明 source_metric_id「{metric_id}」，但 metrics_used 里没有这条快照"
                "（契约 3.3 第 2 条过不去）"
            )

    return {
        "contract_version": (answer.get("meta") or {}).get("contract_version") or "1.0",
        "kind": "metric_dict_proposal",
        "generated_by": "show/export.py",
        "note": (
            "提案制：本文件只是一份**待人审阅**的提案，一个字段都还没进字典。show/ 从不读写 dict/metrics.json，"
            "也不自动生效 —— 契约 3.2 V12 要求整份字典一次性校验、任一条失败即整份加载失败，"
            "合并与校验只能在 dict/ 里由人做。另：契约第 3 节定义了字典格式与校验规则，"
            "**没有**定义「提案文件」的形状，下面的字段名尽量贴着 3.1 走，多出来的 "
            "proposal_id / suggested / observations / checks 只属于这份提案，不是契约字段。"
        ),
        "answer": {
            "answer_id": answer.get("answer_id"),
            "question": answer.get("question"),
            "status": answer.get("status"),
            "metric_dict_sha256": (answer.get("meta") or {}).get("metric_dict_sha256"),
        },
        "proposals": proposals,
        "used_metrics": used,
        "problems": problems,
        "summary": {
            "proposals": len(proposals),
            "applied": False,
            "pending_fields": sorted({
                field for proposal in proposals for field in proposal.get("pending_fields") or []
            }),
            "next_step": "人工在 dict/ 里补齐 definition / owner / requires_tables，跑 dict/validate.py（契约 3.2 V1–V12），"
                         "全部通过后整份字典一次性生效",
        },
    }


# --------------------------------------------------------------------------
# 产物 4：report.md
# --------------------------------------------------------------------------


def section_header(answer: dict) -> list[str]:
    meta = answer.get("meta") or {}
    cache = meta.get("cache") or {}
    options = meta.get("options") or {}
    lines = [f"# {md_prose(answer.get('plan_summary') or answer.get('question') or 'answer.json')}", ""]
    note = answer.get("_fixture_note")
    if note is not None and str(note).strip():
        lines += ["> **示意数据 · 免责声明**：" + md_prose(str(note).strip()), ""]
    lines += [
        f"> 问：{md_prose(answer.get('question'))}",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| status | {md_code(answer.get('status'))} |",
        f"| answer_id | {md_code(answer.get('answer_id'))} |",
        f"| 契约版本 | {md_code('v' + str(meta.get('contract_version')))} |",
        f"| 缓存 | {'命中' if cache.get('hit') else '未命中'} · source_answer_id {md_code(cache.get('source_answer_id'))} |",
        f"| 字典哈希 | {md_code(meta.get('metric_dict_sha256'))} |",
        f"| row_cap / trace_row_id_cap | {md_code(options.get('row_cap'))} / {md_code(options.get('trace_row_id_cap'))} |",
        "",
    ]
    return lines


def section_plan(answer: dict) -> list[str]:
    plan = [step for step in answer.get("plan") or [] if isinstance(step, dict)]
    if not plan:
        return ["## 计划", "", "计划是空的（契约 2.1：`status=\"clarify\"` 时允许为空数组）。", ""]
    lines = ["## 计划", ""]
    for step in plan:
        needs = step.get("needs") or {}
        lines.append(
            f"{md_code('step ' + str(step.get('step_id')))} · intent {md_code(step.get('intent'))} · "
            f"{md_prose(step.get('title'))}"
        )
        lines.append(
            f"  - 指标：{md_prose('、'.join(map(str, needs.get('metric_ids') or [])) or '无')}；"
            f"维度：{md_prose('、'.join(map(str, needs.get('dimensions') or [])) or '无')}；"
            f"sql_index：{md_code(step.get('sql_index'))}；"
            f"depends_on：{md_prose(step.get('depends_on') or [])}；"
            f"fallback_of：{md_code(step.get('fallback_of'))}"
        )
    lines.append("")
    return lines


def section_clarify(answer: dict) -> list[str]:
    clarify = answer.get("clarify")
    if not isinstance(clarify, dict):
        return []
    lines = [
        "## 需要你先确认一句",
        "",
        f"> {md_prose(clarify.get('question'))}",
        "",
        "| 槽位 | 为什么缺它就算不出来 | 候选（id · 名称 · 定义） |",
        "|---|---|---|",
    ]
    for item in clarify.get("missing") or []:
        item = item or {}
        candidates = []
        for candidate in item.get("candidates") or []:
            candidate = candidate or {}
            candidates.append(
                f"{md_prose(candidate.get('id') if candidate.get('id') is not None else 'null')} · "
                f"{md_prose(candidate.get('name'))} · {md_prose(candidate.get('definition'))}"
            )
        lines.append(
            f"| {md_cell(item.get('slot'))} | {md_cell(item.get('why_needed'))} | "
            f"{md_cell('；'.join(candidates) or '没有候选')} |"
        )
    lines += ["", f"反问发生在 {md_code(clarify.get('asked_at_step'))}（契约 7.1：一次最多 3 个槽位）。", ""]
    return lines


def section_error(answer: dict) -> list[str]:
    error = answer.get("error")
    if not isinstance(error, dict):
        return []
    lines = [
        "## 这次没能算出结果",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| code | {md_code(error.get('code'))} |",
        f"| stage | {md_code(error.get('stage'))} |",
        f"| message（原始错误，未改写） | {md_cell(error.get('message'))} |",
        f"| readable_hint | {md_cell(error.get('readable_hint'))} |",
        "",
    ]
    attempts = [item or {} for item in error.get("attempts") or []]
    if attempts:
        lines += [
            "### 每次尝试",
            "",
            "| # | 回退到的决策 | sql_index | error_code | error_message |",
            "|---|---|---|---|---|",
        ]
        for attempt in attempts:
            lines.append(
                f"| {md_cell(attempt.get('attempt'))} | {md_cell(attempt.get('source_decision'))} | "
                f"{md_cell(attempt.get('sql_index'))} | {md_cell(attempt.get('error_code'))} | "
                f"{md_cell(attempt.get('error_message'))} |"
            )
        lines.append("")
        for attempt in attempts:
            snapshot = attempt.get("d3_snapshot")
            if snapshot is None:
                continue
            lines += [f"`attempt {attempt.get('attempt')}` 的选表选列快照（`d3_snapshot`，原样保留）：", "",
                      md_json(snapshot), ""]
    else:
        lines += ["没有 attempt 记录。", ""]
    return lines


def section_narrative(answer: dict, by_id: dict, columns: dict) -> list[str]:
    items = [item or {} for item in answer.get("narrative") or []]
    if not items:
        return []
    lines = ["## 结论", ""]
    for item in items:
        template = str(item.get("text_template") or "")
        pieces = []
        cursor = 0
        for match in render_mod.PLACEHOLDER.finditer(template):
            pieces.append(md_prose(template[cursor:match.start()]))
            trace = by_id.get(match.group(1))
            if trace is None:
                pieces.append(f"**未知追溯 {md_prose(match.group(1))}**")
            else:
                pieces.append(f"**{md_prose(render_mod.trace_text(trace, columns))}**（{md_code(match.group(1))}）")
            cursor = match.end()
        pieces.append(md_prose(template[cursor:]))
        evidence = "、".join(md_code(tid) for tid in item.get("evidence") or []) or "空"
        lines += [
            f"- {''.join(pieces)}",
            f"  - claim_kind：{md_code(item.get('claim_kind'))} · 证据：{evidence}",
        ]
    lines += ["", "句子里每个数字都来自它后面那个 trace_id，没有一个裸数字（契约 2.8）。", ""]
    return lines


def section_table(answer: dict, columns: dict) -> list[str]:
    cols = [column for column in answer.get("columns") or [] if isinstance(column, dict)]
    rows = list(answer.get("rows") or [])
    if not cols:
        return []
    head = ["| # | " + " | ".join(column_header(column) for column in cols) + " | __rid |",
            "|---" * (len(cols) + 2) + "|"]
    body = []
    for index, row in enumerate(rows):
        cells = [str(index)]
        for column in cols:
            cells.append(md_cell(cell_text(row.get(column.get("name")), column)))
        cells.append(md_cell(row.get("__rid")))
        body.append("| " + " | ".join(cells) + " |")
    lines = [f"## 结果表（{len(rows)} 行 × {len(cols)} 列）", ""]
    if not rows:
        lines += ["0 行：结果集是空的。空不是错（契约 7.4），但也不许为了好看就放宽条件重查。", ""]
    lines += head + body + [""]
    return lines


def section_charts(answer: dict, plans: list[dict], written: list[dict]) -> list[str]:
    if not plans:
        return []
    files = {item.get("chart_id"): item for item in written}
    lines = ["## 图表", ""]
    for plan in plans:
        chart = plan["chart"]
        chart_id = chart.get("chart_id")
        title = chart.get("title") or chart_id
        kind = chart.get("type")
        info = files.get(chart_id) or {}
        lines.append(f"### {md_code(chart_id)} · {md_prose(kind)} · {md_prose(title)}")
        lines.append("")
        if info.get("file"):
            lines.append(f"![{md_prose(title)}]({info['file']})")
        else:
            lines.append(f"未生成 SVG：{md_prose(info.get('note') or '没有说明')}")
        lines.append("")
        x_spec = chart.get("x") or {}
        lines.append(f"- x：{md_code(x_spec.get('field'))}（{md_prose(x_spec.get('label'))}，{md_prose(x_spec.get('type'))}）")
        for spec in chart.get("y") or []:
            spec = spec or {}
            lines.append(
                f"- y：{md_code(spec.get('field'))}（{md_prose(spec.get('label'))}，"
                f"{md_prose(spec.get('unit') if spec.get('unit') is not None else 'null')}，"
                f"{md_prose(spec.get('digits'))} 位小数）"
            )
        sort = chart.get("sort") or {}
        by = sort.get("by")
        order_word = "降序" if sort.get("desc") else "升序"
        hint = "（沿用结果表顺序）" if by == "__row" else ""
        lines.append(f"- 排序：{md_prose(by)} {order_word}{hint}")
        traces = "、".join(md_code(tid) for tid in chart.get("source_trace_ids") or []) or "无"
        lines.append(f"- 用到的追溯：{traces}")
        lines.append(f"- 备注：{md_prose(chart.get('note')) if chart.get('note') else 'null'}")
        if plan["skipped"]:
            skipped = "；".join(
                f"{md_code(item['value_path'])}（{md_prose(item['reason'])}）" for item in plan["skipped"]
            )
            lines.append(f"- 没画的点：{skipped}")
        lines.append("")
    lines += ["图里没有数据值，也不产生新数字：点一律从结果表取、从追溯链找解释（契约 2.7）。", ""]
    return lines


def section_trace(answer: dict, columns: dict) -> list[str]:
    traces = [trace for trace in answer.get("trace") or [] if isinstance(trace, dict)]
    if not traces:
        return ["## 追溯链", "", "没有 trace：`N = 0`，追溯完整率记 null（契约 4.4），不写 0 也不写 100%。", ""]
    lines = [
        f"## 追溯链（{len(traces)} 条 · 一个输出数字一条）",
        "",
        "| trace | 指向 | 值 | metric_id | 源文件 | sheet | 命中行数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for trace in traces:
        source = trace.get("source") or {}
        hits = (
            f"{trace.get('row_ids_total')}（已截断，只列 {len(trace.get('row_ids') or [])} 条）"
            if trace.get("row_ids_truncated") else str(trace.get("row_ids_total"))
        )
        lines.append(
            f"| {md_cell(trace.get('trace_id'))} | {md_cell(trace.get('value_path'))} | "
            f"{md_cell(render_mod.trace_text(trace, columns))} | "
            f"{md_cell(trace.get('metric_id') if trace.get('metric_id') is not None else 'null')} | "
            f"{md_cell(source.get('file'))} | "
            f"{md_cell(source.get('sheet') if source.get('sheet') is not None else 'null')} | {md_cell(hits)} |"
        )
    lines.append("")
    for trace in traces:
        source = trace.get("source") or {}
        filter_value = trace.get("filter")
        if isinstance(filter_value, dict) and filter_value.get("none") is True:
            filter_text = "none（不过滤）"
        else:
            filter_text = json.dumps(filter_value, ensure_ascii=False, default=str)
        steps = []
        for step in trace.get("aggregation") or []:
            step = step or {}
            bit = str(step.get("op"))
            if step.get("over"):
                bit += f" over {step.get('over')}"
            if step.get("group_by"):
                bit += " by " + ",".join(map(str, step.get("group_by")))
            steps.append(bit)
        lines += [
            f"### {md_code(trace.get('trace_id'))} → {md_code(trace.get('value_path'))}",
            "",
            f"- 值：{md_prose(render_mod.trace_text(trace, columns))}"
            f"（与结果表那一格严格相等，契约 4.3 T8）",
            f"- 源：{md_code(source.get('file'))} · sheet {md_code(source.get('sheet') if source.get('sheet') is not None else 'null')}"
            f" · 表头行 {md_code(source.get('header_row') if source.get('header_row') is not None else 'null')}"
            f" · 文件哈希 {md_code(source.get('file_sha256'))}",
            f"- 用到的列：{'、'.join(md_code(name) for name in source.get('columns') or []) or '空'}",
            f"- 过滤：{md_prose(filter_text)}",
            f"- 表达式：{md_code(trace.get('expression'))}",
            f"- 聚合：{md_prose('；'.join(steps) or '空数组（明细指标必须写 identity，空数组算不完整）')}",
            f"- 命中行：共 {md_prose(trace.get('row_ids_total'))} 行，这里列出 {md_prose(len(trace.get('row_ids') or []))} 条 = "
            f"{'、'.join(md_code(rid) for rid in trace.get('row_ids') or []) or '空'}"
            f"{'（已按 trace_row_id_cap 截断）' if trace.get('row_ids_truncated') else ''}",
            f"- sql_index：{md_code(trace.get('sql_index'))}",
            "",
        ]
    return lines


def section_caveats(answer: dict) -> list[str]:
    items = [item or {} for item in answer.get("caveats") or []]
    if not items:
        return ["## 必须声明的限制", "", "本次没有需要声明的限制（`caveats` 为空）。", ""]
    lines = [
        "## 必须声明的限制",
        "",
        "| code | 说明 | 影响字段 | 影响追溯 |",
        "|---|---|---|---|",
    ]
    for item in items:
        affects = item.get("affects") or {}
        lines.append(
            f"| {md_cell(item.get('code'))} | {md_cell(item.get('message'))} | "
            f"{md_cell('、'.join(map(str, affects.get('fields') or [])) or '无')} | "
            f"{md_cell('、'.join(map(str, affects.get('trace_ids') or [])) or '无')} |"
        )
    lines += ["", "`caveats` 的 code 是封闭集（契约 2.9），一条都不许藏。", ""]
    return lines


def section_sql(answer: dict) -> list[str]:
    statements = list(answer.get("sql") or [])
    if not statements:
        return ["## 实际执行的取数语句", "",
                "`sql` 是空数组：`status=\"clarify\"` / `\"error\"` 时按契约 2.13 必须为空"
                "（错误体里失败过的语句在 `error.attempts[].sql_index`，不在 `sql[]` 里）。", ""]
    lines = ["## 实际执行的取数语句", "", "原文照抄 `answer.json` 的 `sql[]`，未改写、未重新生成：", ""]
    for index, statement in enumerate(statements):
        lines += [f"`sql[{index}]`", "", "```text", str(statement), "```", ""]
    return lines


def section_checks(checks: list[render_mod.Check]) -> list[str]:
    passed = sum(1 for check in checks if check.ok)
    lines = [
        f"## 契约自检（{passed}/{len(checks)} 通过）",
        "",
        "只读 answer.json 做比对，不重算任何数字。",
        "",
        "| 检查 | 结论 | 说明 |",
        "|---|---|---|",
    ]
    for check in checks:
        lines.append(
            f"| {md_cell(check.id)} · {md_cell(check.title)} | "
            f"{'通过' if check.ok else '**不通过**'} | {md_cell('无问题' if check.ok else check.detail)} |"
        )
    lines.append("")
    return lines


def section_proposal(proposal: dict, path_name: str) -> list[str]:
    summary = proposal.get("summary") or {}
    lines = [
        "## 指标字典提案（提案制）",
        "",
        f"本次另写了一份 {md_code(path_name)}：{md_prose(summary.get('proposals'))} 条待人工审阅的提案，"
        "`applied` 恒为 false。",
        "",
        "- show/ **不读写** `dict/metrics.json`，提案只是提案，不会自动生效；"
        "契约 3.2 V12 要求整份字典一次性校验，合并只能在 `dict/` 里由人做。",
        "- definition / owner / requires_tables 这些字段 show/ **不代拟**（README 铁律 3：口径不许自创），"
        "提案里留 null 并在 `checks` 里标 pending。",
    ]
    for item in proposal.get("proposals") or []:
        metric = item.get("metric") or {}
        observations = item.get("observations") or {}
        lines += [
            "",
            f"### {md_code(item.get('proposal_id'))} · {'立成字典条目' if item.get('action') == 'add_metric' else md_prose(item.get('action'))}"
            f" · 列 {md_code((observations.get('column') or {}).get('name'))}",
            "",
            f"- 为什么提：{md_prose(item.get('reason'))}",
            f"- 涉及的值：{'、'.join(md_code(path) for path in observations.get('value_paths') or [])}",
            f"- 涉及追溯：{'、'.join(md_code(tid) for tid in observations.get('trace_ids') or [])}",
            f"- 现成表达式：{md_code(observations.get('expression'))}（照抄自 trace，不是字典定义）",
            f"- 待填字段：{md_prose('、'.join(item.get('pending_fields') or []) or '无')}",
            f"- 建议值（只是建议）：{md_prose(json.dumps(item.get('suggested') or {}, ensure_ascii=False, default=str))}",
        ]
        failed = [check for check in item.get("checks") or [] if check.get("status") == "fail"]
        lines.append(f"- 校验：{'有 ' + str(len(failed)) + ' 条不过，见 dict_proposal.json' if failed else '在 show/ 里没有判出问题（多数规则要字典与 schema 才能判）'}")
    problems = proposal.get("problems") or []
    if problems:
        lines += ["", "### 顺带发现的答案侧问题", ""]
        lines += [f"- {md_prose(text)}" for text in problems]
    lines.append("")
    return lines


def footer_lines(answer: dict, written_files: list[str]) -> list[str]:
    meta = answer.get("meta") or {}
    return [
        "---",
        "",
        "由 `show/export.py` 生成：数字全部取自 answer.json，未重算；报告是纯 Markdown，不含 HTML 与脚本。"
        f" 契约 v{md_prose(meta.get('contract_version'))}。",
        "",
        "本次写出：" + "、".join(md_code(name) for name in written_files),
        "",
    ]


def build_report(answer: dict, checks: list[render_mod.Check], plans: list[dict],
                 written: list[dict], proposal: dict, written_files: list[str]) -> str:
    columns = render_mod.columns_by_name(answer)
    by_id, _ = render_mod.trace_maps(answer)
    lines: list[str] = []
    lines += section_header(answer)
    lines += section_plan(answer)
    lines += section_clarify(answer)
    lines += section_error(answer)
    lines += section_narrative(answer, by_id, columns)
    lines += section_table(answer, columns)
    lines += section_charts(answer, plans, written)
    lines += section_trace(answer, columns)
    lines += section_caveats(answer)
    lines += section_sql(answer)
    lines += section_checks(checks)
    lines += section_proposal(proposal, PROPOSAL_NAME)
    lines += footer_lines(answer, written_files)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def export_all(answer_path: Path, out_dir: Path, *, write_svgs: bool = True) -> dict:
    try:
        answer = render_mod.load_answer(answer_path)
    except render_mod.ShowError as exc:
        raise ExportError(str(exc)) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    answer_out, copied = export_answer(answer_path, out_dir)

    columns = render_mod.columns_by_name(answer)
    _, by_path = render_mod.trace_maps(answer)
    plans = [
        render_mod.plan_chart(answer, chart, columns, by_path)
        for chart in (answer.get("charts") or [])
        if isinstance(chart, dict)
    ]
    written_charts = export_charts(answer, plans, out_dir) if write_svgs else []

    proposal = build_proposal(answer)
    proposal_path = out_dir / PROPOSAL_NAME
    write_text(proposal_path, json.dumps(proposal, ensure_ascii=False, indent=2) + "\n")

    checks = render_mod.run_checks(answer, plans)
    written_files = [ANSWER_NAME] + [str(item["file"]) for item in written_charts if item.get("file")] + [
        REPORT_NAME, PROPOSAL_NAME,
    ]
    report_path = out_dir / REPORT_NAME
    write_text(report_path, build_report(answer, checks, plans, written_charts, proposal, written_files))

    return {
        "answer": answer_out,
        "copied": copied,
        "report": report_path,
        "proposal": proposal_path,
        "charts": written_charts,
        "checks": checks,
        "proposal_count": len(proposal.get("proposals") or []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m show.export",
        description="导出 answer.json 副本 + report.md + 每个图一份 SVG，并写一份不自动生效的字典提案。",
    )
    parser.add_argument("--answer", required=True, help="answer.json 路径（契约第 2 节）")
    parser.add_argument("--out", required=True, help="输出目录（如 show/out/）")
    parser.add_argument("--no-svg", action="store_true", help="只导 answer.json / report.md / 提案，不写 SVG")
    parser.add_argument("--strict", action="store_true", help="契约自检不通过时以退出码 1 结束")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    if out_dir.exists() and not out_dir.is_dir():
        print(f"show.export: --out 要一个目录，收到的是文件：{out_dir.as_posix()}", file=sys.stderr)
        return 2

    try:
        result = export_all(Path(args.answer), out_dir, write_svgs=not args.no_svg)
    except ExportError as exc:
        print(f"show.export: {exc}", file=sys.stderr)
        return 2

    checks = result["checks"]
    failed = [check for check in checks if not check.ok]
    print(f"show.export: {result['answer'].as_posix()}"
          f"{'（原样拷贝）' if result['copied'] else '（输入即输出，未重复写）'}")
    for item in result["charts"]:
        if item.get("file"):
            print(f"show.export: 图 {item['chart_id']} → {item['file']}")
        else:
            print(f"show.export: 图 {item['chart_id']} 未导出：{item['note']}")
    print(f"show.export: {result['report'].as_posix()}")
    print(f"show.export: {result['proposal'].as_posix()}（{result['proposal_count']} 条提案，待人工审阅，未生效）")
    print(f"show.export: 契约自检 {len(checks) - len(failed)}/{len(checks)} 通过")
    for check in failed:
        print(f"  - {check.id} {check.title}：{check.detail}")
    if failed and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
