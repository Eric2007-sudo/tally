#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指标字典校验器 —— contract/answer.md §3 · V1–V12（+ §8.1 静态检查）。

口径：任何一条不过就报错（退出码 1），一次列出全部错误（V12），
绝不跳过坏条目、绝不自动修正、绝不猜单位、绝不「差不多就用」。

用法（在项目根执行）：
    python dict/validate.py --check dict/metrics.json
    python dict/validate.py --check dict/metrics.json --schema dict/schema.snapshot.json
    python dict/validate.py --check dict/fixtures/metrics.bad.json        # 预期退出码 1
    python dict/validate.py --check dict/metrics.json --write-history dict/history.snapshot.json
    python dict/validate.py --selftest

退出码：0 = 全部通过（字典可加载）；1 = 有错（DICT_LOAD_FAILED，字典整体不予加载）。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------- 契约常量

CONTRACT_VERSION = "1.1"

# §3.1 单位白名单（封闭集）
UNIT_WHITELIST = ("CNY", "%", "人", "次", "个", "天", "ratio", "none")
# §3.1 default_aggregation 枚举
AGG_ENUM = ("sum", "avg", "count", "count_distinct", "min", "max")
# §3.1 window.grain / window.compare 枚举
WINDOW_GRAINS = ("month", "quarter", "year")
WINDOW_COMPARES = ("yoy", "mom")
# §3.1 name ≤20 字、id 正则
NAME_MAX_CHARS = 20
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# §3.1 metric 必填字段
REQUIRED_FIELDS = (
    "id",
    "name",
    "definition",
    "unit",
    "owner",
    "version",
    "requires_tables",
    "aliases",
    "time_column",
    "window",
    "default_aggregation",
    "deprecated",
)

# §8.1 S1 · 写操作关键字
WRITE_KEYWORDS = (
    "INSERT",
    "REPLACE",
    "UPDATE",
    "DELETE",
    "UPSERT",
    "CREATE",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "RENAME",
    "ATTACH",
    "DETACH",
    "VACUUM",
    "REINDEX",
    "ANALYZE",
)
# §8.1 S2 · 事务 / 权限 / 管理语句
ADMIN_KEYWORDS = (
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "PRAGMA",
    "GRANT",
    "REVOKE",
    "SET",
    "COPY",
    "MERGE",
    "CALL",
    "EXEC",
    "EXECUTE",
)
# §8.1 S4 · 副作用函数 / 破坏确定性
SIDE_EFFECT_CALLS = (
    "load_extension",
    "readfile",
    "writefile",
    "edit",
    "fts3_tokenizer",
    "random",
    "randomblob",
    "changes",
    "total_changes",
    "last_insert_rowid",
)

# T5 的「关键字 / 函数白名单」：这些标识符不算列名，不需要在 schema 里存在。
SQL_KEYWORDS = frozenset(
    """
    SELECT FROM WHERE GROUP BY HAVING ORDER LIMIT OFFSET AS AND OR NOT IN IS NULL
    BETWEEN LIKE ESCAPE CASE WHEN THEN ELSE END DISTINCT ALL UNION INTERSECT EXCEPT
    JOIN INNER LEFT RIGHT FULL OUTER CROSS ON USING NATURAL ASC DESC FIRST LAST
    TRUE FALSE CAST EXTRACT INTERVAL COLLATE EXISTS
    """.split()
)
SQL_FUNCTIONS = frozenset(
    """
    SUM AVG COUNT MIN MAX TOTAL GROUP_CONCAT STRING_AGG COALESCE IFNULL NULLIF IIF
    ABS ROUND CEIL CEILING FLOOR SIGN POWER POW SQRT MOD
    REPLACE SUBSTR SUBSTRING LENGTH LOWER UPPER TRIM LTRIM RTRIM INSTR
    DATE DATETIME TIME STRFTIME JULIANDAY UNIXEPOCH CURRENT_DATE CURRENT_TIMESTAMP CURRENT_TIME
    PRINTF FORMAT
    INTEGER INT REAL TEXT BLOB NUMERIC DECIMAL BOOLEAN
    """.split()
)
SQL_IDENT_ALLOWED = SQL_KEYWORDS | SQL_FUNCTIONS


# ---------------------------------------------------------------- 小工具


def err(code: str, rule: str, metric_id, message: str) -> dict:
    """一条错误。code 只能取契约 §2.11 的封闭枚举，rule 是 §3.2 的规则号。"""
    return {"code": code, "rule": rule, "metric_id": metric_id, "message": message}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mask_literals(text: str):
    """剥离字符串字面量（返回掩码文本 + 字面量列表）。

    - 单引号字面量整体替换成一个空格（保证词边界，长度不再对齐，够用）；
    - 双引号标识符去掉引号边界但保留内容（它是标识符，要参与列名校验）。
    """
    out = []
    literals = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            buf = []
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            literals.append("".join(buf))
            out.append(" ")
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            buf = []
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), literals


