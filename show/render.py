"""show/render.py —— 把一份 answer.json 渲染成自包含 HTML：结果表 + SVG 图 + 追溯面板。

边界（全部来自 contract/answer.md，别处不猜）：
  * 只吃 answer.json —— 不读源表格、不连数据库、不 import ask/、不看别的目录。
  * **不重算任何数字**：表里的值原样取自 `rows[]`；图上的点原样取自 `rows[]`；
    句子里的数字只能出自 `narrative[].text_template` 的 `{{trace:...}}` 占位符，
    替换成 `trace[].value`。`charts[]` 里本来就没有数据值（第 2.7 节），
    所以「图上的数」和「表里的数」一定同源。
  * 不新增契约字段：必填字段缺失或体内字段互相矛盾 ⇒ 抛 ShowError 停下，
    不补默认值、不发明约定。
  * 输出 HTML 不含 `<script>`、不联网、不含时间戳 —— 同一份 answer.json
    两次渲染结果字节级一致（第 6.1 节）。

用法：
  python -m show.render --answer show/fixtures/answer.sample.json --out show/out/index.html
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path

from show import svg

# 契约 2.1 的必填顶层字段（顺序即契约声明顺序）
REQUIRED_TOP = (
    "question",
    "answer_id",
    "status",
    "plan",
    "plan_summary",
    "metrics_used",
    "sql",
    "columns",
    "rows",
    "trace",
    "charts",
    "narrative",
    "caveats",
    "clarify",
    "error",
    "meta",
)

STATUSES = ("ok", "clarify", "error")
INTENTS = ("distribution", "trend", "compare", "anomaly", "rank", "detail")
CHART_TYPES = ("bar", "line", "scatter")
CLAIM_KINDS = ("fact", "compare", "limit")
NUMERIC_TYPES = ("int", "decimal")

PLACEHOLDER = re.compile(r"\{\{trace:(t-[0-9]{4})\}\}")
ARABIC_DIGIT = re.compile(r"[0-9]")
CELL_PATH = re.compile(r"^rows\[(\d+)\]\.(.+)$")

# 单位白名单（契约 3.1）→ 人读后缀；ratio / none 不加后缀
UNIT_LABEL = {
    "CNY": " 元",
    "%": "%",
    "人": " 人",
    "次": " 次",
    "个": " 个",
    "天": " 天",
    "ratio": "",
    "none": "",
}


class ShowError(Exception):
    """answer.json 不可渲染（缺字段 / 形状不对）。渲染器不猜。"""


# --------------------------------------------------------------------------
# 读入与数字格式化
# --------------------------------------------------------------------------


def load_answer(path: Path) -> dict:
    if not path.is_file():
        raise ShowError(f"找不到 answer 文件：{path.name}（契约 2 节要求一份 answer.json）")
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:  # pragma: no cover - 环境问题
        raise ShowError(f"读不了 {path.name}：{exc}") from exc
    try:
        # parse_float=Decimal：契约 6.2 禁止用二进制 float 碰数据数字
        data = json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ShowError(
            f"{path.name} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ShowError("answer.json 顶层必须是对象（契约 2.1）")
    missing = [name for name in REQUIRED_TOP if name not in data]
    if missing:
        raise ShowError("answer.json 缺必填字段：" + "、".join(missing) + "（契约 2.1）")
    status = data.get("status")
    if status not in STATUSES:
        raise ShowError(f"status 必须是 ok / clarify / error，收到 {status!r}（契约 2.1）")
    for key in ("columns", "rows", "trace", "charts", "narrative", "caveats", "plan", "sql"):
        if not isinstance(data.get(key), list):
            raise ShowError(f"{key} 必须是数组，收到 {type(data.get(key)).__name__}（契约 2.1）")
    for index, column in enumerate(data["columns"]):
        if not isinstance(column, dict) or "name" not in column:
            raise ShowError(f"columns[{index}] 必须是含 name 的对象（契约 2.4）")
    return data


def to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _group_digits(digits_part: str) -> str:
    out = ""
    for offset, char in enumerate(reversed(digits_part)):
        if offset and offset % 3 == 0:
            out = "," + out
        out = char + out
    return out


def unit_suffix(unit: object) -> str:
    if unit is None:
        return ""
    return UNIT_LABEL.get(str(unit), f" {unit}")


def format_number(value: object, *, digits: int = 0, unit: object = None) -> str:
    """按 columns[].digits 定点输出（ROUND_HALF_EVEN，无科学计数法，无 -0）。"""
    number = to_decimal(value)
    if number is None:
        return "null"
    places = max(0, int(digits))
    quant = Decimal(1).scaleb(-places)
    rounded = number.quantize(quant, rounding=ROUND_HALF_EVEN)
    if rounded == 0:  # 禁止 -0
        rounded = abs(rounded)
    text = format(rounded, "f")
    if "E" in text or "e" in text:  # 极端量级兜底，仍然定点
        text = f"{rounded:.{places}f}"
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    integer_part, _, frac_part = text.partition(".")
    integer_part = _group_digits(integer_part)
    out = integer_part + ("." + frac_part if frac_part else "")
    return ("-" if negative else "") + out + unit_suffix(unit)


def display_value(value: object, column: dict | None) -> str:
    if value is None:
        return "null"
    if column:
        kind = column.get("type")
        if kind in NUMERIC_TYPES:
            return format_number(value, digits=column.get("digits") or 0, unit=column.get("unit"))
        if kind == "bool":
            return "true" if value else "false"
    return str(value)


def columns_by_name(answer: dict) -> dict:
    out = {}
    for column in answer.get("columns") or []:
        if isinstance(column, dict) and column.get("name") is not None:
            out[column["name"]] = column
    return out


def trace_maps(answer: dict) -> tuple[dict, dict]:
    by_id = {}
    by_path = {}
    for trace in answer.get("trace") or []:
        if not isinstance(trace, dict):
            continue
        by_id[trace.get("trace_id")] = trace
        by_path[trace.get("value_path")] = trace
    return by_id, by_path


def trace_text(trace: dict, columns: dict) -> str:
    path = str(trace.get("value_path") or "")
    match = CELL_PATH.match(path)
    column = columns.get(match.group(2)) if match else None
    if column is not None:
        return display_value(trace.get("value"), column)
    return format_number(trace.get("value"))


def anchor_id(value_path: object) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", str(value_path or ""))
    return "cell-" + slug.strip("-")


# --------------------------------------------------------------------------
# 图：把 charts[] 变成可画的数据（只搬运，不算）
# --------------------------------------------------------------------------


def _sort_token(value: object):
    if value is None:
        return None
    number = to_decimal(value)
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool) and number is not None:
        return (0, number)
    return (1, str(value))


def plan_chart(answer: dict, chart: dict, columns: dict, by_path: dict) -> dict:
    """把一条 charts[] 描述解析成绘制数据。

    规矩（契约 2.7）：图里的每个点都必须有 trace，否则**该点不许画**。
    这里把画不出来的点记进 `skipped`，页面上一并列出，不静默丢。
    """
    rows = answer.get("rows") or []
    x_spec = chart.get("x") or {}
    y_specs = [item for item in (chart.get("y") or []) if isinstance(item, dict)]
    x_field = x_spec.get("field")
    allowed = set(chart.get("source_trace_ids") or [])

    sort = chart.get("sort") or {}
    sort_by = sort.get("by", "__row")
    descending = bool(sort.get("desc"))
    order = list(range(len(rows)))
    if sort_by in ("x", "y") and rows and (x_field or y_specs):
        field = x_field if sort_by == "x" else (y_specs[0].get("field") if y_specs else x_field)
        keyed = [(i, _sort_token(rows[i].get(field))) for i in order]
        known = [(token, i) for i, token in keyed if token is not None]
        unknown = [i for i, token in keyed if token is None]
        known.sort(key=lambda item: item[0], reverse=descending)
        order = [i for _, i in known] + unknown

    drawn = []
    skipped = []
    for index in order:
        row = rows[index]
        cells = {}
        for spec in y_specs:
            field = spec.get("field")
            path = f"rows[{index}].{field}"
            cell_value = row.get(field)
            trace = by_path.get(path)
            if cell_value is None:
                skipped.append({"value_path": path, "reason": "该单元格是 null（null 表示没有，不是 0）"})
                continue
            if trace is None:
                skipped.append({"value_path": path, "reason": "没有对应的 trace，按契约 2.7 不画这个点"})
                continue
            if trace.get("trace_id") not in allowed:
                skipped.append({
                    "value_path": path,
                    "reason": f"trace {trace.get('trace_id')} 不在 source_trace_ids 里，按契约 2.7 不画这个点",
                })
                continue
            cells[field] = cell_value
        if not cells:
            continue
        drawn.append({"index": index, "row": row, "cells": cells})

    x_column = columns.get(x_field) if x_field else None
    categories = [display_value(item["row"].get(x_field), x_column) for item in drawn]
    series = []
    for spec in y_specs:
        field = spec.get("field")
        digits = spec.get("digits")
        if digits is None and field in columns:
            digits = columns[field].get("digits")
        digits = int(digits or 0)
        unit = spec.get("unit")
        if unit is None and field in columns:
            unit = columns[field].get("unit")
        series.append({
            "field": field,
            "label": spec.get("label") or field,
            "unit": unit,
            "digits": digits,
            "values": [item["cells"].get(field) for item in drawn],
            "labels": [
                (None if item["cells"].get(field) is None
                 else format_number(item["cells"].get(field), digits=digits, unit=unit))
                for item in drawn
            ],
        })

    points = []
    if chart.get("type") == "scatter":
        y_field = y_specs[0].get("field") if y_specs else None
        for item in drawn:
            x_number = to_decimal(item["row"].get(x_field))
            y_number = to_decimal(item["cells"].get(y_field)) if y_field else None
            if x_number is None or y_number is None:
                skipped.append({
                    "value_path": f"rows[{item['index']}].{x_field}",
                    "reason": "散点的 x 不是数值列，按契约 2.7 不画这个点",
                })
                continue
            points.append({
                "x": x_number,
                "y": y_number,
                "x_text": display_value(item["row"].get(x_field), x_column),
                "y_text": display_value(item["cells"].get(y_field), columns.get(y_field)),
                "label": " · ".join(
                    part for part in (
                        display_value(item["row"].get(x_field), x_column),
                        (format_number(item["cells"].get(y_field),
                                       digits=series[0]["digits"] if series else 0,
                                       unit=series[0]["unit"] if series else None)),
                    ) if part
                ),
            })

    return {
        "chart": chart,
        "categories": categories,
        "series": series,
        "points": points,
        "skipped": skipped,
        "x_label": x_spec.get("label") or str(x_field or ""),
    }


# --------------------------------------------------------------------------
# 契约自检（只读、只比对；不重算任何数字）
# --------------------------------------------------------------------------


class Check:
    def __init__(self, check_id: str, title: str, problems: list[str]):
        self.id = check_id
        self.title = title
        self.problems = problems

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def detail(self) -> str:
        return "；".join(self.problems) if self.problems else "通过"


def run_checks(answer: dict, plans: list[dict]) -> list[Check]:
    checks: list[Check] = []
    status = answer.get("status")
    columns = columns_by_name(answer)
    traces = [t for t in (answer.get("trace") or []) if isinstance(t, dict)]
    by_id, by_path = trace_maps(answer)
    rows = answer.get("rows") or []

    # C2 三选一体与 clarify / error 的互斥（2.1 / 2.10 / 2.11）
    problems = []
    if status == "clarify":
        if not isinstance(answer.get("clarify"), dict):
            problems.append('status="clarify" 但 clarify 不是对象（契约 2.10）')
        if answer.get("error") is not None:
            problems.append('status="clarify" 时 error 必须是 null（契约 2.13）')
    else:
        if answer.get("clarify") is not None:
            problems.append(f'status="{status}" 时 clarify 必须是 null（契约 2.1）')
    if status == "error":
        if not isinstance(answer.get("error"), dict):
            problems.append('status="error" 但 error 不是对象（契约 2.11）')
    elif answer.get("error") is not None:
        problems.append(f'status="{status}" 时 error 必须是 null（契约 2.13）')
    checks.append(Check("C1", "体互斥：clarify / error 只在对应 status 下出现", problems))

    # C2 降级体硬清单（2.13）
    problems = []
    if status == "clarify":
        for key in ("sql", "rows", "columns", "trace", "charts", "narrative"):
            if answer.get(key):
                problems.append(f'status="clarify" 时 {key} 必须为空（契约 2.13，实际 {len(answer[key])} 条）')
    if status == "error":
        for key in ("rows", "trace", "charts", "narrative"):
            if answer.get(key):
                problems.append(f'status="error" 时 {key} 必须为空（契约 2.13，实际 {len(answer[key])} 条）')
    checks.append(Check("C2", "降级体硬清单：该为空的确实为空", problems))

    # C3 |trace| == N（4.1）：N = int/decimal 单元格数
    problems = []
    numeric_fields = [c["name"] for c in answer.get("columns") or [] if c.get("type") in NUMERIC_TYPES]
    expected = len(rows) * len(numeric_fields)
    if status == "ok" and len(traces) != expected:
        problems.append(f"数值单元格 {expected} 个，trace {len(traces)} 条，不相等（契约 4.1）")
    checks.append(Check("C3", "一个输出数字一条 trace（|trace| == N）", problems))

    # C4 T8：trace.value 与 value_path 指向处严格相等
    problems = []
    for trace in traces:
        path = str(trace.get("value_path") or "")
        match = CELL_PATH.match(path)
        if not match:
            problems.append(f"{trace.get('trace_id')} 的 value_path「{path}」不是 rows[i].列名 形态")
            continue
        index, field = int(match.group(1)), match.group(2)
        if index >= len(rows):
            problems.append(f"{trace.get('trace_id')} 指向 rows[{index}]，但只有 {len(rows)} 行")
            continue
        left = to_decimal(trace.get("value"))
        right = to_decimal(rows[index].get(field))
        if left is None or right is None:
            problems.append(f"{trace.get('trace_id')} 的 value 或 rows[{index}].{field} 不是数值")
        elif left != right:
            problems.append(f"{trace.get('trace_id')}.value 与 rows[{index}].{field} 不相等")
    checks.append(Check("C4", "T8：trace.value 与表格里那一格严格相等", problems))

    # C5 图：字段必须存在、每个点都要有 trace（2.7）
    problems = []
    for plan in plans:
        chart = plan["chart"]
        chart_id = chart.get("chart_id")
        if chart.get("type") not in CHART_TYPES:
            problems.append(f"{chart_id} 的 type「{chart.get('type')}」不在 bar/line/scatter 内")
        x_field = (chart.get("x") or {}).get("field")
        if x_field not in columns:
            problems.append(f"{chart_id} 的 x.field「{x_field}」不是 columns[].name")
        for spec in chart.get("y") or []:
            if (spec or {}).get("field") not in columns:
                problems.append(f"{chart_id} 的 y.field「{(spec or {}).get('field')}」不是 columns[].name")
        unknown = [tid for tid in (chart.get("source_trace_ids") or []) if tid not in by_id]
        if unknown:
            problems.append(f"{chart_id} 的 source_trace_ids 有不存在的 trace：{', '.join(map(str, unknown))}")
        for item in plan["skipped"]:
            problems.append(f"{chart_id} 没画 {item['value_path']}：{item['reason']}")
    checks.append(Check("C5", "图的每个点都有 trace、字段都真的存在（2.7）", problems))

    # C6 narrative：占位符 ⊆ evidence ⊆ trace，且人话里没有裸阿拉伯数字（2.8 / 5.1）
    problems = []
    for position, item in enumerate(answer.get("narrative") or []):
        item = item or {}
        template = str(item.get("text_template") or "")
        evidence = list(item.get("evidence") or [])
        refs = PLACEHOLDER.findall(template)
        missing_evidence = [tid for tid in refs if tid not in evidence]
        if missing_evidence:
            problems.append(
                f"narrative[{position}] 引用了 {', '.join(missing_evidence)}，但 evidence 里没有（契约 2.8）"
            )
        unknown = [tid for tid in evidence if tid not in by_id]
        if unknown:
            problems.append(f"narrative[{position}] 的 evidence 含不存在的 trace：{', '.join(unknown)}")
        if not evidence:
            problems.append(f"narrative[{position}] 的 evidence 为空（契约 2.8 要求非空）")
        if item.get("claim_kind") not in CLAIM_KINDS:
            problems.append(f"narrative[{position}] 的 claim_kind「{item.get('claim_kind')}」不在枚举内")
        if item.get("claim_kind") == "compare" and len(evidence) < 2:
            problems.append(f"narrative[{position}] 是 compare，但 evidence 少于 2 条（契约 5.1）")
        bare = PLACEHOLDER.sub("", template)
        if ARABIC_DIGIT.search(bare):
            problems.append(f"narrative[{position}] 里出现没被 {{{{trace:...}}}} 包裹的阿拉伯数字（契约 2.8）")
    checks.append(Check("C6", "解读句：数字只许来自占位符、证据齐全", problems))

    # C7 plan 形状（2.2）
    problems = []
    plan = answer.get("plan") or []
    sql = answer.get("sql") or []
    seen = []
    for item in plan:
        item = item or {}
        step_id = item.get("step_id")
        if not isinstance(step_id, int):
            problems.append(f"step_id 不是整数：{step_id!r}")
            continue
        if step_id in seen:
            problems.append(f"step_id {step_id} 重复")
        seen.append(step_id)
        if item.get("intent") not in INTENTS:
            problems.append(f"step {step_id} 的 intent「{item.get('intent')}」不在六选一内")
        for dependency in item.get("depends_on") or []:
            if not isinstance(dependency, int) or dependency >= step_id:
                problems.append(f"step {step_id} 依赖了不更小的 step_id {dependency!r}")
        sql_index = item.get("sql_index")
        # 错误体里 sql 恒为 []（契约 2.15 的注），此时 plan 仍可能指向「试过的语句」，
        # 所以只在 status="ok" 上校验下标落在 sql[] 内。
        if (
            answer.get("status") == "ok"
            and sql_index is not None
            and (not isinstance(sql_index, int) or not 0 <= sql_index < len(sql))
        ):
            problems.append(f"step {step_id} 的 sql_index {sql_index!r} 越界（sql 有 {len(sql)} 条）")
    if plan and seen and seen != list(range(1, len(seen) + 1)):
        problems.append(f"step_id 必须是 1..{len(seen)} 连续升序，实际 {seen}")
    checks.append(Check("C7", "计划：step_id 连续、依赖只往回指、sql_index 不越界", problems))

    return checks


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

CSS = """
:root{--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--bg:#f6f8fb;--card:#ffffff;
--accent:#2f6fed;--ok:#15803d;--warn:#b45309;--bad:#b91c1c}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 64px;background:var(--bg);color:var(--ink);
font-family:system-ui,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.55}
main{max-width:1120px;margin:0 auto}
h1{font-size:22px;margin:0 0 6px}
h2{font-size:16px;margin:0 0 12px;display:flex;gap:10px;align-items:baseline}
h2 small{font-weight:400;color:var(--muted);font-size:12px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:18px 20px;margin:16px 0}
.lede{color:var(--muted);margin:0 0 14px;font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 0}
.chip{font-size:12px;padding:3px 9px;border-radius:999px;background:#eef2ff;
border:1px solid #dbe3ff;color:#28306b;font-family:ui-monospace,Consolas,monospace}
.chip.ok{background:#ecfdf5;border-color:#bbf7d0;color:#15803d}
.chip.warn{background:#fffbeb;border-color:#fde68a;color:#b45309}
.chip.bad{background:#fef2f2;border-color:#fecaca;color:#b91c1c}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
thead th{background:#f8fafc;font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;
font-family:ui-monospace,Consolas,monospace}
td.idx{color:var(--muted);text-align:right;width:40px}
td .t{display:block;font-weight:400;color:var(--muted);font-size:11px}
.null{color:#a3aebf;font-style:italic}
.notrace{background:#fffbeb}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.tl{margin-left:6px;font-size:10px;font-family:ui-monospace,Consolas,monospace;
color:var(--muted);border-bottom:1px dotted var(--muted)}
code,pre{font-family:ui-monospace,Consolas,monospace;font-size:12px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;background:#f8fafc;
border:1px solid var(--line);border-radius:6px;padding:8px 10px}
figure{margin:0 0 22px}
figcaption{font-size:12px;color:var(--muted);margin-top:6px}
.trace{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 12px}
.trace h3{margin:0 0 8px;font-size:14px;font-family:ui-monospace,Consolas,monospace}
.trace table.kv{font-size:12px}
.trace table.kv th{width:190px;background:#f8fafc;font-weight:600;
font-family:ui-monospace,Consolas,monospace;color:#334155}
.trace table.kv th,.trace table.kv td{border:none;border-bottom:1px solid #f1f5f9}
.pill{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;
background:#f1f5f9;border:1px solid var(--line);margin:0 4px 4px 0;
font-family:ui-monospace,Consolas,monospace}
.quote{margin:0 0 12px;padding:10px 14px;border-left:3px solid var(--accent);
background:#f8fafc;font-size:14px}
.check{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid #f1f5f9;font-size:13px}
.check:last-child{border-bottom:none}
.check .mark{width:20px;flex:none;font-weight:700}
.check .ok{color:var(--ok)}
.check .bad{color:var(--bad)}
.check .body{flex:1}
.check .detail{color:var(--muted);font-size:12px;word-break:break-word}
ul.narrative{margin:0;padding-left:20px}
ul.narrative li{margin:0 0 8px}
.muted{color:var(--muted)}
.fixture-note{background:#fff7ed;border:2px solid #f59e0b;border-left-width:8px;
border-radius:10px;padding:14px 18px;margin:0 0 18px;color:#7c2d12}
.fixture-note .label{display:inline-block;font-weight:700;font-size:12px;
letter-spacing:.4px;background:#b45309;color:#fff;border-radius:6px;
padding:2px 9px;margin-bottom:8px}
.fixture-note p{margin:0;font-size:15px;font-weight:600;line-height:1.6}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:24px}
""".strip()


def esc(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def code(text: object) -> str:
    return f"<code>{esc(text)}</code>"


def short_hex(value: object, keep: int = 12) -> str:
    text = "" if value is None else str(value)
    if len(text) <= keep:
        return code(text)
    return f'<code title="{esc(text)}">{esc(text[:keep])}…</code>'


def null_span(note: str = "") -> str:
    hint = f' <span class="muted">（{esc(note)}）</span>' if note else ""
    return f'<span class="null">null</span>{hint}'


def document(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)}</title>\n<style>\n{CSS}\n</style>\n</head>\n"
        f"<body>\n<main>\n{body}</main>\n</body>\n</html>\n"
    )


def banner_section(answer: dict) -> str:
    """页面顶部的免责声明横幅。

    触发条件只有一个：answer.json 顶层带 `_fixture_note`（这不是契约 2.1 的字段，
    是我们的 fixture 自己带的标注）。真实答案没有这个键 ⇒ 一个字都不显示。
    有就**原样**摆出来，一个字不改、不折叠、不放页脚 —— 它的全部作用就是
    「别把这些数字当成实测结果」，藏起来等于没写。
    """
    note = answer.get("_fixture_note")
    if note is None:
        return ""
    text = str(note).strip()
    if not text:
        return ""
    return (
        '<aside class="fixture-note" id="fixture-note" role="note">'
        '<span class="label">示意数据 · 免责声明</span>'
        f"<p>{esc(text)}</p>"
        "</aside>"
    )


def header_section(answer: dict) -> str:
    status = answer.get("status")
    badge = {"ok": "chip ok", "clarify": "chip warn", "error": "chip bad"}.get(status, "chip")
    meta = answer.get("meta") or {}
    cache = meta.get("cache") or {}
    title = answer.get("plan_summary") or answer.get("question") or "answer.json"
    if status == "clarify":
        title = "需要你先确认一句"
    elif status == "error":
        title = "这次没能算出结果"
    chips = [
        f'<span class="{badge}">status = {esc(status)}</span>',
        f'<span class="chip">answer_id {esc(answer.get("answer_id"))}</span>',
        f'<span class="chip">契约 v{esc(meta.get("contract_version"))}</span>',
    ]
    if cache:
        chips.append(
            f'<span class="chip">cache {"命中" if cache.get("hit") else "未命中"}'
            f' · source_answer_id {esc(cache.get("source_answer_id"))}</span>'
        )
    if meta.get("metric_dict_sha256"):
        chips.append(f'<span class="chip">字典 {esc(str(meta.get("metric_dict_sha256"))[:12])}…</span>')
    return (
        "<section>\n"
        f"<h1>{esc(title)}</h1>\n"
        f'<blockquote class="quote">问：{esc(answer.get("question"))}</blockquote>\n'
        f'<div class="chips">{"".join(chips)}</div>\n'
        "</section>"
    )


def clarify_section(answer: dict) -> str:
    clarify = answer.get("clarify")
    if not isinstance(clarify, dict):
        return ""
    parts = [
        "<section>\n<h2>反问<small>契约 2.10 · 一次最多 3 个槽位</small></h2>",
        f'<blockquote class="quote">{esc(clarify.get("question"))}</blockquote>',
        '<table><thead><tr><th>槽位</th><th>为什么缺它算不出来</th><th>候选（id / 名称 / 定义）</th></tr></thead><tbody>',
    ]
    for item in clarify.get("missing") or []:
        candidates = []
        for candidate in (item or {}).get("candidates") or []:
            candidate = candidate or {}
            candidates.append(
                f'<span class="pill">{esc(candidate.get("id") if candidate.get("id") is not None else "null")}'
                f' · {esc(candidate.get("name"))}</span>'
                f'<div class="muted">{esc(candidate.get("definition"))}</div>'
            )
        parts.append(
            f'<tr><td>{esc((item or {}).get("slot"))}</td>'
            f'<td>{esc((item or {}).get("why_needed"))}</td>'
            f'<td>{"".join(candidates) or null_span("没有候选")}</td></tr>'
        )
    parts.append("</tbody></table>")
    parts.append(f'<p class="lede">反问发生在 {esc(clarify.get("asked_at_step"))}。</p>')
    parts.append("</section>")
    return "\n".join(parts)


def error_section(answer: dict) -> str:
    error = answer.get("error")
    if not isinstance(error, dict):
        return ""
    rows = [
        ("code", code(error.get("code"))),
        ("stage", code(error.get("stage"))),
        ("message（原始错误，未改写）", f"<pre>{esc(error.get('message'))}</pre>"),
        ("readable_hint（给用户看的一遍）", f'<div>{esc(error.get("readable_hint"))}</div>'),
    ]
    body = "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in rows)

    attempts = error.get("attempts") or []
    attempt_parts = []
    if attempts:
        head = (
            "<tr><th>#</th><th>回退到的决策</th><th>sql_index</th><th>error_code</th>"
            "<th>error_message</th><th>d3_snapshot</th></tr>"
        )
        cells = []
        for attempt in attempts:
            attempt = attempt or {}
            snapshot = attempt.get("d3_snapshot")
            snapshot_html = (
                f"<pre>{esc(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))}</pre>"
                if snapshot is not None else null_span("没留快照")
            )
            cells.append(
                f'<tr><td>{esc(attempt.get("attempt"))}</td>'
                f'<td>{esc(attempt.get("source_decision"))}</td>'
                f'<td>{esc(attempt.get("sql_index"))}</td>'
                f'<td>{esc(attempt.get("error_code"))}</td>'
                f'<td>{esc(attempt.get("error_message"))}</td>'
                f"<td>{snapshot_html}</td></tr>"
            )
        attempt_parts.append(f'<table><thead>{head}</thead><tbody>{"".join(cells)}</tbody></table>')
    else:
        attempt_parts.append('<p class="lede">没有 attempt 记录。</p>')

    return (
        "<section>\n<h2>错误<small>契约 2.11</small></h2>\n"
        f'<table class="kv"><tbody>{body}</tbody></table>\n'
        f'<h2 style="margin-top:18px">每次尝试<small>回退顺序与当时的选表选列快照</small></h2>\n'
        f'{"".join(attempt_parts)}\n</section>'
    )


def table_section(answer: dict, columns: dict, by_path: dict) -> str:
    cols = answer.get("columns") or []
    rows = answer.get("rows") or []
    if not cols:
        return ""
    head = ['<th class="idx">#</th>']
    for column in cols:
        unit = column.get("unit")
        type_line = esc(column.get("type"))
        if column.get("type") in NUMERIC_TYPES:
            type_line += f" · {esc(column.get('digits'))} 位"
        if unit is not None:
            type_line += f" · {esc(unit)}"
        if column.get("source_metric_id"):
            type_line += f" · 字典 {esc(column.get('source_metric_id'))}"
        elif column.get("source_column"):
            type_line += f" · 源列 {esc(column.get('source_column'))}"
        head.append(f'<th>{esc(column.get("name"))}<span class="t">{type_line}</span></th>')
    head.append("<th>__rid</th>")

    body = []
    for index, row in enumerate(rows):
        cells = [f'<td class="idx">{index}</td>']
        for column in cols:
            name = column.get("name")
            value = row.get(name)
            if column.get("type") in NUMERIC_TYPES:
                path = f"rows[{index}].{name}"
                cell_id = anchor_id(path)
                if value is None:
                    cells.append(f'<td class="num" id="{esc(cell_id)}">{null_span("没有值")}</td>')
                    continue
                text = display_value(value, column)
                trace = by_path.get(path)
                if trace is not None:
                    cells.append(
                        f'<td class="num" id="{esc(cell_id)}">{esc(text)}'
                        f'<a class="tl" href="#{esc(trace.get("trace_id"))}" '
                        f'title="追溯 {esc(trace.get("trace_id"))}">{esc(trace.get("trace_id"))}</a></td>'
                    )
                else:
                    cells.append(
                        f'<td class="num notrace" id="{esc(cell_id)}">{esc(text)}'
                        f'<span class="tl" title="没有任何 trace 指向这一格">无追溯</span></td>'
                    )
            elif value is None:
                cells.append(f'<td>{null_span("没有值")}</td>')
            else:
                cells.append(f"<td>{esc(display_value(value, column))}</td>")
        cells.append(f'<td><code>{esc(row.get("__rid"))}</code></td>')
        body.append("<tr>" + "".join(cells) + "</tr>")

    if not rows:
        note = '<p class="lede">0 行：结果集是空的（空不是错，契约 7.4）。</p>'
    else:
        note = ""
    return (
        f'<section>\n<h2>结果表<small>{len(rows)} 行 × {len(cols)} 列 · '
        f'点数字右边的 t-xxxx 可跳到它的追溯链</small></h2>\n{note}'
        f'<table><thead><tr>{"".join(head)}</tr></thead><tbody>{"".join(body)}</tbody></table>\n'
        "</section>"
    )


def chart_section(answer: dict, plans: list[dict], columns: dict) -> str:
    if not answer.get("charts"):
        return ""
    figures = []
    for plan in plans:
        chart = plan["chart"]
        chart_id = chart.get("chart_id")
        title = chart.get("title") or chart_id
        kind = chart.get("type")
        y_label = " / ".join(
            f'{spec.get("label") or spec.get("field")}'
            + (f'（{spec.get("unit")}）' if spec.get("unit") else "")
            for spec in plan["series"]
        )
        if kind == "bar":
            markup = svg.bar_chart(
                title=title, categories=plan["categories"], series=plan["series"],
                x_label=plan["x_label"], y_label=y_label,
            )
        elif kind == "line":
            markup = svg.line_chart(
                title=title, categories=plan["categories"], series=plan["series"],
                x_label=plan["x_label"], y_label=y_label,
            )
        elif kind == "scatter":
            markup = svg.scatter_chart(
                title=title, points=plan["points"],
                x_label=plan["x_label"], y_label=y_label,
            )
        else:
            markup = f'<p class="lede">未知图型 {esc(kind)}，按契约 2.7 只有 bar / line / scatter。</p>'

        traces = "".join(
            f'<a class="pill" href="#{esc(tid)}">{esc(tid)}</a>'
            for tid in (chart.get("source_trace_ids") or [])
        )
        sort = chart.get("sort") or {}
        extra = [
            f'<figcaption>{esc(chart_id)} · {esc(kind)} · 排序 {esc(sort.get("by"))}'
            f'{" 降序" if sort.get("desc") else " 升序"}'
            + (f' · 备注：{esc(chart.get("note"))}' if chart.get("note") else "")
            + f'<br>用到的追溯：{traces}</figcaption>'
        ]
        if plan["skipped"]:
            skipped = "；".join(f'{esc(item["value_path"])}（{esc(item["reason"])}）' for item in plan["skipped"])
            extra.append(f'<p class="lede">没画的点：{skipped}</p>')
        figures.append(f"<figure>{markup}{''.join(extra)}</figure>")
    return (
        '<section>\n<h2>图表<small>纯 SVG · 值来自结果表，解释来自追溯链</small></h2>\n'
        + "\n".join(figures)
        + "\n</section>"
    )


def narrative_section(answer: dict, by_id: dict, columns: dict) -> str:
    items = answer.get("narrative") or []
    if not items:
        return ""
    rendered = []
    for item in items:
        item = item or {}
        template = str(item.get("text_template") or "")
        pieces = []
        cursor = 0
        for match in PLACEHOLDER.finditer(template):
            pieces.append(esc(template[cursor:match.start()]))
            trace = by_id.get(match.group(1))
            if trace is None:
                pieces.append(f'<span class="chip bad">未知追溯 {esc(match.group(1))}</span>')
            else:
                pieces.append(
                    f'<a class="num" href="#{esc(match.group(1))}">'
                    f'{esc(trace_text(trace, columns))}</a>'
                )
            cursor = match.end()
        pieces.append(esc(template[cursor:]))
        evidence = "".join(
            f'<a class="pill" href="#{esc(tid)}">{esc(tid)}</a>' for tid in (item.get("evidence") or [])
        )
        rendered.append(
            f'<li><span class="chip">{esc(item.get("claim_kind"))}</span>{"".join(pieces)}'
            f'<div class="lede" style="margin:4px 0 0">证据：{evidence or null_span("空")}</div></li>'
        )
    return (
        '<section>\n<h2>解读<small>句子里的数字全部来自 trace，没有裸数字</small></h2>\n'
        f'<ul class="narrative">{"".join(rendered)}</ul>\n</section>'
    )


def caveat_section(answer: dict) -> str:
    items = answer.get("caveats") or []
    if not items:
        return ""
    rows = []
    for item in items:
        item = item or {}
        affects = item.get("affects") or {}
        fields = "".join(f'<span class="pill">{esc(name)}</span>' for name in affects.get("fields") or [])
        trace_ids = "".join(
            f'<a class="pill" href="#{esc(tid)}">{esc(tid)}</a>' for tid in affects.get("trace_ids") or []
        )
        rows.append(
            f'<tr><td>{code(item.get("code"))}</td><td>{esc(item.get("message"))}</td>'
            f'<td>{fields or null_span("无")}</td><td>{trace_ids or null_span("无")}</td></tr>'
        )
    return (
        '<section>\n<h2>必须声明的限制<small>契约 2.9 · 封闭枚举，一条不许藏</small></h2>\n'
        "<table><thead><tr><th>code</th><th>说明</th><th>影响字段</th><th>影响追溯</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table>\n</section>'
    )


def trace_section(answer: dict, columns: dict, caveat_codes: set) -> str:
    traces = [t for t in (answer.get("trace") or []) if isinstance(t, dict)]
    if not traces:
        return ""
    sql = answer.get("sql") or []
    cards = []
    for trace in traces:
        trace_id = str(trace.get("trace_id"))
        source = trace.get("source") or {}
        path = str(trace.get("value_path"))
        header_row = source.get("header_row")
        if header_row is None:
            header_note = null_span(
                "推断不出表头行" + ("，已记 HEADER_ROW_GUESSED" if "HEADER_ROW_GUESSED" in caveat_codes else "")
            )
        else:
            header_note = code(header_row)

        filter_value = trace.get("filter")
        if isinstance(filter_value, dict) and filter_value.get("none") is True:
            filter_html = f'{code("none")} <span class="muted">（不过滤）</span>'
        else:
            filter_html = f"<pre>{esc(json.dumps(filter_value, ensure_ascii=False, default=str))}</pre>"

        aggregation = trace.get("aggregation") or []
        aggregation_html = "".join(
            f'<span class="pill">{esc((step or {}).get("op"))}'
            + (f' over {esc((step or {}).get("over"))}' if (step or {}).get("over") else "")
            + (
                " by " + esc(",".join(map(str, (step or {}).get("group_by") or [])))
                if (step or {}).get("group_by") else ""
            )
            + "</span>"
            for step in aggregation
        ) or null_span("空数组算不完整（契约 4.2）")

        row_ids = [str(rid) for rid in trace.get("row_ids") or []]
        if trace.get("row_ids_truncated"):
            total = trace.get("row_ids_total")
            ids_note = (
                f'<span class="chip warn">已截断：命中 {esc(total)} 行，只列前 {len(row_ids)} 条'
                f'（caveat TRACE_ROW_IDS_TRUNCATED 必须在 caveats 里）</span>'
            )
        else:
            ids_note = f'<span class="muted">命中 {esc(trace.get("row_ids_total"))} 行</span>'
        id_chips = "".join(f'<span class="pill">{esc(rid)}</span>' for rid in row_ids)

        sql_index = trace.get("sql_index")
        sql_text = (
            sql[sql_index]
            if isinstance(sql_index, int) and 0 <= sql_index < len(sql)
            else None
        )
        sql_html = (
            f"<pre>{esc(sql_text)}</pre>" if sql_text is not None
            else null_span(f"sql[{sql_index}] 不存在")
        )

        metric_id = trace.get("metric_id")
        rows_kv = [
            ("value_path", f'{code(path)} <a class="tl" href="#{esc(anchor_id(path))}">定位到表格</a>'),
            ("value", f'<span class="num">{esc(trace_text(trace, columns))}</span>'),
            ("metric_id", code(metric_id) if metric_id is not None
             else null_span("非字典口径不许填近似 id")),
            ("source.file", code(source.get("file"))),
            ("source.file_sha256", short_hex(source.get("file_sha256"))),
            ("source.sheet", code(source.get("sheet")) if source.get("sheet") is not None
             else null_span("CSV 源的 sheet 就是 null")),
            ("source.header_row", header_note),
            ("source.columns", "".join(f'<span class="pill">{esc(name)}</span>'
                                       for name in source.get("columns") or []) or null_span("空")),
            ("filter", filter_html),
            ("expression", f"<pre>{esc(trace.get('expression'))}</pre>"),
            ("aggregation", aggregation_html),
            ("sql_index", f'{code(sql_index)}<div style="margin-top:6px">{sql_html}</div>'),
            ("row_ids", f'{ids_note}<div style="margin-top:6px">{id_chips}</div>'),
        ]
        body = "".join(f"<tr><th>{esc(key)}</th><td>{value}</td></tr>" for key, value in rows_kv)
        cards.append(
            f'<div class="trace" id="{esc(trace_id)}"><h3>{esc(trace_id)}</h3>'
            f'<table class="kv"><tbody>{body}</tbody></table></div>'
        )
    return (
        f'<section>\n<h2>追溯面板<small>{len(traces)} 条 trace · 一个输出数字一条（契约第 4 节）'
        f'</small></h2>\n' + "\n".join(cards) + "\n</section>"
    )


def check_section(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        mark = '<span class="ok">✓</span>' if check.ok else '<span class="bad">✗</span>'
        lines.append(
            f'<div class="check"><div class="mark">{mark}</div>'
            f'<div class="body">{esc(check.id)} · {esc(check.title)}'
            f'<div class="detail">{esc(check.detail)}</div></div></div>'
        )
    passed = sum(1 for check in checks if check.ok)
    return (
        f'<section>\n<h2>契约自检<small>{passed}/{len(checks)} 通过 · '
        f"只读 answer.json 做比对，不重算任何数字</small></h2>\n"
        + "".join(lines)
        + "\n</section>"
    )


def footer_section(answer: dict) -> str:
    meta = answer.get("meta") or {}
    options = meta.get("options") or {}
    return (
        "<footer>由 show/render.py 生成（纯 SVG，无脚本、不联网）· "
        f'契约 contract/answer.md v{esc(meta.get("contract_version"))} · '
        f'row_cap {esc(options.get("row_cap"))} · '
        f'trace_row_id_cap {esc(options.get("trace_row_id_cap"))}</footer>'
    )


def render_page(answer: dict, checks: list[Check]) -> str:
    columns = columns_by_name(answer)
    by_id, by_path = trace_maps(answer)
    caveat_codes = {item.get("code") for item in (answer.get("caveats") or []) if isinstance(item, dict)}
    plans = [
        plan_chart(answer, chart, columns, by_path)
        for chart in (answer.get("charts") or [])
        if isinstance(chart, dict)
    ]
    body = [
        banner_section(answer),
        header_section(answer),
        clarify_section(answer),
        error_section(answer),
        table_section(answer, columns, by_path),
        chart_section(answer, plans, columns),
        narrative_section(answer, by_id, columns),
        caveat_section(answer),
        trace_section(answer, columns, caveat_codes),
        check_section(checks),
        footer_section(answer),
    ]
    return document(answer.get("plan_summary") or answer.get("question") or "answer.json",
                    "\n".join(part for part in body if part))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m show.render",
        description="把 answer.json 渲染成自包含 HTML（结果表 + SVG 图 + 追溯面板）；不重算任何数字。",
    )
    parser.add_argument("--answer", required=True, help="answer.json 路径（契约第 2 节）")
    parser.add_argument("--out", required=True, help="输出 HTML 路径")
    parser.add_argument("--strict", action="store_true", help="契约自检不通过时以退出码 1 结束")
    args = parser.parse_args(argv)

    try:
        answer = load_answer(Path(args.answer))
    except ShowError as exc:
        print(f"show.render: {exc}", file=sys.stderr)
        return 2

    columns = columns_by_name(answer)
    _, by_path = trace_maps(answer)
    plans = [
        plan_chart(answer, chart, columns, by_path)
        for chart in (answer.get("charts") or [])
        if isinstance(chart, dict)
    ]
    checks = run_checks(answer, plans)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_page(answer, checks)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(html_text)

    failed = [check for check in checks if not check.ok]
    print(f"show.render: 写出 {out_path.as_posix()}（{len(html_text)} 字符）")
    print(f"show.render: 契约自检 {len(checks) - len(failed)}/{len(checks)} 通过")
    for check in failed:
        print(f"  - {check.id} {check.title}：{check.detail}")
    if failed and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
