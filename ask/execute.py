"""ask.execute · 只读执行 + 追溯链（契约 §2.5 / §2.6 / §4 / §6.3 / §8.2）

契约出处（`contract/answer.md`）：
- §8.2 只读连接、`PRAGMA query_only=1`、`trusted_schema=OFF`、单语句超时、`row_cap`
- §6.2 一律 `Decimal`、`ROUND_HALF_EVEN`、禁科学计数法、`NaN/Inf` → `RESULT_NONFINITE`
- §6.3 行序：全序检查（`ORDER_NOT_TOTAL`）、`rows` 末尾按 `__rid` tiebreak
- §2.5 `__rid` 规则（明细 / 聚合）、§2.6 + §4 `trace[]` 与 T1–T8 判据

**契约没写清、本模块自己定的四条（报告里要报）**
1. §8.2 要求 `sqlite3.connect("file:<path>?mode=ro", uri=True)`。本实现把源文件**导入内存库**再查询
   （源文件是 xlsx，本来就不在 SQLite 里，没有可只读挂载的库文件），因此 `mode=ro` 无处可加；
   等价的两道闸是：导入完成后立刻 `PRAGMA query_only = 1`（此后任何写都会报错）+ `trusted_schema=OFF`
   + 静态检查 §8.1 已跑过。内存库不存在「改到源文件」的通路，这是这条替换成立的唯一理由。
2. §4.1 说 `N` = 所有 `int/decimal` 单元格个数，`null` 单元格也算「一个数字格」但**没有数字**可追溯。
   本实现 `N` 只数**非 null** 的数值单元格（`|trace| == N` 才能成立）。对 smoke 数据两者无差别。
3. §6.3.2「排序必须全序」的机械判据：明细语句的 `ORDER BY` 末尾必须有导入时写入的 `__src_rowid`
   （它按数据行号生成，天然唯一）；聚合语句的 `ORDER BY` 必须覆盖 `GROUP BY` 的全部维度
   （维度全等 ⟹ 组唯一）。两条都不成立时，退化为逐行比对排序键，出现并列即 `ORDER_NOT_TOTAL`。
4. **JOIN 之后的行标识只取主表**：§2.5 说 JOIN 行用 `+` 连接各源 id，但 §4.3 T3 的反解规则
   只定义了单个「文件#sheet!行号」。为了让追溯链仍然可机械核对，`trace.row_ids` 记**主表**
   （事实表/维表基表）命中的源行；JOIN 子查询里的 `__src_rowid` 也就是主表的行号
   （`rid_text` 只表示它经过一次 `CAST(... AS TEXT)`）。契约缺口，见 `ask/_selfcheck/notes.md` 第 22 条。
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path

from .decide import DecisionError, NULL_OPS, sha256_text
from .schema import Table, format_decimal_plain
from .sqlgen import SRC_ROWID, OutColumn, StepPlan
from .static_check import StaticReject, check_identifiers, identifiers, literal, quote_ident, tokenize

STATEMENT_TIMEOUT_S = 30.0  # §8.2 单条语句超时
PROGRESS_STEPS = 10_000  # 每 1 万条 VM 指令看一眼表

NUMERIC_TYPES = ("int", "decimal")
AGG_OPS = ("sum", "avg", "count", "count_distinct", "min", "max", "identity")

# 列类型 → SQLite 声明类型（决定 affinity；decimal 走 REAL，读回时按 str 还原成 Decimal）
_AFFINITY = {
    "int": "INTEGER",
    "decimal": "REAL",
    "text": "TEXT",
    "date": "TEXT",
    "bool": "INTEGER",
    "null": "TEXT",
}


# --------------------------------------------------------------------------- #
# 导入 + 只读连接
# --------------------------------------------------------------------------- #


def _stored(value, col_type: str):
    """Table 里已归一化的值 → 便于 SQLite 存储的形态（Decimal 存定点文本，靠 REAL affinity 转数）。"""
    if value is None:
        return None
    if col_type == "decimal":
        return format_decimal_plain(value) if isinstance(value, Decimal) else str(value)
    if col_type == "int":
        return int(value)
    if col_type == "bool":
        return 1 if value else 0
    return str(value)


def build_connection(tables_by_name: dict) -> sqlite3.Connection:
    """把各表导入内存 SQLite（表名 = sheet 名 / CSV 文件主名），导入完立刻上只读锁（§8.2）。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA trusted_schema = OFF")
        for name, table in tables_by_name.items():
            cols = ", ".join(
                [f"{quote_ident(SRC_ROWID)} INTEGER"]
                + [f"{quote_ident(c.name)} {_AFFINITY.get(c.type, 'TEXT')}" for c in table.columns]
            )
            conn.execute(f"CREATE TABLE {quote_ident(name)} ({cols})")
            names = [c.name for c in table.columns]
            placeholders = ", ".join("?" * (len(names) + 1))
            payload = []
            for row in table.rows:
                payload.append(
                    [int(row["__row_no"])] + [_stored(row.get(n), table.column(n).type) for n in names]
                )
            if payload:
                conn.executemany(f"INSERT INTO {quote_ident(name)} VALUES ({placeholders})", payload)
        conn.commit()
        # §8.2 的第二道闸：从这里开始，任何写操作都会报错
        conn.execute("PRAGMA query_only = 1")
    except sqlite3.Error as exc:  # pragma: no cover - 导入失败属于实现 bug
        conn.close()
        raise DecisionError(
            "INTERNAL",
            f"把数据导入内存 SQLite 失败: {exc}",
            stage="execute",
            readable_hint="这是执行器自身的问题，不是数据的问题",
        )
    return conn