def keyword_hits(masked: str, words) -> list[str]:
    """按词边界、大小写不敏感匹配关键字。

    §8.1 的唯一例外：`REPLACE(` 形态放行（REPLACE 字符串函数），
    但 `REPLACE` 作为语句关键字（后面不是 `(`）照样拒。
    """
    hits = []
    for word in words:
        pattern = re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE)
        for m in pattern.finditer(masked):
            if word == "REPLACE" and re.match(r"\s*\(", masked[m.end():]):
                continue
            hits.append(m.group(0))
    return hits


def call_hits(masked: str) -> list[str]:
    hits = []
    for name in SIDE_EFFECT_CALLS:
        if re.search(r"\b" + re.escape(name) + r"\s*\(", masked, re.IGNORECASE):
            hits.append(name + "(")
    return hits


def static_problems(definition: str) -> list[str]:
    """§8.1 静态检查（S1/S2/S3-多语句/S4），用于 V3。"""
    if not isinstance(definition, str) or not definition.strip():
        return ["definition 为空"]
    masked, literals = mask_literals(definition)
    problems = []
    if ";" in masked:
        problems.append("含 ;（多语句）")
    if "--" in masked or "/*" in masked:
        problems.append("含注释（-- 或 /* */），注释里能藏关键字")
    write = keyword_hits(masked, WRITE_KEYWORDS)
    if write:
        problems.append("含写操作关键字: " + ", ".join(sorted({w.upper() for w in write})))
    admin = keyword_hits(masked, ADMIN_KEYWORDS)
    if admin:
        problems.append("含事务/权限/管理关键字: " + ", ".join(sorted({w.upper() for w in admin})))
    calls = call_hits(masked)
    if calls:
        problems.append("含副作用/破坏确定性函数: " + ", ".join(sorted(set(calls))))
    if any(lit.strip().lower() == "now" for lit in literals):
        problems.append("含 'now' 时间字面量（时间只能来自 D1 的 time_range，否则同问同答作废）")
    return problems


def limit_order_problems(masked: str) -> list[str]:
    """V9：指标只描述「一个数」，截断/排序属于查询层。"""
    problems = []
    if re.search(r"\bLIMIT\b", masked, re.IGNORECASE):
        problems.append("含 LIMIT（截断属于查询层，指标只描述一个数）")
    if re.search(r"\bORDER\s+BY\b", masked, re.IGNORECASE):
        problems.append("含 ORDER BY（排序属于查询层，指标只描述一个数）")
    return problems


def strip_outer_parens(text: str) -> str:
    s = text.strip()
    while s.startswith("(") and s.endswith(")") and len(s) > 1:
        depth = 0
        wraps_all = True
        for idx, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and idx != len(s) - 1:
                    wraps_all = False
                    break
        if not wraps_all:
            break
        s = s[1:-1].strip()
    return s


def outer_aggregation(masked: str):
    """V11：取 definition 的外层聚合函数；返回 sum/avg/count/count_distinct/min/max 或 None。"""
    s = strip_outer_parens(masked)
    m = re.match(r"(SUM|AVG|COUNT|MIN|MAX)\s*\(", s, re.IGNORECASE)
    if not m:
        return None
    fn = m.group(1).lower()
    if fn == "count" and re.match(r"\s*DISTINCT\b", s[m.end():], re.IGNORECASE):
        return "count_distinct"
    return fn


def extract_refs(masked: str):
    """从 definition 里抽出列引用 + 未知函数调用（T5 的机械判据）。"""
    qualified = re.findall(
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)", masked
    )
    rest = re.sub(r"[A-Za-z_][A-Za-z0-9_]*\s*\.\s*[A-Za-z_][A-Za-z0-9_]*", " ", masked)
    columns = []
    unknown_functions = []
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", rest):
        name = m.group(0)
        if name.upper() in SQL_IDENT_ALLOWED:
            continue
        if re.match(r"\s*\(", rest[m.end():]):
            unknown_functions.append(name)
            continue
        columns.append((None, name))
    for table, column in qualified:
        if table.upper() in SQL_IDENT_ALLOWED and column.upper() in SQL_IDENT_ALLOWED:
            continue
        columns.append((table, column))
    return columns, unknown_functions


# ---------------------------------------------------------------- schema 快照


class Schema:
    """schema 快照 → 表/列索引。

    快照形状直接用契约 §5.1 D1 输入里的 `schema`：
        [{"file": "eval/data/sales.xlsx", "sha256": ..., "sheets": [
            {"name": "Sheet1", "columns": [{"name": "amount", "type": "decimal",
                                            "null_ratio": 0.0, "unique_count": 0}]}]}]

    表身份（契约 §3.2 V4/V5 只说「当前 schema 快照」，没写表名怎么来；本文件取
    §2.5 `__rid` 与 §2.15 `d3_snapshot` 已经用到的两个名字）：
      - 主名 = 文件 basename 去掉扩展名（`sales.xlsx` → `sales`）；
      - 别名 = sheet 名（同一文件多 sheet 时主名改成 `sales#Sheet1` 这种形式）。
    requires_tables / definition 里的限定名用主名或别名都能解析；见 dict/README.md。
    """

    def __init__(self, entries):
        self.entries = entries
        self.by_name = {}
        self.columns_to_ids = {}
        for entry in entries:
            for alias in entry["aliases"]:
                self.by_name.setdefault(alias, set()).add(entry["id"])
            for column in entry["columns"]:
                self.columns_to_ids.setdefault(column, set()).add(entry["id"])

    def ids_for(self, name: str) -> set:
        return set(self.by_name.get(name, ()))

    def ids_with_column(self, column: str) -> set:
        return set(self.columns_to_ids.get(column, ()))

    def ids_with_column_ci(self, column: str) -> set:
        low = column.lower()
        return {
            entry["id"]
            for entry in self.entries
            if any(c.lower() == low for c in entry["columns"])
        }

    def columns_of(self, entry_id: str) -> set:
        for entry in self.entries:
            if entry["id"] == entry_id:
                return entry["columns"]
        return set()

    def all_columns(self) -> set:
        return set(self.columns_to_ids)


