"""ask.sqlgen · 把 D2 的步骤 + D3 的选表选列变成一条条只读 SELECT（契约 §2.2 / §2.4 / §2.6 / §6.3）

契约出处（`contract/answer.md`）：
- §2.2 `plan[]`：一个 step 最多挂一条 SQL（要两条就拆两步）
- §2.4 `columns[]`：`name/type/unit/digits/source_metric_id/source_column`
- §2.6 `expression` 用指标 `definition` 的**逐字拷贝**（T7 要求 `sql[]` 里能找到这个子串）
- §6.3 行序稳定：SQL 必须带 `ORDER BY`，且排序必须**全序**
- §8.1 S5/S6：表名列名一律走白名单 + 双引号包裹

本模块只产出 SQL 文本与列元数据，不执行、不拼 answer.json。
执行与追溯链（行号、row_ids）在 `ask.execute`。

**契约未写清、本模块自己定的两条（报告里要报）**
1. **时间范围落在哪一列**：§5.1 给了 D1 的 `time_range`，但没说它过滤哪一列。
   本实现的规则（写死、可解释、不猜）：① 该步用的指标若声明了 `time_column` 且该列在当前表里，
   就用它；② 否则用当前表里**唯一一个** `date` 类型的列；③ 找不到或有多个 → `COLUMN_NOT_FOUND`
   （宁可停下，也不悄悄忽略时间范围——忽略会答成「所有年份」）。
2. **多步时 answer.json 的 `rows` 取哪一步**：§2.1 只有一份 `rows`/`columns`，装不下多个不同形状
   的结果表。本实现取**最后一步**（D2 示例里最后一步就是「按区域汇总成交额」那一步），
   所有执行过的 SQL 都进 `sql[]`，每步的 `sql_index` 各自指认。

**多表 JOIN（#12）怎么拼**：列 → 表 → 等值键的推导在 `ask.joins`；本模块只负责把它落成 SQL：

```
SELECT "region_name" AS "region_name", SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) AS "gmv"
FROM (SELECT "sales"."amount" AS "amount", "sales"."status" AS "status",
             "region_dim"."region_name" AS "region_name",
             CAST("sales"."__src_rowid" AS TEXT) AS "__src_rowid"
      FROM "sales" INNER JOIN "region_dim" ON "sales"."region" = "region_dim"."region") AS "__join"
GROUP BY "region_name" ORDER BY "region_name" ASC, "gmv" ASC
```

外层的列名都写在子查询里投影成同名，所以指标定义可以**逐字**出现（§4.3 T7 要求子串），
`ON` 里的键一律带表名限定；两层加起来既没有歧义，也不用改 `static_check` 的白名单。

"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .decide import DecisionError, DELTA_SUFFIX, PCT_SUFFIX, PERIOD_COLUMN, TIME_GRAINS
from .joins import JoinSource, plan_source
from .metric_dict import Metric, MetricDict, check_metric_use, metric_columns
from .plan_rules import Shape
from .schema import Column, Table, format_decimal_plain
from .static_check import check_identifier, identifiers, literal, quote_ident

# 单位 → 输出小数位（§2.4 的 `digits` 是**本实现**选的；契约只要求「格式化按这个数走」）
UNIT_DIGITS = {"CNY": 2, "%": 2, "ratio": 4, "none": 2, "人": 0, "次": 0, "个": 0, "天": 0}

# 计数类单位 → 输出成 int（§2.4：int 的 digits 必须是 0）
INT_UNITS = ("人", "次", "个", "天")

# 明细行排序时用的稳定行号列（§6.3.1 的 `__src_rowid`）
SRC_ROWID = "__src_rowid"

# JOIN 后的取数源别名（子查询里已经把各表需要的列投影成同名，外层就不用再带表名前缀）
JOIN_ALIAS = "__join"

# 反向条件的「右表键」在子查询里的列别名（§2.5：右表没有这一行就是「不存在」）
ANTI_ALIAS = "__anti_key"

_DIM_OP = "="


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class OutColumn:
    """§2.4 的列定义 + 内部需要的上下文（指标对象、参与计算的原始列）。"""

    name: str
    type: str
    unit: str | None
    digits: int
    source_metric_id: str | None
    source_column: str | None
    metric: Metric | None = None
    src_columns: tuple = ()
    # §2.4 v1.1：派生列（period / <指标>__delta / <指标>__pct）
    derived: bool = False
    from_column: str | None = None  # period 才有：源时间列
    grain: str | None = None  # period 才有：粒度

    def as_answer(self) -> dict:
        """§2.4 元素，字段顺序照契约。派生列才带 `derived`（period 另带 `from` / `grain`）。"""
        out = {
            "name": self.name,
            "type": self.type,
            "unit": self.unit,
            "digits": self.digits,
            "source_metric_id": self.source_metric_id,
            "source_column": self.source_column,
        }
        if self.derived:
            out["derived"] = True
            if self.from_column is not None:
                out["from"] = self.from_column
                out["grain"] = self.grain
        return out


@dataclass
class StepPlan:
    """一步 = 一条 SQL + 列元数据 + 追溯需要的上下文。"""

    step: dict
    sql_index: int
    sql: str
    table: str
    mode: str  # "detail" | "aggregate" | "window" | "outlier"
    columns: list
    where_text: str = ""
    where_items: list = field(default_factory=list)  # [(列名, op, value_literal)]
    dimensions: list = field(default_factory=list)  # 原始维度列名（进 source.columns）
    metric_ids: list = field(default_factory=list)
    order_keys: list = field(default_factory=list)  # 结果列名（用于全序检查）
    group_by: list = field(default_factory=list)  # 结果列名：分组键（含时间桶）
    # 窗口/异常/两期对比这几类语句的分组发生在模板内部，追溯改走 `prov_sql`
    prov_sql: str = ""
    key_exprs: list = field(default_factory=list)  # 与 `group_by` 对齐的 SQL 表达式
    # 多表 JOIN（#12）：`from_sql` 是实际取数源（单表就是带引号的表名；JOIN 时是投影子查询）
    from_sql: str = ""
    joins: list = field(default_factory=list)  # 本次用到的 joins[]（d3 形状，供核对与 attempts 快照）
    join_subquery: str = ""  # `(SELECT ...) AS "__join"`，用于行放大检查
    join_filter: str = ""  # 行放大检查时先套上的过滤（反向条件要先把「右表没有」的行留下）
    join_tables: list = field(default_factory=list)  # 参与连接的表名（含桥接表），query 据此补进导入
    rid_text: bool = False  # 追溯的行标识已经是文本（JOIN 子查询里的复合 __src_rowid）

    @property
    def where_sql(self) -> str:
        return f" WHERE {self.where_text}" if self.where_text else ""

    def plan_entry(self) -> dict:
        """§2.2 `plan[]` 元素。`needs` 原样来自 D2（dimensions 是**原始列名**）。"""
        step = self.step
        return {
            "step_id": step["step_id"],
            "intent": step["intent"],
            "title": step["title"],
            "needs": {
                "metric_ids": list(step["needs"]["metric_ids"]),
                "dimensions": list(step["needs"]["dimensions"]),
            },
            "sql_index": self.sql_index,
            "depends_on": list(step.get("depends_on") or []),
            "fallback_of": step.get("fallback_of"),
        }


# --------------------------------------------------------------------------- #
# 列到 SQL 的映射
# --------------------------------------------------------------------------- #


def _table_columns(table: Table) -> list:
    return [c.name for c in table.columns]


def _column(table: Table, name: str) -> Column:
    for col in table.columns:
        if col.name == name:
            return col
    raise DecisionError(
        "COLUMN_NOT_FOUND",
        f"表 {table.sheet or table.file} 里没有列 {name!r}（有 {_table_columns(table)}）",
        stage="D3",
        readable_hint=f"「{name}」这一列在这份数据里不存在",
    )


def _dim_column(table: Table, name: str) -> OutColumn:
    """维度/原始列 → 输出列（直接映射，`source_column` 填原始列名）。

    §2.4 要求 `type="decimal"` 的列 `unit` 非空，但 §5.1 的 schema 快照里**没有任何单位信息**，
    裸列也没法从指标字典借一个（借了就是冒充口径）。所以这里填白名单里的 `"none"`
    （=「这列没有可申报的单位」），而不是 `null`，也不是编一个 `"CNY"`。**契约缺口，报告里要报。**
    """
    col = _column(table, name)
    return OutColumn(
        name=col.name,
        type=col.type,
        unit="none" if col.type == "decimal" else None,
        digits=col.digits if col.type == "decimal" else 0,
        source_metric_id=None,
        source_column=col.name,
        metric=None,
        src_columns=(col.name,),
    )


def _metric_column(metric: Metric, used_columns) -> OutColumn:
    """指标 → 输出列（`source_column` 必须为 null，§2.4）。"""
    if metric.unit in INT_UNITS:
        ctype, digits = "int", 0
    else:
        ctype, digits = "decimal", UNIT_DIGITS.get(metric.unit, 2)
    return OutColumn(
        name=metric.id,
        type=ctype,
        unit=metric.unit,
        digits=digits,
        source_metric_id=metric.id,
        source_column=None,
        metric=metric,
        src_columns=tuple(used_columns),
    )


# --------------------------------------------------------------------------- #
# 取数源（单表 / 多表 JOIN）：见 `ask.joins`
# --------------------------------------------------------------------------- #


def _source_sql(source: JoinSource, all_tables: dict) -> tuple:
    """→ `(外层 FROM 用的文本, JOIN 子查询文本)`。

    单表：文本就是带引号的表名，子查询为空。
    JOIN：`(SELECT "sales"."order_id" AS "order_id", …, CAST("sales"."__src_rowid" AS TEXT) AS "__src_rowid"
    FROM "sales" INNER JOIN "region_dim" ON …) AS "__join"`——
    各表需要的列在子查询里就投影成同名，外层照旧用裸列名写（指标定义 `SUM(amount)` 才能逐字出现，
    满足 §4.3 T7「expression 是 sql 的子串」）。连接键在 `ON` 里写全表名限定，避免同名列歧义。
    """
    if not source.joined:
        return quote_ident(source.base), ""
    selects = [
        f"{quote_ident(table)}.{quote_ident(column)} AS {quote_ident(alias)}"
        for table, column, alias in source.projected
    ]
    selects.append(
        f"CAST({quote_ident(source.base)}.{quote_ident(SRC_ROWID)} AS TEXT) AS {quote_ident(SRC_ROWID)}"
    )
    from_text = quote_ident(source.base)
    for step in source.joins:
        on = " AND ".join(
            f"{quote_ident(step.left)}.{quote_ident(left)} = {quote_ident(step.right)}.{quote_ident(right)}"
            for left, right in step.on
        )
        if step.on_extra:
            on = f"{on} AND {step.on_extra}"
        from_text += f" {step.type.upper()} JOIN {quote_ident(step.right)} ON {on}"
    subquery = f"(SELECT {', '.join(selects)} FROM {from_text})"
    return f"{subquery} AS {quote_ident(JOIN_ALIAS)}", subquery


# --------------------------------------------------------------------------- #
# 时间范围 / D1 过滤条件
# --------------------------------------------------------------------------- #


def resolve_time_column(d1: dict, metrics_used, table: Table) -> str | None:
    """时间范围落在哪一列。

    v1.1：D1 的 `time_column` 是**真源**（非 null 时只能是它，见 §5.1 D3 硬规则）；
    为 null 时才按模块 docstring 第 1 条的规则自己挑。
    """
    names = _table_columns(table)
    declared = d1.get("time_column")
    if declared:
        if declared in names:
            return declared
        raise DecisionError(
            "COLUMN_NOT_FOUND",
            f"D1 的 time_column={declared!r} 在表 {table.sheet or table.file} 里不存在（有 {names}）",
            stage="D3",
            readable_hint=f"D1 指定的时间列「{declared}」在这份数据里不存在",
        )
    time_range = d1.get("time_range")
    if time_range is None:
        return None
    for metric in metrics_used:
        if metric is not None and metric.time_column and metric.time_column in names:
            return metric.time_column
    dates = [c.name for c in table.columns if c.type == "date"]
    if len(dates) == 1:
        return dates[0]
    if not dates:
        raise DecisionError(
            "COLUMN_NOT_FOUND",
            f"问题给了时间范围 {time_range}，但表 {table.sheet or table.file} 里没有任何 date 类型的列",
            stage="D3",
            readable_hint="这份数据里找不到能按时间过滤的列；不猜，也不悄悄忽略时间范围",
        )
    raise DecisionError(
        "COLUMN_NOT_FOUND",
        f"问题给了时间范围，但表里有多个 date 列 {dates}，契约没有规定该用哪个",
        stage="D3",
        readable_hint="时间要落在哪一列不明确（契约 §5.1 未规定），请指定口径或补 time_column",
    )


def _value_literal(value) -> str:
    if isinstance(value, list):
        return "(" + ", ".join(literal(v) for v in value) + ")"
    return literal(value)


def _clause(col: str, op: str, value) -> tuple:
    """一条过滤 → `(SQL 片段, (列名, op, value_literal))`。

    v1.1：空值判断用独立的 op（`is_null` / `is_not_null`），且 §4.3 T4 要求这两个 op 的
    `value_literal` **显式写 null**（不是 `NULL` 字符串、也不是省略），所以这里返回 `None`。
    旧的「`op="="` + `value=null`」写法仍然认（v1.0 的注入文件还在用），生成 `IS NULL`。
    """
    if op == "is_null":
        return f"{quote_ident(col)} IS NULL", (col, "is_null", None)
    if op == "is_not_null":
        return f"{quote_ident(col)} IS NOT NULL", (col, "is_not_null", None)
    if value is None and op in ("=", "=="):
        return f"{quote_ident(col)} IS NULL", (col, "=", None)
    if value is None and op in ("!=", "<>"):
        return f"{quote_ident(col)} IS NOT NULL", (col, "!=", None)
    value_sql = _value_literal(value)
    if op == "in":
        return f"{quote_ident(col)} IN {value_sql}", (col, op, value_sql)
    if op == "not in":
        return f"{quote_ident(col)} NOT IN {value_sql}", (col, op, value_sql)
    if op == "like":
        return f"{quote_ident(col)} LIKE {value_sql}", (col, op, value_sql)
    return f"{quote_ident(col)} {op} {value_sql}", (col, op, value_sql)


def _time_in_where(source) -> bool:
    """时间过滤该写进外层 WHERE，还是已经写进 `LEFT JOIN … ON`。

    - 非反连接：**一律进 WHERE**（绝大多数查询走这条）。
    - 反连接（「哪些 X 没有 Y」）：若时间列落在基表，由 `_apply_anti` 标记
      `time_in_outer=True`，仍进 WHERE；若时间被放进了 ON，就别在外层再过滤一次
      （LEFT JOIN 的 ON 为假时左行仍在，外层再滤会把该保留的行滤掉）。

    这里曾经写成 `bool(source.anti and source.anti.get("time_in_outer"))`：
    对**所有非反连接查询**都算出 False，于是时间过滤整段丢失 ——
    实测 54 道带 `time_range` 的题里 47 道的 SQL 一个日期字面量都没有，
    而判分器只看结构不看过滤，所以全是绿的。
    """
    if not source.anti:
        return True
    return bool(source.anti.get("time_in_outer"))


def build_where(d1: dict, metrics_used, table: Table, *, include_time: bool = True) -> tuple:
    """→ `(where_text, [(列名, op, value_literal)])`。全部条件都写进文本，供 §2.6 `filter.where` 引用。

    `include_time=False` 用于反向条件：时间范围要落进 `LEFT JOIN … ON`（放进外层 WHERE
    会把「右表没有这一行」的行整片滤掉，那就永远答不出「没有」）。
    """
    clauses: list = []
    items: list = []

    time_col = resolve_time_column(d1, metrics_used, table) if include_time else None
    if time_col is not None and d1.get("time_range"):
        time_range = d1["time_range"]
        start, end = time_range.get("start"), time_range.get("end")
        if start:
            clauses.append(f"{quote_ident(time_col)} >= {literal(start)}")
            items.append((time_col, ">=", literal(start)))
        if end:
            clauses.append(f"{quote_ident(time_col)} <= {literal(end)}")
            items.append((time_col, "<=", literal(end)))

    for flt in d1.get("filters") or []:
        col = str(flt.get("column"))
        if col not in _table_columns(table):
            raise DecisionError(
                "COLUMN_NOT_FOUND",
                f"D1 的过滤条件用了表里没有的列 {col!r}（有 {_table_columns(table)}）",
                stage="D3",
                readable_hint=f"过滤条件里的「{col}」在这份数据里不存在",
            )
        clause, item = _clause(col, str(flt.get("op")), flt.get("value"))
        clauses.append(clause)
        items.append(item)

    return " AND ".join(clauses), items


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def build_step_plans(
    *,
    d1: dict,
    d2: dict,
    d3: dict,
    tables_by_name: dict,
    metrics: MetricDict,
    shape=None,
    question: str = "",
    pool: dict | None = None,
) -> list:
    """D2 的每个 step → 一个 `StepPlan`（一个 step 一条 SQL，§2.2）。

    `shape` 是 `ask.plan_rules` 按问题文本算出来的「D2 表达不了的那部分」
    （排名方向 / 截断条数 / 时间桶 / 窗口 / 异常判据 / 两期对比），只挂在**最后一步**上
    （§2.1 只有一份 `rows`，多步时承载答案的就是最后一步）。`shape=None` → 与第 4 步完全一致。

    `question` / `pool` 是 #12 新增的：跨表列解析要看问题里的「没有/从未」这类反向条件，
    桥接表（如 q039 的 `product_dim`）放在 `pool` 里，只有真的用上才进 `meta.files`。
    """
    steps = list(d2.get("steps") or [])
    plans: list = []
    for index, step in enumerate(steps):
        plan = _build_one(
            step=step,
            d1=d1,
            d3=d3,
            tables_by_name=tables_by_name,
            metrics=metrics,
            sql_index=index,
            shape=shape if index == len(steps) - 1 else None,
            question=question,
            pool=pool,
        )
        plans.append(plan)
    if not plans:
        raise DecisionError("INTERNAL", "D2 没有产出任何步骤（§5.1 要求 steps 非空）", stage="D2")
    return plans


# --------------------------------------------------------------------------- #
# 时间桶 / 排序 / 追溯用语句
# --------------------------------------------------------------------------- #

# grain → 桶表达式模板。桶列名固定叫 `period`（§2.4 v1.1 派生列），
# 文本格式固定：YYYY-MM-DD / YYYY-Www / YYYY-MM / YYYY-Qn / YYYY。
# `{col}` 会被换成带引号的时间列名。
_BUCKET_SPEC = {
    "day": "strftime('%Y-%m-%d', {col})",
    "week": "strftime('%Y-W%W', {col})",
    "month": "strftime('%Y-%m', {col})",
    "quarter": "strftime('%Y', {col}) || '-Q' || ((CAST(strftime('%m', {col}) AS INTEGER) + 2) / 3)",
    "year": "strftime('%Y', {col})",
}


def _bucket_time_column(d1: dict, metrics_used, table: Table) -> str:
    """时间桶落在哪一列：与 `resolve_time_column` 同一套规则，只是没有 `time_range` 也要给列。"""
    col = resolve_time_column(d1, metrics_used, table)
    if col is not None:
        return col
    names = _table_columns(table)
    for metric in metrics_used:
        if metric is not None and metric.time_column and metric.time_column in names:
            return metric.time_column
    dates = [c.name for c in table.columns if c.type == "date"]
    if len(dates) == 1:
        return dates[0]
    raise DecisionError(
        "COLUMN_NOT_FOUND",
        f"问题要求按时间粒度分组，但表 {table.sheet or table.file} 里找不到唯一的时间列（date 列 {dates}）",
        stage="D3",
        readable_hint="按时间分组要落到哪一列不明确（契约 §5.1 未规定），请补 time_column",
    )


def _widen_time_range(d1: dict, months: int) -> dict:
    """把 D1 的时间范围**起点往前挪 N 个月**（只挪起点，终点不动）。

    环比/同比算的是「本期 vs 上一期」，基期必然落在 D1 声明的区间之外；不往前捞，
    `LAG` 只能拿到区间内的空位。这里只扩**取数范围**，`answer.json` 的时间范围、
    输出行仍以 D1 为准（多出来的那一期没有基期，会被窗口题过滤掉）。
    """
    time_range = d1.get("time_range") or {}
    start = time_range.get("start")
    if not start:
        return d1
    shifted = dict(d1)
    shifted["time_range"] = {**time_range, "start": _shift_months(start, -months)}
    return shifted


def _shift_months(iso: str, months: int) -> str:
    """`2025-03-31` 往前 1 个月 → `2025-02-28`（目标月没有这一天就落到月末）。"""
    try:
        day = date.fromisoformat(iso)
    except ValueError:
        return iso
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    next_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (next_first - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day)).isoformat()


def _bucket_columns(grain: str, time_col: str) -> tuple:
    """→ `(period 列 OutColumn, 桶表达式)`。

    §2.4 v1.1 把粒度分组的派生列写死成 `period`：`type="text"`、`digits=0`、`unit=null`、
    `source_metric_id=null`、`source_column=null`、`derived=true`、`from=<时间列>`、`grain=<粒度>`。
    """
    if grain not in TIME_GRAINS:
        raise DecisionError(
            "COLUMN_NOT_FOUND",
            f"时间粒度 {grain!r} 不在 {TIME_GRAINS}（§5.1 D1 的 time_grain_grouping）",
            stage="D3",
            readable_hint="这个时间粒度不认识，不猜格式",
        )
    column = OutColumn(
        name=PERIOD_COLUMN,
        type="text",
        unit=None,
        digits=0,
        source_metric_id=None,
        source_column=None,
        metric=None,
        src_columns=(time_col,),
        derived=True,
        from_column=time_col,
        grain=grain,
    )
    return column, _BUCKET_SPEC[grain].format(col=quote_ident(time_col))


def _order_clause(order_desc, key_cols, metric_cols) -> tuple:
    """→ `(ORDER BY 文本, 排序键名列表)`。`order_desc=None` = 默认全序升序（键 + 指标）。"""
    keys = [c.name for c in key_cols]
    metrics = [c.name for c in metric_cols]
    if order_desc is None:
        pairs = [(n, "ASC") for n in keys + metrics]
    else:
        pairs = [(n, "DESC" if order_desc else "ASC") for n in metrics] + [(n, "ASC") for n in keys]
    text = ", ".join(f"{quote_ident(name)} {direction}" for name, direction in pairs)
    return text, [name for name, _direction in pairs]


def _prov_sql(key_exprs, from_sql: str, where_text: str) -> str:
    """追溯用语句：取「分组键 + 源行号」，分组在 `ask.execute` 里做（与正式 SQL 同一套键表达式）。

    `from_sql` 与正式 SQL 用的是同一个取数源：单表是带引号的表名，JOIN 之后是投影子查询
    （子查询里已经带了 `__src_rowid`，文本形式，见 `ask.execute.run_step` 的 `rid_text`）。
    """
    select = list(key_exprs) + [quote_ident(SRC_ROWID)]
    order = [f"{expr} ASC" for expr in key_exprs] + [f"{quote_ident(SRC_ROWID)} ASC"]
    where = f" WHERE {where_text}" if where_text else ""
    return f"SELECT {', '.join(select)} FROM {from_sql}{where} ORDER BY {', '.join(order)}"


def _filters_text(d1: dict, table: Table) -> str:
    """D1 的 `filters`（**不含**时间范围）→ 一段可 `AND` 起来的条件文本。"""
    parts: list = []
    for flt in d1.get("filters") or []:
        col = str(flt.get("column"))
        if col not in _table_columns(table):
            raise DecisionError(
                "COLUMN_NOT_FOUND",
                f"D1 的过滤条件用了表里没有的列 {col!r}（有 {_table_columns(table)}）",
                stage="D3",
                readable_hint=f"过滤条件里的「{col}」在这份数据里不存在",
            )
        clause, _item = _clause(col, str(flt.get("op")), flt.get("value"))
        parts.append(clause)
    return " AND ".join(parts)


def _derived_column(metric: Metric, *, unit: str | None = None) -> OutColumn:
    """派生列（环比/同比/两期增长率）：值是**百分比**，但仍挂在这个指标 id 上（口径不变）。"""
    if unit is None:
        return _metric_column(metric, metric_columns(metric))
    return OutColumn(
        name=metric.id,
        type="decimal",
        unit=unit,
        digits=2,
        source_metric_id=metric.id,
        source_column=None,
        metric=metric,
        src_columns=tuple(metric_columns(metric)),
    )


def _apply_anti(source: JoinSource, d1: dict, metric_objs, all_tables: dict) -> None:
    """反向条件：时间范围与「已付」判据落进 `LEFT JOIN … ON`，外层只留 `IS NULL`。

    写进 ON 还是外层，判据是「这一列属于哪一侧」：
    - 时间列落在被 LEFT JOIN 的那张事实表上（q033：维表为基、sales 在右侧）→ 必须写进 **ON**，
      写外层会把「右表没有这一行」的行连时间一起滤掉；
    - 时间列落在**主表**上（q038：sales 为基、payment 在右侧）→ 留在**外层 WHERE**，
      写进 ON 会让「时间不满足」的左行也保下来（LEFT JOIN 的 ON 为假时左行仍在），结果反而多算。
    """
    anti = source.anti
    fact = anti["fact"]
    on_parts: list = []
    time_range = d1.get("time_range") or {}
    anti["time_in_outer"] = False
    if time_range:
        # 时间列可能不在「存在性表」上（q038 在 sales、q033 也在 sales），
        # 按 base → fact → 其余参与表 的顺序找第一个能放下时间过滤的表
        for name in dict.fromkeys([source.base, fact] + list(source.tables)):
            try:
                time_col = resolve_time_column(d1, metric_objs, all_tables[name])
            except DecisionError:
                continue
            if not time_col:
                continue
            if name == source.base:
                anti["time_in_outer"] = True
            else:
                if time_range.get("start"):
                    on_parts.append(
                        f"{quote_ident(name)}.{quote_ident(time_col)} >= {literal(time_range['start'])}"
                    )
                if time_range.get("end"):
                    on_parts.append(
                        f"{quote_ident(name)}.{quote_ident(time_col)} <= {literal(time_range['end'])}"
                    )
            break
    predicate = anti.get("predicate")
    if predicate:
        on_parts.append(
            f"{quote_ident(fact)}.{quote_ident(predicate['column'])} = {literal(predicate['value'])}"
        )
    source.joins[0].on_extra = " AND ".join(on_parts)
    key = anti["key"]
    source.projected.append((fact, key[1], ANTI_ALIAS))
    anti["alias"] = ANTI_ALIAS


def _build_one(
    *,
    step: dict,
    d1: dict,
    d3: dict,
    tables_by_name: dict,
    metrics: MetricDict,
    sql_index: int,
    shape=None,
    question: str = "",
    pool: dict | None = None,
) -> StepPlan:
    metric_ids = list(step["needs"].get("metric_ids") or [])
    # §2.4 v1.1：`period` 是派生列（粒度分组时才出现），不是表里的列，这里先剔掉
    dimensions = [d for d in (step["needs"].get("dimensions") or []) if d != PERIOD_COLUMN]

    metric_objs = [metrics.require(mid) for mid in metric_ids]
    all_tables = {**(pool or {}), **tables_by_name}
    need_cols: list = []
    for metric in metric_objs:
        for column in metric_columns(metric):
            if column not in need_cols:
                need_cols.append(column)

    # #12：跨表列解析（主表 + 维表 + 反向条件 + 桥接表），见 `ask.joins`
    source = plan_source(
        tables_by_name,
        dimensions=dimensions,
        metric_columns=need_cols,
        metric_tables=[t for metric in metric_objs for t in metric.requires_tables],
        prefer=[t["name"] for t in d3.get("tables") or []],
        question=question,
        pool=pool,
    )
    table_name, table = source.base, all_tables[source.base]

    # V4：指标定义引用的列必须在取数源里（JOIN 之后可能来自被连接的表）
    visible = {column for _table, column, _alias in source.projected} if source.joined else set(_table_columns(table))
    for metric in metric_objs:
        missing = [column for column in metric_columns(metric) if column not in visible]
        if missing:
            check_metric_use(metric, sorted(visible))

    if source.anti:
        _apply_anti(source, d1, metric_objs, all_tables)
    from_sql, join_subquery = _source_sql(source, all_tables)

    where_text, where_items = build_where(
        d1, metric_objs, table, include_time=_time_in_where(source)
    )
    if source.anti:
        clause = f"{quote_ident(ANTI_ALIAS)} IS NULL"
        where_text = f"{where_text} AND {clause}" if where_text else clause
        where_items = list(where_items) + [(source.anti["key"][1], "is_null", None)]

    if not metric_ids:
        return _detail_plan(
            step=step,
            d1=d1,
            d3=d3,
            table_name=table_name,
            table=table,
            where_text=where_text,
            where_items=where_items,
            sql_index=sql_index,
            from_sql=from_sql,
            source=source,
            join_subquery=join_subquery,
        )

    shape = shape or Shape()
    if shape.window_compare:
        # 窗口指标（环比/同比）的基期在 D1 的区间之外：起点往前挪一期再聚合，
        # 输出仍然只剩 D1 区间里的桶（多出来的那个基期桶因为没有基期会被过滤掉）。
        back = 12 if shape.window_compare == "yoy" else 1
        where_text, where_items = build_where(
            _widen_time_range(d1, back), metric_objs, table,
            include_time=_time_in_where(source),
        )
    common = dict(
        step=step,
        sql_index=sql_index,
        table=table_name,
        where_text=where_text,
        where_items=where_items,
        dimensions=dimensions,
        metric_ids=metric_ids,
        from_sql=from_sql,
        joins=source.as_joins(),
        join_subquery=join_subquery,
        join_filter=f"{quote_ident(ANTI_ALIAS)} IS NULL" if source.anti else "",
        join_tables=list(source.tables),
        rid_text=source.rid_text,
    )

    # 分组键 = 时间桶（可选）+ 维度；键表达式同时用于 SELECT / GROUP BY / 追溯语句
    key_cols: list = []
    key_exprs: list = []
    # 粒度分组：v1.1 以 D1 的 `time_grain_grouping` 为真源（`ask.plan_rules` 的猜测只作兜底）
    grain = d1.get("time_grain_grouping") or (None if d1.get("comparison") else shape.grain)
    if grain and not d1.get("comparison") and not shape.compare:
        time_col = _bucket_time_column(d1, metric_objs, table)
        bucket_col, bucket_expr = _bucket_columns(grain, time_col)
        key_cols.append(bucket_col)
        key_exprs.append(bucket_expr)
    for dim in dimensions:
        owner = source.owner.get(dim, table_name)
        key_cols.append(_dim_column(all_tables[owner], dim))
        key_exprs.append(quote_ident(dim))

    metric_cols = [_metric_column(metric, metric_columns(metric)) for metric in metric_objs]
    base_select = [f"{expr} AS {quote_ident(col.name)}" for expr, col in zip(key_exprs, key_cols)]
    base_select += [
        f"{metric.definition} AS {quote_ident(col.name)}" for metric, col in zip(metric_objs, metric_cols)
    ]
    where_sql = f" WHERE {where_text}" if where_text else ""
    base = f"SELECT {', '.join(base_select)} FROM {from_sql}{where_sql}"
    if key_exprs:
        # §5.1 D1/D3 v1.1 要求 SQL 里出现 `GROUP BY period`：按**输出列名**分组（SQLite 认别名），
        # 这样粒度分组的语句就是 `GROUP BY "period", "region"`。
        base += " GROUP BY " + ", ".join(quote_ident(col.name) for col in key_cols)

    if d1.get("comparison"):
        return _period_compare_plan(
            d1=d1,
            metric_objs=metric_objs,
            metric_cols=metric_cols,
            table_obj=table,
            **common,
        )
    if shape.compare:
        return _compare_plan(
            shape=shape,
            metric_objs=metric_objs,
            metric_cols=metric_cols,
            table_obj=table,
            d1=d1,
            **common,
        )
    if shape.window_compare:
        return _window_plan(
            shape=shape,
            base=base,
            key_cols=key_cols,
            key_exprs=key_exprs,
            metric_objs=metric_objs,
            metric_cols=metric_cols,
            **common,
        )
    if shape.outlier:
        return _outlier_plan(
            shape=shape,
            base=base,
            key_cols=key_cols,
            key_exprs=key_exprs,
            metric_objs=metric_objs,
            metric_cols=metric_cols,
            table_obj=table,
            **common,
        )

    # --- 普通聚合（可带时间桶 / 排名 / 截断 / 去重 HAVING）---
    sql = base
    if shape.duplicate:
        sql += " HAVING COUNT(*) > 1"
    order_text, order_keys = _order_clause(shape.order_desc, key_cols, metric_cols)
    sql += " ORDER BY " + order_text
    if shape.limit is not None:
        sql += f" LIMIT {int(shape.limit)}"
    return StepPlan(
        mode="aggregate",
        sql=sql,
        columns=key_cols + metric_cols,
        order_keys=order_keys,
        group_by=[col.name for col in key_cols],
        prov_sql=_prov_sql(key_exprs, from_sql, where_text),
        key_exprs=key_exprs,
        **common,
    )


def _window_plan(*, shape, base, key_cols, key_exprs, metric_objs, metric_cols, **common) -> StepPlan:
    """环比/同比：先按桶聚合，再用 `LAG` 取上一期（同比错 12 期），最后算出变化率。"""
    metric, metric_col = metric_objs[0], metric_cols[0]
    bucket = quote_ident(key_cols[0].name)
    keys_sel = "".join(f"{quote_ident(col.name)}, " for col in key_cols)
    partition = ""
    if common["dimensions"]:
        partition = "PARTITION BY " + ", ".join(quote_ident(d) for d in common["dimensions"]) + " "
    offset = 12 if shape.window_compare == "yoy" else 1
    if shape.grain == "year":
        offset = 1  # 桶本身就是年（LAG 12 期是「按月分桶再错 12 个月」的写法）
    cur_alias, prev_alias = quote_ident("__cur"), quote_ident("__prev")
    calc = (
        f"SELECT {keys_sel}{quote_ident(metric_col.name)} AS {cur_alias}, "
        f"LAG({quote_ident(metric_col.name)}, {offset}) OVER ({partition}ORDER BY {bucket} ASC) AS {prev_alias} "
        f"FROM base"
    )
    derived = (
        f"CASE WHEN {prev_alias} IS NULL OR {prev_alias} = 0 THEN NULL "
        f"ELSE ROUND(({cur_alias} - {prev_alias}) * 100.0 / {prev_alias}, 2) END"
    )
    out_col = _derived_column(metric, unit="%")
    order_text = ", ".join(f"{quote_ident(col.name)} ASC" for col in key_cols)
    # 没有基期（第一桶/基期为 0）的桶算不出增长率：直接不进结果，免得留一格空值
    keep = f"{prev_alias} IS NOT NULL AND {prev_alias} <> 0"
    sql = (
        f"WITH base AS ({base}), calc AS ({calc}) "
        f"SELECT {keys_sel}{derived} AS {quote_ident(out_col.name)} FROM calc "
        f"WHERE {keep} ORDER BY {order_text}"
    )
    return StepPlan(
        mode="window",
        sql=sql,
        columns=key_cols + [out_col],
        order_keys=[col.name for col in key_cols],
        group_by=[col.name for col in key_cols],
        prov_sql=_prov_sql(key_exprs, common["from_sql"], common["where_text"]),
        key_exprs=key_exprs,
        **common,
    )


def _outlier_predicate(spec: dict, metric_sql: str) -> str:
    avg = quote_ident("__avg")
    guards = f"{metric_sql} IS NOT NULL AND {avg} IS NOT NULL"
    if spec["kind"] == "variance":
        sq = quote_ident("__avg_sq")
        sigma = float(spec.get("sigma", 3.0))
        return (
            f"{guards} AND ({metric_sql} - {avg}) * ({metric_sql} - {avg}) "
            f"> {sigma * sigma} * ({sq} - {avg} * {avg})"
        )
    if "tolerance" in spec:
        tolerance = float(spec["tolerance"])
        return f"{guards} AND ABS({metric_sql} - {avg}) > ABS({avg}) * {tolerance}"
    factor = float(spec.get("factor", 1.5))
    operator = "<" if spec["compare"] == "below" else ">"
    return f"{guards} AND {metric_sql} {operator} {avg} * {factor}"


def _outlier_plan(
    *, shape, base, key_cols, key_exprs, metric_objs, metric_cols, table_obj, **common
) -> StepPlan:
    """异常：按「组内均值」打标（`AVG() OVER`），窗口判据不能进 WHERE，所以套一层 `stat`。"""
    spec = shape.outlier
    metric, metric_col = metric_objs[0], metric_cols[0]
    metric_sql = quote_ident(metric_col.name)

    if spec.get("row_level"):
        # 「单笔订单金额」= 按 order_id 分组的窗口求和：不穿 GROUP BY 也能拿到每笔的值
        if "order_id" not in [col.name for col in key_cols]:
            key_cols = list(key_cols) + [_dim_column(table_obj, "order_id")]
            key_exprs = list(key_exprs) + [quote_ident("order_id")]
        base = (
            f"SELECT {''.join(f'{quote_ident(col.name)}, ' for col in key_cols)}"
            f"{metric.definition} OVER (PARTITION BY {quote_ident('order_id')}) AS {metric_sql} "
            f"FROM {common['from_sql']}"
            + (f" WHERE {common['where_text']}" if common["where_text"] else "")
        )

    keys_sel = "".join(f"{quote_ident(col.name)}, " for col in key_cols)
    partition = ""
    if spec["partition"] == "dim" and common["dimensions"]:
        partition = "PARTITION BY " + ", ".join(quote_ident(d) for d in common["dimensions"])
    elif spec["partition"] == "month" and key_cols:
        partition = f"PARTITION BY substr({quote_ident(key_cols[0].name)}, 1, 7)"
    over = f"OVER ({partition})"

    stat_select = f"{keys_sel}{metric_sql} AS {metric_sql}, AVG({metric_sql}) {over} AS {quote_ident('__avg')}"
    if spec["kind"] == "variance":
        stat_select += f", AVG({metric_sql} * {metric_sql}) {over} AS {quote_ident('__avg_sq')}"
    predicate = _outlier_predicate(spec, metric_sql)
    if spec.get("month_filter") and key_cols:
        predicate += f" AND {quote_ident(key_cols[0].name)} = {literal(spec['month_filter'])}"

    grouping = ""
    if spec.get("row_level") and common["dimensions"]:
        # 同一笔订单会命中多行源数据，按「键 + 值」去重（问题要的是「哪些客户」，不是行数）
        grouping = " GROUP BY " + ", ".join(
            [quote_ident(col.name) for col in key_cols] + [metric_sql]
        )
    order_text = ", ".join([f"{quote_ident(col.name)} ASC" for col in key_cols] + [f"{metric_sql} ASC"])
    sql = (
        f"WITH base AS ({base}), stat AS (SELECT {stat_select} FROM base) "
        f"SELECT {keys_sel}{metric_sql} FROM stat WHERE {predicate}{grouping} ORDER BY {order_text}"
    )
    return StepPlan(
        mode="outlier",
        sql=sql,
        columns=key_cols + [metric_col],
        order_keys=[col.name for col in key_cols] + [metric_col.name],
        group_by=[col.name for col in key_cols],
        prov_sql=_prov_sql(key_exprs, common["from_sql"], common["where_text"]),
        key_exprs=key_exprs,
        **common,
    )


def _compare_plan(*, shape, metric_objs, metric_cols, table_obj, d1, **common) -> StepPlan:
    """两期对比：两个 CTE 各算一次，再相减（绝对增长）或相除（变化率）。"""
    spec = shape.compare
    metric, metric_col = metric_objs[0], metric_cols[0]
    table_name = common["table"]
    time_col = _bucket_time_column(d1, metric_objs, table_obj)
    extra = _filters_text(d1, table_obj)

    def bucket_where(start: str, end: str) -> str:
        parts = [f"{quote_ident(time_col)} >= {literal(start)}", f"{quote_ident(time_col)} <= {literal(end)}"]
        if extra:
            parts.append(extra)
        return " AND ".join(parts)

    cur_alias, prev_alias = quote_ident("__cur"), quote_ident("__prev")
    cur_sel = f"SELECT {metric.definition} AS {cur_alias} FROM {common['from_sql']} WHERE {bucket_where(spec['cur_start'], spec['cur_end'])}"
    prev_sel = f"SELECT {metric.definition} AS {prev_alias} FROM {common['from_sql']} WHERE {bucket_where(spec['prev_start'], spec['prev_end'])}"
    cur_value = f"(SELECT {cur_alias} FROM cur)"
    prev_value = f"(SELECT {prev_alias} FROM prev)"
    mode = spec.get("mode") or ("rate" if spec.get("rate") else "diff")
    if mode == "rate":
        out_col = _derived_column(metric, unit="%")
        expr = (
            f"CASE WHEN {prev_value} IS NULL OR {prev_value} = 0 THEN NULL "
            f"ELSE ROUND(({cur_value} - {prev_value}) * 100.0 / {prev_value}, 2) END"
        )
    elif mode == "pp":
        # 指标本身就是率（%）：两个率的差就是「百分点」，再除以基期会算成「率的增长率」
        out_col = _derived_column(metric, unit="%")
        expr = f"ROUND({cur_value} - {prev_value}, 2)"
    else:
        out_col = metric_col
        expr = f"ROUND({cur_value} - {prev_value}, 2)"
    sql = (
        f"WITH cur AS ({cur_sel}), prev AS ({prev_sel}) "
        f"SELECT {expr} AS {quote_ident(out_col.name)} ORDER BY {quote_ident(out_col.name)} ASC"
    )
    prov = (
        f"SELECT {quote_ident(SRC_ROWID)} FROM {common['from_sql']} WHERE ("
        f"({quote_ident(time_col)} >= {literal(spec['cur_start'])} AND {quote_ident(time_col)} <= {literal(spec['cur_end'])}) OR "
        f"({quote_ident(time_col)} >= {literal(spec['prev_start'])} AND {quote_ident(time_col)} <= {literal(spec['prev_end'])}))"
        + (f" AND ({extra})" if extra else "")
    )
    return StepPlan(
        mode="compare",
        sql=sql,
        columns=[out_col],
        order_keys=[out_col.name],
        group_by=[],
        prov_sql=prov,
        key_exprs=[],
        **common,
    )


def _period_compare_plan(*, d1, metric_objs, metric_cols, table_obj, **common) -> StepPlan:
    """两期对比（§5.1 D1 v1.1）：`comparison.left` 是**基期**，`right` 是对比期。

    两期必须在**同一条 SQL** 里算完，且这条 SQL 必须含 `LAG(` / `OVER(` / `WITH ` 之一（§5.1 硬规则）。
    这里用 `WITH` 起三个 CTE：两期各聚一次（`__left` / `__right`），再用 `__keys` 把两期的键
    `UNION` 成**并集**，最后 `LEFT JOIN` 回两侧，输出三列：
    `<指标>` = 基期值、`<指标>__delta` = 对比期 − 基期、`<指标>__pct` = 差值 ÷ 基期 × 100。

    §5.1 v1.1 硬规则：**合并后的键集必须是两期键集的并集**。只在其中一期出现的键也要出结果，
    缺失那一侧的值写 `null`（**不许写 0**——0 表示「有行、合计为 0」），该行 `__delta` / `__pct`
    同样写 `null`；这两件事由 `ask.query` 分别记 `COMPARISON_KEY_MISSING` 与 `PCT_BASE_ZERO`。

    **契约没写清、本实现自己定的一条（报告里要报）**
    哪一侧是「基期」：§5.1 只写「`left` 为 0 时该格写 null 并记 `PCT_BASE_ZERO`」，本实现据此
    认定 `left` 是除数（基期）、`right` 是被比的那一期（`time_range` 也要求等于 `left`）；
    输出的 `<指标>` 列取基期值，`__delta` / `__pct` 都是「对比期相对基期」的变化。
    """
    spec = d1["comparison"]
    table_name = common["table"]
    time_col = _bucket_time_column(d1, metric_objs, table_obj)
    extra = _filters_text(d1, table_obj)

    key_cols = [_dim_column(table_obj, dim) for dim in common["dimensions"]]
    key_exprs = [quote_ident(dim) for dim in common["dimensions"]]

    def period_cte(side: dict, alias: str) -> str:
        parts = [
            f"{quote_ident(time_col)} >= {literal(side['start'])}",
            f"{quote_ident(time_col)} <= {literal(side['end'])}",
        ]
        if extra:
            parts.append(extra)
        select = [f"{expr} AS {quote_ident(col.name)}" for expr, col in zip(key_exprs, key_cols)]
        select += [
            f"{metric.definition} AS {quote_ident(col.name)}" for metric, col in zip(metric_objs, metric_cols)
        ]
        body = f"SELECT {', '.join(select)} FROM {common['from_sql']} WHERE {' AND '.join(parts)}"
        if key_exprs:
            body += " GROUP BY " + ", ".join(quote_ident(col.name) for col in key_cols)
        return f"{quote_ident(alias)} AS ({body})"

    left_tbl, right_tbl = quote_ident("__left"), quote_ident("__right")
    keys_tbl = quote_ident("__keys")
    # §5.1 v1.1：键集 = 两期键集的**并集**，所以维度值从 `__keys` 取（它是两侧的并集）
    select = [f"{keys_tbl}.{quote_ident(col.name)} AS {quote_ident(col.name)}" for col in key_cols]
    columns: list = list(key_cols)
    for metric, metric_col in zip(metric_objs, metric_cols):
        base = f"{left_tbl}.{quote_ident(metric_col.name)}"
        cur = f"{right_tbl}.{quote_ident(metric_col.name)}"
        # §2.4 v1.1：差值列 type/digits/unit 与指标一致；变化率列固定 decimal/2/%
        delta_col = OutColumn(
            name=f"{metric_col.name}{DELTA_SUFFIX}",
            type=metric_col.type,
            unit=metric_col.unit,
            digits=metric_col.digits,
            source_metric_id=metric.id,
            source_column=None,
            metric=metric,
            src_columns=tuple(metric_columns(metric)),
            derived=True,
        )
        pct_col = OutColumn(
            name=f"{metric_col.name}{PCT_SUFFIX}",
            type="decimal",
            unit="%",
            digits=2,
            source_metric_id=metric.id,
            source_column=None,
            metric=metric,
            src_columns=tuple(metric_columns(metric)),
            derived=True,
        )
        select.append(f"{base} AS {quote_ident(metric_col.name)}")
        select.append(f"ROUND({cur} - {base}, 2) AS {quote_ident(delta_col.name)}")
        select.append(
            f"CASE WHEN {base} IS NULL OR {base} = 0 THEN NULL "
            f"ELSE ROUND(({cur} - {base}) * 100.0 / {base}, 2) END AS {quote_ident(pct_col.name)}"
        )
        columns += [metric_col, delta_col, pct_col]

    if key_exprs:
        # 键并集：`UNION` 会去重，所以同一维度组合在结果里只出现一行
        keys_body = " UNION ".join(
            f"SELECT {', '.join(quote_ident(col.name) for col in key_cols)} FROM {tbl}"
            for tbl in (left_tbl, right_tbl)
        )
        keys_cte = f"{keys_tbl} AS ({keys_body})"
        on_left = " AND ".join(
            f"{keys_tbl}.{quote_ident(col.name)} = {left_tbl}.{quote_ident(col.name)}" for col in key_cols
        )
        on_right = " AND ".join(
            f"{keys_tbl}.{quote_ident(col.name)} = {right_tbl}.{quote_ident(col.name)}" for col in key_cols
        )
        source = f"{keys_tbl} LEFT JOIN {left_tbl} ON {on_left} LEFT JOIN {right_tbl} ON {on_right}"
    else:
        # 没有分组维度：两期各自最多一行，用常数键配成一行（两期都空时结果仍为 0 行）
        keys_cte = (
            f'{keys_tbl} AS (SELECT 1 AS "__one" FROM {left_tbl} '
            f'UNION SELECT 1 FROM {right_tbl})'
        )
        source = f"{keys_tbl} LEFT JOIN {left_tbl} ON 1 = 1 LEFT JOIN {right_tbl} ON 1 = 1"

    order_keys = [col.name for col in key_cols] + [col.name for col in columns[len(key_cols) :]]
    sql = (
        f"WITH {period_cte(spec['left'], '__left')}, {period_cte(spec['right'], '__right')}, {keys_cte} "
        f"SELECT {', '.join(select)} FROM {source} "
        f"ORDER BY {', '.join(f'{quote_ident(name)} ASC' for name in order_keys)}"
    )

    # 追溯：两期的源行都要算进来（同一行的三格数字分别来自两期，`row_ids` 取并集）
    ranges = " OR ".join(
        f"({quote_ident(time_col)} >= {literal(spec[side]['start'])} "
        f"AND {quote_ident(time_col)} <= {literal(spec[side]['end'])})"
        for side in ("left", "right")
    )
    prov_where = f"({ranges})" + (f" AND ({extra})" if extra else "")
    return StepPlan(
        mode="compare",
        sql=sql,
        columns=columns,
        order_keys=order_keys,
        group_by=[col.name for col in key_cols],
        prov_sql=_prov_sql(key_exprs, common["from_sql"], prov_where),
        key_exprs=key_exprs,
        **common,
    )


def _detail_plan(
    *,
    step,
    d1,
    d3,
    table_name,
    table,
    where_text,
    where_items,
    sql_index,
    from_sql,
    source,
    join_subquery,
) -> StepPlan:
    # --- 明细：直接映射原始列 ---
    chosen = [c["column"] for c in d3.get("columns") or [] if c.get("table") == table_name]
    picked = [c for c in table.columns if c.name in set(chosen)] or list(table.columns)
    out_cols = [_dim_column(table, c.name) for c in picked]
    if not out_cols:
        raise DecisionError(
            "COLUMN_NOT_FOUND",
            f"D3 没给表 {table_name} 选出任何列",
            stage="D3",
            readable_hint="这一步没有可输出的列",
        )
    select_parts = [f"{quote_ident(c.name)} AS {quote_ident(c.name)}" for c in out_cols]
    sql = f"SELECT {', '.join(select_parts)} FROM {from_sql}"
    if where_text:
        sql += f" WHERE {where_text}"
    # §6.3.1：ORDER BY 必须有，且 §6.3.2 要求全序 → 末尾补稳定行号（明细行可能整行重复）
    order = [quote_ident(c.name) for c in out_cols] + [quote_ident(SRC_ROWID)]
    sql += " ORDER BY " + ", ".join(f"{o} ASC" for o in order)
    # JOIN 时主语句里有嵌套子查询，执行器不能靠「第一处 FROM」反推追溯语句，这里显式给出来
    prov_sql = ""
    if source.joined:
        prov_select = select_parts + [quote_ident(SRC_ROWID)]
        prov_sql = f"SELECT {', '.join(prov_select)} FROM {from_sql}"
        if where_text:
            prov_sql += f" WHERE {where_text}"
        prov_sql += " ORDER BY " + ", ".join(f"{o} ASC" for o in order)
    return StepPlan(
        step=step,
        sql_index=sql_index,
        sql=sql,
        table=table_name,
        mode="detail",
        columns=out_cols,
        where_text=where_text,
        where_items=where_items,
        dimensions=[],
        metric_ids=[],
        order_keys=[c.name for c in out_cols] + [SRC_ROWID],
        group_by=[],
        from_sql=from_sql,
        joins=source.as_joins(),
        join_subquery=join_subquery,
        join_filter=f"{quote_ident(ANTI_ALIAS)} IS NULL" if source.anti else "",
        join_tables=list(source.tables),
        rid_text=source.rid_text,
        prov_sql=prov_sql,
    )