def _fetch(conn: sqlite3.Connection, sql: str, *, row_cap: int | None = None) -> list:
    """执行一条**已经过静态检查**的语句并取行；超时/出错 → `SQL_EXEC_ERROR`（§8.2）。"""
    deadline = time.monotonic() + STATEMENT_TIMEOUT_S
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_STEPS)
    try:
        cursor = conn.execute(sql)
        if row_cap is None:
            return cursor.fetchall()
        return cursor.fetchmany(int(row_cap))
    except sqlite3.Error as exc:
        message = str(exc)
        hint = "数据或列名对不上，SQL 没跑通"
        if "interrupted" in message.lower():
            message = f"语句超过 {STATEMENT_TIMEOUT_S:.0f}s 被中断"
            hint = "这条查询太慢了；先缩小时间范围或加过滤条件"
        raise DecisionError(
            "SQL_EXEC_ERROR",
            f"{message}（语句：{sql}）",
            stage="execute",
            readable_hint=hint,
        )
    finally:
        conn.set_progress_handler(None, 0)


# --------------------------------------------------------------------------- #
# 单元格取值（§6.2）
# --------------------------------------------------------------------------- #


def _nonfinite(value, column: str) -> DecisionError:
    return DecisionError(
        "RESULT_NONFINITE",
        f"列 {column} 算出了 {value}（NaN / Infinity 不许当数字输出，§6.2）",
        stage="execute",
        readable_hint="这个数算出来了但不是有限的数（多半是除零或空集合求平均）；不四舍五入，也不拿 0 顶替",
    )