def build_schema(snapshot) -> Schema:
    """按契约 §5.1 的 schema 形状建索引；形状不对就抛，不猜。

    v1.1：字典侧的快照文件是一个带版本串的**文档**（`{"contract_version":"1.1","schema":[...]}`，
    见 dict/README.md 判断 4），版本串必须等于 CONTRACT_VERSION，否则整份字典不予加载。
    裸数组（契约 §5.1 的 `schema` 值本身）仍然接受，供 --selftest / 调用方直接传 schema。
    """
    if isinstance(snapshot, dict):
        if "contract_version" not in snapshot:
            raise ValueError(
                "schema 快照文件缺 contract_version（字典侧快照一律带版本串，见 dict/README.md 判断 4）"
            )
        if snapshot["contract_version"] != CONTRACT_VERSION:
            raise ValueError(
                f"schema 快照的 contract_version 必须是 {CONTRACT_VERSION!r}"
                f"（当前 {snapshot['contract_version']!r}）：版本对不上就不加载"
            )
        snapshot = snapshot.get("schema")
    if not isinstance(snapshot, list) or not snapshot:
        raise ValueError("schema 快照必须是文件条目的数组（契约 §5.1 形状）")
    raw = []
    for fi, file_entry in enumerate(snapshot):
        if not isinstance(file_entry, dict) or "file" not in file_entry:
            raise ValueError(f"schema 快照第 {fi} 项缺 file")
        base = PurePosixPath(str(file_entry["file"]).replace("\\", "/")).stem
        sheets = file_entry.get("sheets")
        if not isinstance(sheets, list) or not sheets:
            raise ValueError(f"schema 快照 {file_entry['file']} 缺 sheets")
        for si, sheet in enumerate(sheets):
            if not isinstance(sheet, dict) or "name" not in sheet:
                raise ValueError(f"schema 快照 {file_entry['file']} 第 {si} 个 sheet 缺 name")
            columns = sheet.get("columns")
            if not isinstance(columns, list):
                raise ValueError(f"schema 快照 {file_entry['file']}/{sheet['name']} 缺 columns")
            names = set()
            for column in columns:
                if not isinstance(column, dict) or not isinstance(column.get("name"), str):
                    raise ValueError(
                        f"schema 快照 {file_entry['file']}/{sheet['name']} 里有列缺 name"
                    )
                names.add(column["name"])
            raw.append({"base": base, "sheet": sheet["name"], "columns": names})

    base_counts = {}
    for item in raw:
        base_counts[item["base"]] = base_counts.get(item["base"], 0) + 1

    entries = []
    for item in raw:
        entry_id = item["base"] if base_counts[item["base"]] == 1 else f"{item['base']}#{item['sheet']}"
        aliases = {entry_id, item["base"], item["sheet"]}
        entries.append({"id": entry_id, "aliases": aliases, "columns": set(item["columns"])})
    return Schema(entries)


# ---------------------------------------------------------------- 逐条目校验


def check_definition_refs(mid, masked, schema: Schema, requires_tables, findings) -> bool:
    """V4（列/表必须存在）+ V5（引用的表集合恰好等于 requires_tables）。返回 refs 是否可机械判定。"""
    columns, unknown_functions = extract_refs(masked)
    if unknown_functions:
        findings.append(
            err(
                "SQL_STATIC_REJECT",
                "V3",
                mid,
                "definition 里出现不在函数白名单里的调用: "
                + ", ".join(sorted(set(unknown_functions))),
            )
        )

    required_ids = set()
    tables_ok = True
    for table in requires_tables:
        ids = schema.ids_for(table)
        if not ids:
            tables_ok = False
            hint = difflib.get_close_matches(table, sorted(schema.by_name), n=1, cutoff=0.6)
            findings.append(
                err(
                    "COLUMN_NOT_FOUND",
                    "V4",
                    mid,
                    f"requires_tables 里的表 {table!r} 不在 schema 快照里"
                    + (f"；最接近的是 {hint[0]!r}（不自动替换）" if hint else ""),
                )
            )
        else:
            required_ids |= ids

    used_ids = set()
    refs_ok = True
    seen = set()
    for qualifier, column in columns:
        key = (qualifier, column)
        if key in seen:
            continue
        seen.add(key)

        candidates = schema.ids_with_column(column)
        if qualifier is not None:
            qualifier_ids = schema.ids_for(qualifier)
            if not qualifier_ids:
                refs_ok = False
                findings.append(
                    err(
                        "COLUMN_NOT_FOUND",
                        "V4",
                        mid,
                        f"definition 里的限定名 {qualifier!r} 在 schema 快照里没有对应表",
                    )
                )
                continue
            candidates &= qualifier_ids

        if len(candidates) > 1 and required_ids:
            narrowed = candidates & required_ids
            if len(narrowed) == 1:
                candidates = narrowed

        if not candidates:
            refs_ok = False
            all_columns = sorted(schema.all_columns())
            case_hint = sorted({c for c in all_columns if c.lower() == column.lower()})
            hint = difflib.get_close_matches(column, all_columns, n=3, cutoff=0.6)
            message = f"definition 引用的列 {column!r} 不在 schema 快照里"
            if case_hint:
                message += f"；schema 里大小写不同的是 {case_hint[0]!r}（不自动替换）"
            elif hint:
                message += "；最接近的是 " + ", ".join(repr(h) for h in hint) + "（不自动替换）"
            if qualifier is not None:
                message += f"（限定名 {qualifier!r} 下也没有）"
            findings.append(err("COLUMN_NOT_FOUND", "V4", mid, message))
            continue

        if len(candidates) > 1:
            refs_ok = False
            findings.append(
                err(
                    "METRIC_INVALID",
                    "V5",
                    mid,
                    f"列 {column!r} 同时存在于多张表 {sorted(candidates)}，"
                    "requires_tables 无法唯一确定引用的是哪张（不猜）",
                )
            )
        used_ids |= candidates

    if refs_ok and tables_ok and used_ids != required_ids:
        extra = sorted(used_ids - required_ids)
        missing = sorted(required_ids - used_ids)
        parts = []
        if extra:
            parts.append("多声明: " + ", ".join(extra))
        if missing:
            parts.append("少声明: " + ", ".join(missing))
        findings.append(
            err(
                "METRIC_INVALID",
                "V5",
                mid,
                "definition 实际引用的表集合 != requires_tables（" + "；".join(parts) + "）",
            )
        )
    return refs_ok and tables_ok