def convert(value, col: OutColumn):
    """SQLite 取回的原始值 → 契约 §2.4 声明类型的值（`Decimal` 一律走 str 还原，避开二进制浮点）。"""
    if value is None:
        return None
    if col.type == "int":
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise _nonfinite(value, col.name)
        if not number.is_finite():
            raise _nonfinite(value, col.name)
        return int(number.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    if col.type == "decimal":
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise _nonfinite(value, col.name)
        if not number.is_finite():
            raise _nonfinite(value, col.name)
        exponent = Decimal(1).scaleb(-max(int(col.digits), 0))
        quantized = number.quantize(exponent, rounding=ROUND_HALF_EVEN)
        if quantized == 0:
            quantized = abs(quantized)  # §6.2：不许出现 -0.00
        return quantized
    if col.type == "bool":
        return bool(int(value))
    if col.type == "null":
        return None
    return str(value)


# --------------------------------------------------------------------------- #
# 执行一步
# --------------------------------------------------------------------------- #


@dataclass
class StepResult:
    """一步的执行结果：值 + 每行对应的源行 id（供 §2.6 追溯）。"""

    plan: StepPlan
    columns: list
    rows: list = field(default_factory=list)  # 不含 __rid
    rids: list = field(default_factory=list)  # 与 rows 对齐，每行一个 rid 列表
    truncated_by_row_cap: bool = False
    row_ids_truncated: bool = False
    join_multiplied: bool = False  # JOIN 后行数比任一源表大（§2.9 JOIN_ROW_MULTIPLIED）
    sql: str = ""  # **实际执行的那一条**（`finalize_sql` 之后），原样进 `sql[]`

    @property
    def numeric_columns(self) -> list:
        return [c for c in self.columns if c.type in NUMERIC_TYPES]


def _agg_rid(table: Table, dimensions, row) -> str:
    """§2.5 聚合行 `__rid`：`agg(<文件basename>#<sheet>:<列名=值;…>)`。"""
    basename = table.file.rsplit("/", 1)[-1]
    tag = table.sheet if table.sheet else Path(table.file).stem
    key = ";".join(f"{name}={row.get(name)}" for name in dimensions)
    return f"agg({basename}#{tag}:{key})"


_ROWID_TIEBREAK = f"{quote_ident(SRC_ROWID)} ASC"


def finalize_sql(plan: StepPlan) -> str:
    """§6.3.1/§6.3.2：执行器负责让语句带上**全序** `ORDER BY`。

    生成器已经写好了，这里只兜底：没 `ORDER BY` 就追加，明细语句末尾缺 `__src_rowid` 就补上
    （数据行号唯一，补上之后一定是全序）。返回的文本就是**实际执行的那一条**，要原样进 `sql[]`。
    """
    sql = plan.sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    if " ORDER BY " not in sql:
        first = plan.columns[0].name if plan.columns else SRC_ROWID
        return f"{sql} ORDER BY {quote_ident(first)} ASC, {_ROWID_TIEBREAK}"
    if plan.mode == "detail" and not sql.endswith(_ROWID_TIEBREAK):
        return f"{sql}, {_ROWID_TIEBREAK}"
    return sql


def _split_select(sql: str) -> tuple:
    """`SELECT <列表> FROM <其余>` → `(SELECT 列表, 其余)`。生成的语句里第一处 ` FROM ` 就是表位置。"""
    head, sep, rest = sql.partition(" FROM ")
    if not sep:
        raise DecisionError(
            "INTERNAL",
            f"执行器认不出一条没有 FROM 的查询：{sql}",
            stage="execute",
            readable_hint="这是执行器自身的问题",
        )
    return head, rest


def _detail_provenance_sql(sql: str) -> str:
    """明细：同一条语句再取一遍，只在 SELECT 列表末尾多带 `__src_rowid`（行序逐行对应）。"""
    head, rest = _split_select(sql)
    return f"{head}, {quote_ident(SRC_ROWID)} FROM {rest}"


def _aggregate_provenance_sql(sql: str, dimensions) -> str:
    """聚合：去掉 `GROUP BY` / `ORDER BY`，改取「维度 + 源行号」，用于反查每组命中的源行。"""
    _head, rest = _split_select(sql)
    tail = rest
    for keyword in (" GROUP BY ", " ORDER BY "):
        index = tail.find(keyword)
        if index != -1:
            tail = tail[:index]
    select = [quote_ident(d) for d in dimensions] + [quote_ident(SRC_ROWID)]
    order = [f"{quote_ident(d)} ASC" for d in dimensions] + [_ROWID_TIEBREAK]
    return f"SELECT {', '.join(select)} FROM {tail} ORDER BY {', '.join(order)}"


def _detail_provenance(conn: sqlite3.Connection, plan: StepPlan, sql: str, row_cap: int) -> list:
    """明细：同一条语句再取一遍，只在 SELECT 列表末尾多带 `__src_rowid`（行序逐行对应）。

    JOIN 之后主语句里已经有嵌套子查询，不能再靠「第一处 ` FROM `」反推，
    这时用 `plan.prov_sql`（生成器已经写好，取数源与主语句同一个子查询）。
    """
    statement = plan.prov_sql or _detail_provenance_sql(sql)
    return [record[-1] for record in _fetch(conn, statement, row_cap=row_cap)]


def _aggregate_provenance(conn: sqlite3.Connection, plan: StepPlan, sql: str) -> dict:
    """聚合：按分组键归并，记下每组命中的源行号（再换成 `__rid`）。

    窗口/异常/两期对比这几类语句的分组发生在模板内部，所以追溯改走 `plan.prov_sql`
    （同一套键表达式，取「键 + 源行号」），而不是从正式 SQL 文本里剥 `GROUP BY`。
    JOIN 之后 `plan.prov_sql` 里的 `__src_rowid` 已经是文本（复合行 id），由调用方按 `plan.rid_text` 处理。
    """
    keys = list(plan.group_by or plan.dimensions)
    prov_sql = plan.prov_sql or _aggregate_provenance_sql(sql, keys)
    rows = _fetch(conn, prov_sql)
    if not keys:
        return {(): [record[-1] for record in rows]}
    key_cols = [next(c for c in plan.columns if c.name == key) for key in keys]
    groups: dict = {}
    for record in rows:
        key = tuple(convert(record[i], col) for i, col in enumerate(key_cols))
        groups.setdefault(key, []).append(record[-1])
    return groups


def _check_total_order(plan: StepPlan, sql: str, rows: list) -> None:
    """§6.3.2 全序检查（判据见模块 docstring 第 3 条）。"""
    if plan.mode == "detail":
        if sql.rstrip().endswith(_ROWID_TIEBREAK):
            return  # 数据行号唯一 ⟹ 全序
    else:
        if plan.group_by and set(plan.group_by) <= set(plan.order_keys):
            return  # GROUP BY 覆盖全部排序键 ⟹ 组唯一
    keys = [k for k in plan.order_keys if k != SRC_ROWID]
    for prev, cur in zip(rows, rows[1:]):
        same = all(
            (type(prev.get(k)).__name__, prev.get(k)) == (type(cur.get(k)).__name__, cur.get(k))
            for k in keys
        )
        if same:
            raise DecisionError(
                "ORDER_NOT_TOTAL",
                f"排序键 {keys} 在结果里有并列，且末尾没有唯一的 tiebreak（§6.3.2）",
                stage="execute",
                readable_hint="排序不唯一会让同一份数据今天一个顺序、明天另一个顺序；需要再加一个唯一列做次级排序",
            )


def run_step(conn: sqlite3.Connection, plan: StepPlan, *, table: Table, options: dict, tables=None) -> StepResult:
    """执行一步 SQL → 值 + 每行的源行 id。`row_cap` 超限只截断、不报错（§8.2）。

    `tables`：本次导入的全部表（含桥接表），只用于 JOIN 后的「行放大」检查（§2.9）。
    """
    row_cap = int(options["row_cap"])
    sql = finalize_sql(plan)
    raw = _fetch(conn, sql, row_cap=row_cap + 1)
    truncated = len(raw) > row_cap
    if truncated:
        raw = raw[:row_cap]

    rows = []
    for record in raw:
        rows.append({col.name: convert(record[i], col) for i, col in enumerate(plan.columns)})
    _check_total_order(plan, sql, rows)

    rids_by_row_no = {int(row["__row_no"]): row["__rid"] for row in table.rows}
    if plan.mode == "detail":
        row_nos = _detail_provenance(conn, plan, sql, row_cap)
        if len(row_nos) != len(rows):
            raise DecisionError(
                "INTERNAL",
                f"追溯查询与正式查询行数不一致（{len(row_nos)} vs {len(rows)}）",
                stage="execute",
                readable_hint="这是执行器自身的问题：两次查询的排序没对齐",
            )
        if plan.rid_text:
            # JOIN 子查询里的 `__src_rowid` 是 CAST(…) 出来的文本，值仍是主表的数据行号
            rids = [[rids_by_row_no.get(int(str(n)), f"?{n}")] for n in row_nos]
        else:
            rids = [[rids_by_row_no.get(int(n), f"?{n}")] for n in row_nos]
    else:
        groups = _aggregate_provenance(conn, plan, sql)
        key_names = list(plan.group_by or plan.dimensions)
        rids = []
        for row in rows:
            key = tuple(row.get(name) for name in key_names)
            hits = groups.get(key)
            if hits is None:
                raise DecisionError(
                    "INTERNAL",
                    f"聚合行 {key} 在追溯查询里找不到对应的源行",
                    stage="execute",
                    readable_hint="这是执行器自身的问题：聚合键没对上",
                )
            rids.append([rids_by_row_no.get(int(str(n)), f"?{n}") for n in hits])
        for row in rows:
            row["__rid"] = _agg_rid(table, key_names, row)
    result = StepResult(plan=plan, columns=list(plan.columns), rows=rows, rids=rids, truncated_by_row_cap=truncated)
    if plan.join_subquery and tables:
        count_sql = f"SELECT COUNT(*) FROM {plan.join_subquery}"
        if plan.join_filter:
            count_sql += f" WHERE {plan.join_filter}"
        joined = int(_fetch(conn, count_sql)[0][0])
        biggest = max((len(t.rows) for name, t in tables.items() if name in set(plan.join_tables)), default=0)
        result.join_multiplied = joined > biggest
    result.sql = sql
    return result


# --------------------------------------------------------------------------- #
# 追溯链（§2.6 / §4）
# --------------------------------------------------------------------------- #


def _filter_for(plan: StepPlan, row: dict) -> dict:
    """§4.3 T4：要么 `{"none": true}`，要么 `column/op/value_literal` 三件套。"""
    if plan.mode != "detail" and (plan.group_by or plan.dimensions):
        key = (plan.group_by or plan.dimensions)[0]
        return {"column": key, "op": "=", "value_literal": literal(row.get(key))}
    if plan.where_items:
        column, op, value_sql = plan.where_items[0]
        return {"column": column, "op": op, "value_literal": value_sql}
    return {"none": True}


def _source_columns(plan: StepPlan, col: OutColumn, table: Table | None = None) -> list:
    """这格数字由哪些源列算出来（§4.3 T2 要求名字都在源表头里）。

    `COUNT(*)` 这类指标不引用任何列：这时「整行都参与计数」，所以退回**表的全部列**，
    而不是留空（留空 T2 直接判不合契约——q009/q058 就是这么报 INTERNAL 的）。
    """
    used = list(plan.dimensions) if plan.mode != "detail" else []
    for name in col.src_columns:
        if name not in used:
            used.append(name)
    if not used and table is not None:
        used = [c.name for c in table.columns]
    return used


def _aggregation_for(col: OutColumn) -> list:
    """§2.6：明细必须显式 `[{"op":"identity"}]`；聚合要写清 op / over / group_by。"""
    if col.metric is None:
        return [{"op": "identity"}]
    op = col.metric.default_aggregation
    over = col.src_columns[0] if col.src_columns else None
    entry = {"op": op}
    if over is not None:
        entry["over"] = over
    return [entry]


def build_traces(result: StepResult, *, table: Table, options: dict) -> list:
    """一个输出数字一条 trace（`|trace| == N`），按 `value_path` 字节序编号（§6.3.4）。

    `N` 的定义是「`rows` 里 int/decimal 的**非空单元格**数」：空值不发 trace。
    理由有二——① 空值没有「值」可追，`row_ids` 反而会暗示一个并不存在的数；
    ② 契约 §2.6 要求 `value` 与单元格严格相等，`null` 谈不上相等。
    **代价**：输出里若出现空值数字格，`|trace|` 会比单元格数少 1，所以生成的 SQL
    一律把算不出来的格子挡在结果外（窗口题 `上一期 IS NOT NULL AND 上一期 <> 0`），
    详见 `ask/_selfcheck/notes.md` 第 4 条。
    """
    cap = int(options["trace_row_id_cap"])
    items = []
    for index, row in enumerate(result.rows):
        for col in result.columns:
            if col.type not in NUMERIC_TYPES:
                continue
            value = row.get(col.name)
            if value is None:
                continue  # 空值不发 trace（见模块 docstring 第 2 条）
            row_ids = sorted({str(r) for r in result.rids[index]}, key=lambda s: s.encode("utf-8"))
            total = len(row_ids)
            truncated = total > cap
            if truncated:
                result.row_ids_truncated = True
                row_ids = row_ids[:cap]
            expression = col.metric.definition if col.metric is not None else quote_ident(col.name)
            aggregation = _aggregation_for(col)
            if col.metric is not None:
                aggregation[0]["group_by"] = list(result.plan.group_by or result.plan.dimensions)
            # §4.3 T2：source.columns 里的列必须都出现在 source.file 的表头里。
            # JOIN 之后维度列可能来自另一张表，而 trace.source 只有**一个** file/sheet
            # （契约缺口，见 ask/_selfcheck/notes.md）——这里只保留主表里真实存在的列，
            # 但表达式引用到的列一个都不能丢（T5 优先）。
            source_columns = _source_columns(result.plan, col, table)
            known = {c.name for c in table.columns}
            expr_ids = set(identifiers(expression))
            if any(name not in known for name in source_columns):
                source_columns = [name for name in source_columns if name in known or name in expr_ids]
            items.append(
                {
                    "value_path": f"rows[{index}].{col.name}",
                    "value": value,
                    "metric_id": col.source_metric_id,
                    "source": {
                        "file": table.file,
                        "file_sha256": table.file_sha256,
                        "sheet": table.sheet,
                        "header_row": table.header_row,
                        "columns": source_columns,
                    },
                    "row_ids": row_ids,
                    "row_ids_total": total,
                    "row_ids_truncated": truncated,
                    "filter": _filter_for(result.plan, row),
                    "expression": expression,
                    "aggregation": aggregation,
                    "sql_index": result.plan.sql_index,
                }
            )
    items.sort(key=lambda item: item["value_path"].encode("utf-8"))
    traces = []
    for seq, item in enumerate(items, start=1):
        trace = {"trace_id": f"t-{seq:04d}"}
        trace.update(item)
        traces.append(trace)
    return traces


# --------------------------------------------------------------------------- #
# T1–T8 校验（§4.3）——自己产的 trace 也要过一遍，别拿「我写的」当「对的」
# --------------------------------------------------------------------------- #

_RID_RE = re.compile(r"^(?P<base>[^#]+)#(?P<tag>[^!]*)!(?P<row>\d+)$")
_PATH_RE = re.compile(r"^rows\[(\d+)\]\.([^.]+)$")


def _fail(rule: str, message: str) -> DecisionError:
    return DecisionError(
        "INTERNAL",
        f"追溯链不合契约 {rule}：{message}",
        stage="export",
        readable_hint="这是导出前的自检：trace 里有一条经不起机械核对，先别把这答案给人看",
    )


def check_traces(
    traces,
    *,
    sql_statements,
    columns,
    rows,
    files,
    tables_by_name,
    options,
    metrics_used,
) -> None:
    """T1–T8 全过才返回；任何一条不过 → `INTERNAL`（说明 trace 生成有 bug）。"""
    sha_by_path = {str(f["path"]): str(f["sha256"]) for f in files or []}
    by_name = {name: table for name, table in (tables_by_name or {}).items()}
    by_file = {}
    for table in by_name.values():
        by_file[(table.file, table.sheet)] = table
    metric_snapshot = {m["id"]: m["definition_sha256"] for m in metrics_used or []}
    cap = int(options["trace_row_id_cap"])

    for trace in traces:
        source = trace["source"]
        # T1 源文件 + 哈希
        if not source["file"] or source["file_sha256"] not in set(sha_by_path.values()):
            raise _fail("T1", f"{trace['trace_id']} 的 source.file_sha256 不在 meta.files 里")
        table = by_file.get((source["file"], source["sheet"]))
        if table is None:
            raise _fail("T1", f"{trace['trace_id']} 指向的 {source['file']}#{source['sheet']} 不是本次导入的表")
        # T2 列名真实存在
        known = {c.name for c in table.columns}
        if not source["columns"]:
            raise _fail("T2", f"{trace['trace_id']} 的 source.columns 为空")
        ghost = [c for c in source["columns"] if c not in known]
        if ghost:
            raise _fail("T2", f"{trace['trace_id']} 的 source.columns 里有表头中不存在的列 {ghost}")
        # T3 行标识
        row_ids = trace["row_ids"]
        if not row_ids:
            # 契约 §4.3 T3 要求 row_ids 非空。缺口：「计数为 0」的格子**没有**任何命中行
            # （q058「是否存在金额为负的订单」= 0 条），此时 row_ids_total 也只能是 0。
            # 这里只放行 row_ids_total == 0 这一种，并把它记进 ask/_selfcheck/notes.md 等契约补。
            if trace["row_ids_total"] != 0:
                raise _fail("T3", f"{trace['trace_id']} 的 row_ids 为空")
        total_rows = len(table.rows)
        for rid in row_ids:
            found = _RID_RE.match(rid)
            if not found or int(found.group("row")) < 1 or int(found.group("row")) > total_rows:
                raise _fail("T3", f"{trace['trace_id']} 的 row_id {rid!r} 反解不出真实源行")
        if trace["row_ids_truncated"]:
            if not (trace["row_ids_total"] > len(row_ids) and len(row_ids) == cap):
                raise _fail("T3", f"{trace['trace_id']} 标了截断，但 row_ids_total/长度不符合 §4.5")
        elif trace["row_ids_total"] != len(row_ids):
            raise _fail("T3", f"{trace['trace_id']} 未截断但 row_ids_total 与长度不等")
        # T4 过滤条件
        flt = trace["filter"]
        if not isinstance(flt, dict):
            raise _fail("T4", f"{trace['trace_id']} 的 filter 不是对象")
        if flt.get("none") is True:
            pass
        else:
            for key in ("column", "op", "value_literal"):
                if key not in flt:
                    raise _fail("T4", f"{trace['trace_id']} 的 filter 缺 {key}")
            # §4.3 T4（v1.1）：`is_null` / `is_not_null` 是独立的两个 op（白名单在 `decide.NULL_OPS`），
            # 且这两个 op 的 `value_literal` 必须**显式**是 null
            if flt["op"] in NULL_OPS:
                if flt["value_literal"] is not None:
                    raise _fail(
                        "T4",
                        f"{trace['trace_id']} 的 filter.op={flt['op']!r} 要求 value_literal 为 null，"
                        f"实际是 {flt['value_literal']!r}",
                    )
            elif flt["op"] not in ("=", "!=", ">", ">=", "<", "<=", "in", "like"):
                raise _fail("T4", f"{trace['trace_id']} 的 filter.op={flt['op']!r} 不在枚举内")
        # T5 表达式只引用声明过的列
        if not trace["expression"].strip():
            raise _fail("T5", f"{trace['trace_id']} 的 expression 为空")
        try:
            check_identifiers(trace["expression"], source["columns"], where=f"{trace['trace_id']}.expression")
        except StaticReject as exc:
            raise _fail("T5", str(exc))
        # T6 聚合步骤
        aggregation = trace["aggregation"]
        if not isinstance(aggregation, list) or not aggregation:
            raise _fail("T6", f"{trace['trace_id']} 的 aggregation 为空")
        for step in aggregation:
            if not isinstance(step, dict) or step.get("op") not in AGG_OPS:
                raise _fail("T6", f"{trace['trace_id']} 的 aggregation.op 不在枚举内")
        # T7 sql_index 指得动 + 表达式是 SQL 的子串
        index = trace["sql_index"]
        if not isinstance(index, int) or not (0 <= index < len(sql_statements)):
            raise _fail("T7", f"{trace['trace_id']} 的 sql_index={index} 越界")
        if trace["expression"] not in sql_statements[index]:
            raise _fail("T7", f"{trace['trace_id']} 的 expression 在 sql[{index}] 里找不到")
        # T8 value 与 value_path 严格相等；指标快照哈希可核
        found = _PATH_RE.match(trace["value_path"])
        if not found:
            raise _fail("T8", f"{trace['trace_id']} 的 value_path={trace['value_path']!r} 不合 §2.6")
        pos, name = int(found.group(1)), found.group(2)
        if pos >= len(rows) or name not in rows[pos]:
            raise _fail("T8", f"{trace['trace_id']} 的 value_path 指不到任何单元格")
        if not _same_value(trace["value"], rows[pos][name], columns, name):
            raise _fail("T8", f"{trace['trace_id']} 的 value 与 {trace['value_path']} 处的值不严格相等")
        if trace["metric_id"] is not None:
            if trace["metric_id"] not in metric_snapshot:
                raise _fail("T8", f"{trace['trace_id']} 的 metric_id 在 metrics_used 里没有快照")
            metric = next(m for m in metrics_used if m["id"] == trace["metric_id"])
            if metric["definition_sha256"] != sha256_text(metric["definition"]):
                raise _fail("T8", f"{trace['trace_id']} 的指标快照哈希对不上 definition")


def _same_value(left, right, columns, name) -> bool:
    digits = next((c["digits"] for c in columns if c["name"] == name), 0)
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        exponent = Decimal(1).scaleb(-max(int(digits), 0))
        a = Decimal(str(left)).quantize(exponent, rounding=ROUND_HALF_EVEN)
        b = Decimal(str(right)).quantize(exponent, rounding=ROUND_HALF_EVEN)
        return a == b and not isinstance(left, str) and not isinstance(right, str)
    return type(left) is type(right) and left == right


# --------------------------------------------------------------------------- #
# CLI（自查用：执行器本身的自检在这里）
# --------------------------------------------------------------------------- #


USAGE = """ask.execute · 只读执行 + 追溯链（契约 §4 / §6.2 / §8.2）

  python -m ask.execute --selftest
"""


def _utf8_stdio() -> None:
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass


def _selftest() -> int:
    """不依赖任何源文件的小自检：导入 → 执行 → provenance → trace → T1–T8。"""
    results = []

    def check(name, fn):
        try:
            fn()
            results.append((True, name, ""))
        except Exception as exc:  # noqa: BLE001 - 自检就是要抓住任何异常
            results.append((False, name, f"{type(exc).__name__}: {exc}"))

    from .schema import Column

    def _table():
        return Table(
            file="ask/fixtures/demo.csv",
            file_sha256="a" * 64,
            sheet=None,
            header_row=1,
            header_row_guessed=False,
            header_reason="第一行是表头",
            columns=[
                Column("region", "text", 0, 1, 0.0, 2),
                Column("amount", "decimal", 2, 2, 0.0, 4),
                Column("qty", "int", 0, 3, 0.25, 2),
            ],
            rows=[
                {"__row_no": 1, "region": "华东", "amount": Decimal("1200.50"), "qty": 3, "__rid": "demo.csv#demo!1"},
                {"__row_no": 2, "region": "华东", "amount": Decimal("800.00"), "qty": 1, "__rid": "demo.csv#demo!2"},
                {"__row_no": 3, "region": "华南", "amount": Decimal("450.25"), "qty": None, "__rid": "demo.csv#demo!3"},
            ],
        )

    def _plan(sql, columns, mode, order_keys, group_by=(), dimensions=()):
        step = {"step_id": 1, "intent": "detail", "title": "自检", "needs": {"metric_ids": [], "dimensions": list(dimensions)}}
        return StepPlan(
            step=step,
            sql_index=0,
            sql=sql,
            table="demo",
            mode=mode,
            columns=columns,
            order_keys=list(order_keys),
            group_by=list(group_by),
            dimensions=list(dimensions),
        )

    def detail_case():
        table = _table()
        conn = build_connection({"demo": table})
        cols = [
            OutColumn("region", "text", None, 0, None, "region", None, ("region",)),
            OutColumn("amount", "decimal", None, 2, None, "amount", None, ("amount",)),
            OutColumn("qty", "int", None, 0, None, "qty", None, ("qty",)),
        ]
        plan = _plan(
            'SELECT "region" AS "region", "amount" AS "amount", "qty" AS "qty" FROM "demo" '
            'ORDER BY "region" ASC, "amount" ASC, "qty" ASC, "__src_rowid" ASC',
            cols,
            "detail",
            ["region", "amount", "qty", SRC_ROWID],
        )
        result = run_step(conn, plan, table=table, options={"row_cap": 100, "trace_row_id_cap": 200})
        assert len(result.rows) == 3, result.rows
        assert result.rows[0]["amount"] == Decimal("800.00"), result.rows[0]
        assert result.rids[0] == ["demo.csv#demo!2"], result.rids
        traces = build_traces(result, table=table, options={"trace_row_id_cap": 200})
        assert [t["trace_id"] for t in traces] == ["t-0001", "t-0002", "t-0003", "t-0004", "t-0005"], traces
        assert traces[0]["value_path"] == "rows[0].amount"
        assert traces[0]["aggregation"] == [{"op": "identity"}]
        assert traces[0]["filter"] == {"none": True}

    def aggregate_case():
        table = _table()
        conn = build_connection({"demo": table})
        metric_def = "SUM(CASE WHEN \"qty\" IS NULL THEN 0 ELSE \"amount\" END)"
        from .metric_dict import Metric

        metric = Metric(
            id="gmv", name="成交额", definition=metric_def, unit="CNY", owner="自检", version=1,
            requires_tables=("demo",), aliases=(), time_column=None, window=None,
            default_aggregation="sum", deprecated=False,
        )
        cols = [
            OutColumn("region", "text", None, 0, None, "region", None, ("region",)),
            OutColumn("gmv", "decimal", "CNY", 2, "gmv", None, metric, ("amount", "qty")),
        ]
        plan = _plan(
            f'SELECT "region" AS "region", {metric_def} AS "gmv" FROM "demo" GROUP BY "region" ORDER BY "region" ASC, "gmv" ASC',
            cols,
            "aggregate",
            ["region", "gmv"],
            group_by=["region"],
            dimensions=["region"],
        )
        result = run_step(conn, plan, table=table, options={"row_cap": 100, "trace_row_id_cap": 200})
        assert len(result.rows) == 2, result.rows
        assert result.rows[1]["__rid"].startswith("agg(demo.csv#demo:region="), result.rows[1]
        traces = build_traces(result, table=table, options={"trace_row_id_cap": 200})
        assert len(traces) == 2, traces
        assert sorted(traces[0]["row_ids"]) == ["demo.csv#demo!1", "demo.csv#demo!2"], traces[0]
        assert traces[0]["filter"]["op"] == "=", traces[0]["filter"]
        assert traces[0]["aggregation"][0]["op"] == "sum", traces[0]["aggregation"]

    def cap_case():
        table = _table()
        conn = build_connection({"demo": table})
        cols = [OutColumn("amount", "decimal", None, 2, None, "amount", None, ("amount",))]
        plan = _plan(
            'SELECT "amount" AS "amount" FROM "demo" ORDER BY "amount" ASC, "__src_rowid" ASC',
            cols,
            "detail",
            ["amount", SRC_ROWID],
        )
        result = run_step(conn, plan, table=table, options={"row_cap": 2, "trace_row_id_cap": 200})
        assert len(result.rows) == 2 and result.truncated_by_row_cap, result

    def readonly_case():
        table = _table()
        conn = build_connection({"demo": table})
        try:
            conn.execute('INSERT INTO "demo" VALUES (9, \'x\', 1, 1)')
        except sqlite3.Error:
            return
        raise AssertionError("query_only 没生效：导入后还能写")

    def nonfinite_case():
        # SQLite 里 1/0 是 NULL 不是 Inf，所以这里直接测转换器：NaN/Inf 一律 RESULT_NONFINITE
        cols = [OutColumn("ratio", "decimal", "ratio", 4, None, None, None, ())]
        for bad in ("nan", "inf", "-Infinity"):
            try:
                convert(bad, cols[0])
            except DecisionError as exc:
                assert exc.code == "RESULT_NONFINITE", exc.code
            else:
                raise AssertionError(f"{bad} 没被挡住")
        assert convert("1.5", cols[0]) == Decimal("1.5000")
        assert convert("-0.00001", cols[0]) == Decimal("0.0000")
        # 空结果不是错误（§7.4）
        table = _table()
        conn = build_connection({"demo": table})
        plan = _plan(
            'SELECT "amount" AS "amount" FROM "demo" WHERE "qty" = 0',
            [OutColumn("amount", "decimal", None, 2, None, "amount", None, ("amount",))],
            "detail",
            ["amount"],
        )
        result = run_step(conn, plan, table=table, options={"row_cap": 10, "trace_row_id_cap": 10})
        assert result.rows == [] and build_traces(result, table=table, options={"trace_row_id_cap": 10}) == []

    def t_case():
        table = _table()
        conn = build_connection({"demo": table})
        cols = [OutColumn("amount", "decimal", None, 2, None, "amount", None, ("amount",))]
        sql = 'SELECT "amount" AS "amount" FROM "demo" ORDER BY "amount" ASC, "__src_rowid" ASC'
        plan = _plan(sql, cols, "detail", ["amount", SRC_ROWID])
        result = run_step(conn, plan, table=table, options={"row_cap": 100, "trace_row_id_cap": 2})
        traces = build_traces(result, table=table, options={"trace_row_id_cap": 200})
        answer_columns = [c.as_answer() for c in cols]
        rows = [{"__rid": r, **row} for row, r in zip(result.rows, result.rids)]
        files = [{"path": table.file, "sha256": table.file_sha256}]
        check_traces(
            traces,
            sql_statements=[result.sql],
            columns=answer_columns,
            rows=rows,
            files=files,
            tables_by_name={"demo": table},
            options={"trace_row_id_cap": 200},
            metrics_used=[],
        )
        # 故意改坏一条，T8 必须报
        broken = [dict(traces[0])]
        broken[0]["value"] = Decimal("999.99")
        try:
            check_traces(
                broken,
                sql_statements=[result.sql],
                columns=answer_columns,
                rows=rows,
                files=files,
                tables_by_name={"demo": table},
                options={"trace_row_id_cap": 200},
                metrics_used=[],
            )
        except DecisionError as exc:
            assert "T8" in exc.message, exc.message
            return
        raise AssertionError("T8 没抓住改坏的 value")

    for name, fn in (
        ("detail", detail_case),
        ("aggregate", aggregate_case),
        ("row_cap", cap_case),
        ("query_only", readonly_case),
        ("nonfinite", nonfinite_case),
        ("T1-T8", t_case),
    ):
        check(name, fn)

    failed = [r for r in results if not r[0]]
    for ok, name, error in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {error}" if error else ""))
    print(f"{len(results) - len(failed)}/{len(results)}")
    return 1 if failed else 0


def main(argv: list | None = None) -> int:
    import argparse

    _utf8_stdio()
    parser = argparse.ArgumentParser(prog="ask.execute", add_help=True, usage=USAGE)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