def check_metric(index, metric, schema: Schema, history, findings) -> None:
    if not isinstance(metric, dict):
        findings.append(err("METRIC_INVALID", "0", None, f"metrics[{index}] 不是对象"))
        return

    mid = metric.get("id") if isinstance(metric.get("id"), str) else None

    missing = [field for field in REQUIRED_FIELDS if field not in metric]
    if missing:
        findings.append(
            err("METRIC_INVALID", "V1", mid, f"缺必填字段: {', '.join(missing)}（不许省略）")
        )

    # V1 · id 正则
    if not isinstance(metric.get("id"), str) or not ID_RE.match(metric.get("id") or ""):
        findings.append(
            err(
                "METRIC_INVALID",
                "V1",
                mid,
                "id 不匹配 ^[a-z][a-z0-9_]{1,31}$（当前 "
                + repr(metric.get("id"))
                + "）",
            )
        )

    # V2 · name
    name = metric.get("name")
    if not isinstance(name, str) or not name.strip():
        findings.append(err("METRIC_INVALID", "V2", mid, "name 为空"))
    elif len(name) > NAME_MAX_CHARS:
        findings.append(
            err(
                "METRIC_INVALID",
                "V2",
                mid,
                f"name 超过 {NAME_MAX_CHARS} 字（当前 {len(name)} 字）",
            )
        )

    # V3 · definition 非空 + §8.1 静态检查
    definition = metric.get("definition")
    masked = ""
    static_failed = False
    if not isinstance(definition, str) or not definition.strip():
        findings.append(err("SQL_STATIC_REJECT", "V3", mid, "definition 为空"))
        static_failed = True
    else:
        masked = mask_literals(definition)[0]
        problems = static_problems(definition)
        for problem in problems:
            findings.append(err("SQL_STATIC_REJECT", "V3", mid, problem))
        static_failed = bool(problems)
        for problem in limit_order_problems(masked):
            findings.append(err("METRIC_INVALID", "V9", mid, problem))

    # V6 · unit 白名单
    unit = metric.get("unit")
    if unit not in UNIT_WHITELIST:
        findings.append(
            err(
                "METRIC_INVALID",
                "V6",
                mid,
                f"unit {unit!r} 不在白名单 {list(UNIT_WHITELIST)} 内（不猜单位）",
            )
        )

    # V7 · owner 非空
    owner = metric.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        findings.append(err("METRIC_INVALID", "V7", mid, "owner 为空（口径负责人必须写出来）"))

    # V8 · version ≥1 整数；同 id 同 version 的 definition 哈希必须一致
    version = metric.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        findings.append(
            err(
                "METRIC_INVALID",
                "V8",
                mid,
                f"version 必须是 ≥1 的整数（当前 {version!r}）",
            )
        )
    elif isinstance(definition, str) and mid is not None:
        previous = history.get(mid) if isinstance(history, dict) else None
        if isinstance(previous, dict) and str(version) in previous:
            current_hash = sha256_text(definition)
            if previous[str(version)] != current_hash:
                findings.append(
                    err(
                        "METRIC_INVALID",
                        "V8",
                        mid,
                        f"同一个版本两个口径：{mid} v{version} 历史哈希 "
                        f"{str(previous[str(version)])[:12]}… 当前 {current_hash[:12]}…",
                    )
                )

    # V5 前置 · requires_tables
    requires_tables = metric.get("requires_tables")
    requires_ok = True
    if (
        not isinstance(requires_tables, list)
        or not requires_tables
        or not all(isinstance(t, str) and t.strip() for t in requires_tables)
    ):
        requires_ok = False
        findings.append(
            err("METRIC_INVALID", "V5", mid, "requires_tables 必须是非空字符串数组")
        )
        requires_tables = []
    else:
        requires_tables = list(requires_tables)

    # aliases 可空，但元素必须是字符串
    aliases = metric.get("aliases")
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        findings.append(err("METRIC_INVALID", "V1", mid, "aliases 必须是字符串数组（可为空数组）"))

    # V10 · window 非空 ⟺ time_column 非空
    time_column = metric.get("time_column")
    window = metric.get("window")
    time_column_ok = True
    if time_column is not None and (not isinstance(time_column, str) or not time_column.strip()):
        time_column_ok = False
        findings.append(
            err("METRIC_INVALID", "V10", mid, "time_column 要么是列名（非空字符串），要么是 null")
        )
    if window is not None:
        if not isinstance(window, dict):
            findings.append(err("METRIC_INVALID", "V10", mid, "window 必须是对象或 null"))
        else:
            extra_keys = sorted(set(window) - {"grain", "compare"})
            if extra_keys:
                findings.append(
                    err(
                        "METRIC_INVALID",
                        "V10",
                        mid,
                        "window 只许含 grain / compare，多了: " + ", ".join(extra_keys),
                    )
                )
            if window.get("grain") not in WINDOW_GRAINS:
                findings.append(
                    err(
                        "METRIC_INVALID",
                        "V10",
                        mid,
                        f"window.grain {window.get('grain')!r} 不在枚举 {list(WINDOW_GRAINS)} 内",
                    )
                )
            if window.get("compare") not in WINDOW_COMPARES:
                findings.append(
                    err(
                        "METRIC_INVALID",
                        "V10",
                        mid,
                        f"window.compare {window.get('compare')!r} 不在枚举 {list(WINDOW_COMPARES)} 内",
                    )
                )
    has_time = time_column is not None and time_column_ok
    if (window is not None) != has_time:
        findings.append(
            err(
                "METRIC_INVALID",
                "V10",
                mid,
                "window 非空 ⟺ time_column 非空（同比/环比到底比哪一期不允许猜）",
            )
        )

    # V11 · default_aggregation 枚举 + 与 definition 外层聚合一致
    default_aggregation = metric.get("default_aggregation")
    if default_aggregation not in AGG_ENUM:
        findings.append(
            err(
                "METRIC_INVALID",
                "V11",
                mid,
                f"default_aggregation {default_aggregation!r} 不在枚举 {list(AGG_ENUM)} 内",
            )
        )
    elif masked:
        outer = outer_aggregation(masked)
        if outer is None:
            findings.append(
                err(
                    "METRIC_INVALID",
                    "V11",
                    mid,
                    "无法从 definition 确定外层聚合函数（必须以 SUM/AVG/COUNT/MIN/MAX 开头，"
                    "否则「这个数是什么聚合」说不清，不放宽）",
                )
            )
        elif outer != default_aggregation:
            findings.append(
                err(
                    "METRIC_INVALID",
                    "V11",
                    mid,
                    f"default_aggregation={default_aggregation!r} 与 definition 外层聚合 "
                    f"{outer!r} 不一致",
                )
            )

    # deprecated 必须显式 bool
    if not isinstance(metric.get("deprecated"), bool):
        findings.append(err("METRIC_INVALID", "V1", mid, "deprecated 必须是 bool（不许省略）"))

    # V4 / V5 · 列名表名必须落在 schema 快照里；引用的表集合恰好等于 requires_tables
    # 静态检查已经否决的 definition 不再解列名（那条 SQL 无论如何都不会被执行，
    # 再报一串 COLUMN_NOT_FOUND 只会淹没真正的第一条错误）。
    if masked and not static_failed and requires_ok and schema is not None:
        check_definition_refs(mid, masked, schema, requires_tables, findings)


# ---------------------------------------------------------------- 整份字典


def validate_dict(doc, schema: Schema, history=None) -> list:
    """整份字典校验：一次列出全部错误（V12），不 fail-fast、不跳过坏条目。"""
    history = history or {}
    findings = []

    if not isinstance(doc, dict):
        return [err("DICT_LOAD_FAILED", "0", None, "字典顶层必须是 JSON 对象")]

    contract_version = doc.get("contract_version")
    if contract_version != CONTRACT_VERSION:
        findings.append(
            err(
                "DICT_LOAD_FAILED",
                "0",
                None,
                f"contract_version 必须是 {CONTRACT_VERSION!r}（当前 {contract_version!r}）",
            )
        )

    metrics = doc.get("metrics")
    if not isinstance(metrics, list):
        findings.append(err("DICT_LOAD_FAILED", "0", None, "顶层缺 metrics 数组"))
        return findings

    for index, metric in enumerate(metrics):
        check_metric(index, metric, schema, history, findings)

    # V1 · id 全局唯一
    ids = [
        m.get("id")
        for m in metrics
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    ]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        findings.append(err("METRIC_INVALID", "V1", None, "id 重复: " + ", ".join(duplicates)))

    # V2 · 同一 name 只许对应一个 id
    by_name = {}
    for metric in metrics:
        if isinstance(metric, dict) and isinstance(metric.get("name"), str):
            by_name.setdefault(metric["name"], []).append(metric.get("id"))
    for name, id_list in sorted(by_name.items()):
        if len(id_list) > 1:
            findings.append(
                err(
                    "METRIC_INVALID",
                    "V2",
                    None,
                    f"两个 id 抢同一个中文名 {name!r}: "
                    + ", ".join(str(i) for i in id_list)
                    + "（中文匹配会歧义）",
                )
            )

    # §3 · metrics 按 id 字节序排列（否则字典整体哈希不稳）
    if ids != sorted(ids, key=lambda s: s.encode("utf-8")):
        findings.append(
            err(
                "METRIC_INVALID",
                "3",
                None,
                "metrics 必须按 id 字节序排列（否则 metric_dict_sha256 不稳）：当前顺序 "
                + ", ".join(ids),
            )
        )

    return findings


# ---------------------------------------------------------------- 自测


def _selftest_schema():
    return [
        {
            "file": "eval/data/sales.xlsx",
            "sha256": None,
            "sheets": [
                {
                    "name": "Sheet1",
                    "columns": [
                        {"name": "order_id", "type": "text"},
                        {"name": "region", "type": "text"},
                        {"name": "amount", "type": "decimal"},
                        {"name": "status", "type": "text"},
                        {"name": "sale_date", "type": "date"},
                    ],
                }
            ],
        },
        {
            "file": "eval/data/region_dim.xlsx",
            "sha256": None,
            "sheets": [
                {
                    "name": "Sheet1",
                    "columns": [
                        {"name": "region", "type": "text"},
                        {"name": "region_name", "type": "text"},
                    ],
                }
            ],
        },
    ]


def _base_metric(**overrides):
    metric = {
        "id": "gmv",
        "name": "成交额",
        "definition": "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
        "unit": "CNY",
        "owner": "运营-王工",
        "version": 1,
        "requires_tables": ["sales"],
        "aliases": ["销售额"],
        # V10 是双向的（window 非空 ⟺ time_column 非空），见 dict/README.md
        # 「V10 的读法」：非 window 指标这里必须是 null。
        "time_column": None,
        "window": None,
        "default_aggregation": "sum",
        "deprecated": False,
    }
    metric.update(overrides)
    return metric


def _doc(*metrics, contract_version=CONTRACT_VERSION):
    ordered = sorted(metrics, key=lambda m: str(m.get("id")).encode("utf-8"))
    return {"contract_version": contract_version, "metrics": ordered}


def run_selftest() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    schema = build_schema(_selftest_schema())
    failures = []

    def case(name, doc, expected_codes, history=None):
        findings = validate_dict(doc, schema, history or {})
        got = sorted({f["code"] for f in findings})
        expected = sorted(set(expected_codes))
        status = "PASS" if got == expected else "FAIL"
        if status == "FAIL":
            failures.append(f"{name}: 期望 {expected} 实得 {got}")
            detail = " | ".join(f"[{f['code']}/{f['rule']}] {f['message']}" for f in findings)
            print(f"{status} {name}\n     期望 {expected} 实得 {got}\n     {detail}")
        else:
            print(f"{status} {name}")

    # 正例
    case("正向·最小合法字典", _doc(_base_metric()), [])
    case(
        "正向·REPLACE(...) 字符串函数放行（§8.1 唯一例外）",
        _doc(
            _base_metric(
                definition="COUNT(DISTINCT REPLACE(order_id, 'X', ''))",
                default_aggregation="count_distinct",
                unit="个",
            )
        ),
        [],
    )
    case(
        "正向·列在多张表里靠 requires_tables 唯一确定",
        _doc(_base_metric(definition="COUNT(DISTINCT region)", default_aggregation="count_distinct")),
        [],
    )
    case(
        "正向·同比指标 window + time_column 齐备",
        _doc(
            _base_metric(
                id="gmv_mom",
                name="成交额环比",
                time_column="sale_date",
                window={"grain": "month", "compare": "mom"},
            )
        ),
        [],
    )

    # 反例
    case("反例·REPLACE 非函数形态", _doc(_base_metric(definition="SUM(amount) REPLACE")), ["SQL_STATIC_REJECT"])
    case("反例·id 大写", _doc(_base_metric(id="GMV")), ["METRIC_INVALID"])
    case(
        "反例·id 重复",
        _doc(_base_metric(), _base_metric(name="成交额（重）")),
        ["METRIC_INVALID"],
    )
    case(
        "反例·两个 id 抢同一个中文名",
        _doc(_base_metric(), _base_metric(id="gmv2")),
        ["METRIC_INVALID"],
    )
    case("反例·unit 不在白名单", _doc(_base_metric(unit="元")), ["METRIC_INVALID"])
    case("反例·owner 为空", _doc(_base_metric(owner="  ")), ["METRIC_INVALID"])
    case("反例·version 0", _doc(_base_metric(version=0)), ["METRIC_INVALID"])
    case(
        "反例·同 id 同 version 两个口径（V8 历史漂移）",
        _doc(_base_metric()),
        ["METRIC_INVALID"],
        history={"gmv": {"1": "0" * 64}},
    )
    case("反例·含 LIMIT", _doc(_base_metric(definition="SUM(amount) LIMIT 10")), ["METRIC_INVALID"])
    case(
        "反例·含 ORDER BY",
        _doc(_base_metric(definition="SUM(amount) ORDER BY amount")),
        ["METRIC_INVALID"],
    )
    case("反例·含分号多语句", _doc(_base_metric(definition="SUM(amount); SUM(amount)")), ["SQL_STATIC_REJECT"])
    case("反例·含写操作关键字", _doc(_base_metric(definition="SUM(amount) DELETE")), ["SQL_STATIC_REJECT"])
    case("反例·含注释", _doc(_base_metric(definition="SUM(amount) -- 注释")), ["SQL_STATIC_REJECT"])
    case(
        "反例·含 'now' 字面量",
        _doc(_base_metric(definition="SUM(CASE WHEN sale_date <= 'now' THEN amount ELSE 0 END)")),
        ["SQL_STATIC_REJECT"],
    )
    case(
        "反例·window 非空但 time_column 为 null",
        _doc(_base_metric(time_column=None, window={"grain": "month", "compare": "yoy"})),
        ["METRIC_INVALID"],
    )
    case(
        "反例·time_column 非空但 window 为 null（V10 是双向的）",
        _doc(_base_metric(time_column="sale_date")),
        ["METRIC_INVALID"],
    )
    case(
        "反例·window.grain 不在枚举",
        _doc(_base_metric(window={"grain": "week", "compare": "yoy"})),
        ["METRIC_INVALID"],
    )
    case("反例·default_aggregation 与外层聚合不一致", _doc(_base_metric(default_aggregation="avg")), ["METRIC_INVALID"])
    case("反例·引用的列不存在（带最近提示）", _doc(_base_metric(definition="SUM(amout)")), ["COLUMN_NOT_FOUND"])
    case("反例·requires_tables 里的表不存在", _doc(_base_metric(requires_tables=["salez"])), ["COLUMN_NOT_FOUND"])
    case(
        "反例·V5 多声明表",
        _doc(_base_metric(requires_tables=["sales", "region_dim"])),
        ["METRIC_INVALID"],
    )
    case("反例·V5 少声明表", _doc(_base_metric(requires_tables=["region_dim"])), ["METRIC_INVALID"])
    case(
        "反例·缺必填字段",
        _doc({k: v for k, v in _base_metric().items() if k != "owner"}),
        ["METRIC_INVALID"],
    )
    case(
        "反例·metrics 未按 id 字节序排列",
        {
            "contract_version": CONTRACT_VERSION,
            "metrics": [_base_metric(), _base_metric(id="abc", name="另一个")],
        },
        ["METRIC_INVALID"],
    )
    case("反例·contract_version 不对", _doc(_base_metric(), contract_version="0.9"), ["DICT_LOAD_FAILED"])

    # v1.1 · 侧挂快照文件的版本串（dict/README.md 判断 4 / 判断 5）
    for name, snapshot, expect_ok in (
        (
            "正向·schema 快照文档带 contract_version=1.1",
            {"contract_version": CONTRACT_VERSION, "schema": _selftest_schema()},
            True,
        ),
        (
            "反例·schema 快照版本串是 1.0",
            {"contract_version": "1.0", "schema": _selftest_schema()},
            False,
        ),
        ("反例·schema 快照缺 contract_version", {"schema": _selftest_schema()}, False),
    ):
        try:
            build_schema(snapshot)
            ok, detail = True, ""
        except ValueError as exc:
            ok, detail = False, str(exc)
        if ok == expect_ok:
            print(f"PASS {name}")
        else:
            failures.append(f"{name}: 期望 {'通过' if expect_ok else '报错'} 实得 {'通过' if ok else detail}")
            print(f"FAIL {name} {detail}")

    if failures:
        print(f"\nSELFTEST FAILED: {len(failures)} 个用例不符")
        for failure in failures:
            print(" -", failure)
        return 1
    print("\nSELFTEST OK: 全部用例通过")
    return 0


# ---------------------------------------------------------------- CLI


def _resolve_sidecar(explicit_path, dict_path: Path, filename: str) -> Path:
    """旁挂文件（schema/history 快照）定位：显式给 > 字典同目录 > 校验器同目录（dict/）。"""
    if explicit_path:
        return Path(explicit_path)
    candidates = [dict_path.parent / filename, Path(__file__).resolve().parent / filename]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="指标字典校验器（contract/answer.md §3 · V1–V12）"
    )
    parser.add_argument("--check", metavar="PATH", help="要校验的指标字典 JSON")
    parser.add_argument("--schema", metavar="PATH", help="schema 快照；默认取字典同目录的 schema.snapshot.json")
    parser.add_argument("--history", metavar="PATH", help="历史快照；默认取字典同目录的 history.snapshot.json")
    parser.add_argument("--write-history", metavar="PATH", help="校验通过后把 {id: {version: sha256}} 落盘")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出错误清单")
    parser.add_argument("--selftest", action="store_true", help="跑内置正反例")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if not args.check:
        parser.error("必须给 --check PATH（或 --selftest）")

    dict_path = Path(args.check)
    findings = []
    doc = None
    try:
        doc = json.loads(dict_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        findings.append(err("DICT_LOAD_FAILED", "0", None, f"字典文件不存在: {args.check}"))
    except json.JSONDecodeError as exc:
        findings.append(err("DICT_LOAD_FAILED", "0", None, f"字典不是合法 JSON: {exc}"))

    schema = None
    if doc is not None:
        schema_path = _resolve_sidecar(args.schema, dict_path, "schema.snapshot.json")
        try:
            schema = build_schema(json.loads(schema_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            findings.append(
                err(
                    "SCHEMA_UNREADABLE",
                    "V4",
                    None,
                    f"缺 schema 快照 {str(schema_path)!r}：V4/V5 无法机械执行，不放宽（用 --schema 指定）",
                )
            )
        except Exception as exc:  # 形状不对也一律报错
            findings.append(
                err("SCHEMA_UNREADABLE", "V4", None, f"schema 快照读不了: {exc}")
            )

    history = {}
    history_note = None
    if doc is not None:
        history_path = _resolve_sidecar(args.history, dict_path, "history.snapshot.json")
        try:
            history_doc = json.loads(history_path.read_text(encoding="utf-8"))
            if not isinstance(history_doc, dict):
                findings.append(err("DICT_LOAD_FAILED", "V8", None, "历史快照顶层必须是对象"))
                history_doc = {}
            elif history_doc.get("contract_version") != CONTRACT_VERSION:
                # v1.1 · 版本串对不上的历史快照不许拿来锁「同版本同口径」
                findings.append(
                    err(
                        "DICT_LOAD_FAILED",
                        "V8",
                        None,
                        f"历史快照的 contract_version 必须是 {CONTRACT_VERSION!r}"
                        f"（当前 {history_doc.get('contract_version')!r}）：版本对不上就不加载",
                    )
                )
                history_doc = {}
            history = history_doc.get("history", {})
            if not isinstance(history, dict):
                findings.append(err("DICT_LOAD_FAILED", "V8", None, "历史快照的 history 必须是对象"))
                history = {}
        except FileNotFoundError:
            history_note = (
                f"V8: 没有历史快照 {str(history_path)!r}，本次只校验了 version 类型；"
                "要锁死「同版本同口径」用 --write-history 落盘"
            )
        except Exception as exc:
            findings.append(err("DICT_LOAD_FAILED", "V8", None, f"历史快照读不了: {exc}"))

    if doc is not None and schema is not None:
        findings.extend(validate_dict(doc, schema, history))

    if args.json:
        print(json.dumps({"findings": findings}, ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(
                f"[{finding['code']}] {finding['rule']} {finding['metric_id'] or '-'}: "
                f"{finding['message']}"
            )
        if history_note:
            print("注意: " + history_note)

    if findings:
        print(
            f"DICT_LOAD_FAILED: {len(findings)} 处错误；按契约 §3.2 V12 整份字典不予加载"
            "（不跳过坏条目，宁可不答）"
        )
        return 1

    metrics = doc.get("metrics") or []
    print(f"OK: {len(metrics)} 个指标全部通过 V1–V12（contract_version={CONTRACT_VERSION}）")

    if args.write_history:
        history_path = Path(args.write_history)
        payload = {"contract_version": CONTRACT_VERSION, "history": {}}
        if history_path.exists():
            try:
                payload["history"] = json.loads(history_path.read_text(encoding="utf-8")).get(
                    "history", {}
                )
            except Exception:
                payload["history"] = {}
        for metric in metrics:
            if (
                isinstance(metric, dict)
                and isinstance(metric.get("id"), str)
                and isinstance(metric.get("version"), int)
                and isinstance(metric.get("definition"), str)
            ):
                payload["history"].setdefault(metric["id"], {})[
                    str(metric["version"])
                ] = sha256_text(metric["definition"])
        payload["history"] = {
            key: dict(sorted(value.items()))
            for key, value in sorted(payload["history"].items())
        }
        history_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"history 已写入: {args.write_history}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
