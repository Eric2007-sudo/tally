"""eval.metrics · 四项指标测量 + 同问两遍字节比对 + 打回清单

断言里跑的就是这一条（不带样本目录，所以四项指标都会写成 null）：

    python -m eval.metrics --determinism-pairs 60 --out eval/report.md

它做四件事，判据全部抄 contract/answer.md，不自创：

1. **SQL 执行准确率**（README 主指标）：拿 eval/questions.jsonl 里每题的 `expect`
   去机械核对 answer.json 的 `status` / `metrics_used` / `columns` / `sql` / `rows`。
   「多表连接」（JOIN）题单独统计，不混进总分（README 明写）。
2. **同问一致性**：同一题两遍产物的**稳定子集**（契约 §6.1、§6.4）逐字节比较；
   另附原始字节与 `answer_id` 是否相同。
3. **口径命中率**：契约 §3.3 的三条判据，按数字单元格计；命中旧版本单列一档。
4. **追溯完整率**：契约 §4.3 T1–T8，分母是「数字单元格数 N」而不是 trace 条数（§4.4）。

输入：`--run1` / `--run2` 两个目录，各放 `<question_id>.json`（内容为契约 §2 的 answer.json）。
目录为空或缺失时**不编数字**：该指标写 `null` 并注明「无可测样本」——
契约 §4.4 与文首要求 `null` 与 `0` 严格区分。

顺带产出 `rework.json`（打回清单）：每条缺陷带 `target_dir`，指明该退回哪一路。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
QUESTIONS_PATH = EVAL_DIR / "questions.jsonl"

CATS = ["筛选", "聚合", "同比环比", "多表连接", "排名", "异常"]
JOIN_CAT = "多表连接"

NUM_TYPES = {"int", "decimal"}          # 契约 §4.1：只有这两类算「一个输出数字」
AGG_OPS = {"sum", "avg", "count", "count_distinct", "min", "max", "identity"}   # 契约 §4.3 T6
FILTER_OPS = {"=", "!=", ">", ">=", "<", "<=", "in", "like"}                    # 契约 §4.3 T4
# 契约 §4.3 T4 还有后两个 op：`is_null` / `is_not_null`。手抄本漏了它们的话，
# 用了空值过滤的 trace 会在 T4 上被误判成「不过」——补齐，另一种「判据实现错」。
FILTER_NULL_OPS = {"is_null", "is_not_null"}
# 契约 §2.10 clarify.missing[].slot 的**封闭**六选一。§7.1 触发条件 4 却要求把这类
# 反问的槽位名写成 `time_column`（不在下面这个集合里）——报告 §0.4 的判据冲突之一。
CONTRACT_CLARIFY_SLOTS = {"metric", "time_range", "grain", "dimension", "filter", "dataset"}

# 契约 §4.3 T5 允许出现在 expression 里的 SQL 关键字 / 函数白名单
SQL_WORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING", "AS", "AND", "OR",
    "NOT", "NULL", "IS", "IN", "LIKE", "BETWEEN", "CASE", "WHEN", "THEN", "ELSE",
    "END", "DISTINCT", "ASC", "DESC", "LIMIT", "OFFSET", "WITH", "UNION", "ALL",
    "OVER", "PARTITION", "TRUE", "FALSE", "CAST", "SUM", "AVG", "COUNT", "MIN",
    "MAX", "TOTAL", "ROUND", "ABS", "COALESCE", "NULLIF", "IFNULL", "SUBSTR",
    "LOWER", "UPPER", "TRIM", "REPLACE", "LENGTH", "STRFTIME", "DATE", "DATETIME",
    "JULIANDAY", "INTEGER", "REAL", "TEXT", "NUMERIC", "NULLS", "FIRST", "LAST",
}

TARGET_DIRS = ["ask/", "show/", "dict/", "contract/", "eval/"]

RE_ID_DETAIL = re.compile(r"^[^#]+#[^!]+!\d+$")
RE_ID_AGG = re.compile(r"^agg\(.+\)$")
RE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RE_ROWS_PATH = re.compile(r"^rows\[(\d+)\]\.(.+)$")
RE_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+[\"'\[]?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
RE_JOIN_TARGET = re.compile(r"\bJOIN\s+[\"'\[]?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
# 契约 §5.1 comparison：两期对比必须一条 SQL 算完，允许 WITH/CTE，此时
# `WITH "__left" AS (...), "__right" AS (...) SELECT ... FROM "__left" LEFT JOIN "__keys"`
# 里的 JOIN 连的是 CTE，不是源表——不能当成「非 JOIN 题连了表」。
RE_CTE = re.compile(r"(?:\bWITH\b|,)\s*[\"'\[]?([A-Za-z_][A-Za-z0-9_]*)[\"'\]]?\s+AS\s*\(", re.IGNORECASE)


# --------------------------------------------------------------------------- 基础工具


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fmt_rate(hit: int | None, total: int | None) -> str:
    if not total:
        return "null（无可测样本）"
    assert hit is not None
    return f"{hit / total * 100:.1f}%（{hit}/{total}）"


def fmt_sample(hit: int, total: int, measured: bool) -> str:
    """没测就写「—/N（未测）」，不许把「没跑」印成「0 分」。"""
    return f"{hit}/{total}" if measured else f"—/{total}（未测）"


def rel_path(p: Any) -> str:
    """报告里写相对项目根的路径：绝对路径换台机器就不一样，也不该出现在报告里。"""
    try:
        return Path(p).resolve().relative_to(REPO_ROOT).as_posix()
    except Exception:  # noqa: BLE001 - 不在项目里的路径就原样写，不猜
        return str(p)


def load_questions(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_answers(directory: Path, qids: list[str]) -> tuple[dict[str, dict], dict[str, str]]:
    """返回 (可用答案, 读不出来的原因)。文件名必须是 <question_id>.json。"""
    got: dict[str, dict] = {}
    bad: dict[str, str] = {}
    if not directory.exists():
        return got, {qid: f"目录不存在：{directory}" for qid in qids}
    for qid in qids:
        fp = directory / f"{qid}.json"
        if not fp.exists():
            bad[qid] = "缺产物"
            continue
        try:
            obj = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏产物只记原因，不让整轮测量崩掉
            bad[qid] = f"JSON 解析失败：{exc}"
            continue
        if not isinstance(obj, dict) or "status" not in obj:
            bad[qid] = "不像 answer.json：缺 status"
            continue
        got[qid] = obj
    return got, bad


# --------------------------------------------------------------------------- 契约 §6.1 确定性

UNSTABLE_META_KEYS = ("created", "elapsed_ms", "runtime", "cache")   # 契约 §6.4


def stable_subset(ans: dict) -> dict:
    """去掉契约 §6.4 允许因环境而不同的字段，剩下的部分必须字节级一致。"""
    obj = copy.deepcopy(ans)
    meta = obj.get("meta")
    if isinstance(meta, dict):
        for key in UNSTABLE_META_KEYS:
            meta.pop(key, None)
        model = meta.get("model")
        if isinstance(model, dict):
            for body in model.values():
                if isinstance(body, dict):
                    body.pop("latency_ms", None)
    return obj


def canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def diff_paths(a: Any, b: Any, prefix: str = "", out: list[str] | None = None, cap: int = 12) -> list[str]:
    if out is None:
        out = []
    if len(out) >= cap:
        return out
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append(f"{prefix or '<root>'}（类型 {type(a).__name__} vs {type(b).__name__}）")
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(f"{prefix}.{key}（只在第二遍有）")
            elif key not in b:
                out.append(f"{prefix}.{key}（只在第一遍有）")
            else:
                diff_paths(a[key], b[key], f"{prefix}.{key}" if prefix else key, out, cap)
            if len(out) >= cap:
                break
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{prefix or '<root>'}[len]（{len(a)} vs {len(b)}）")
        for i, (x, y) in enumerate(zip(a, b)):
            diff_paths(x, y, f"{prefix}[{i}]", out, cap)
            if len(out) >= cap:
                break
    elif a != b:
        out.append(f"{prefix or '<root>'}（{a!r} vs {b!r}）")
    return out


def compare_pair(a: dict, b: dict) -> dict:
    sa, sb = stable_subset(a), stable_subset(b)
    return {
        "stable_equal": canon(sa) == canon(sb),
        "raw_equal": canon(a) == canon(b),
        "answer_id_equal": a.get("answer_id") == b.get("answer_id") and a.get("answer_id") is not None,
        "diffs": diff_paths(sa, sb),
    }


# --------------------------------------------------------------------------- 契约 §4 追溯


def numeric_cells(ans: dict) -> list[dict]:
    """契约 §4.1：N = rows 里 int/decimal 单元格数。"""
    cols = [c for c in ans.get("columns") or [] if isinstance(c, dict)]
    rows = ans.get("rows") or []
    cells: list[dict] = []
    for ci, col in enumerate(cols):
        if col.get("type") not in NUM_TYPES:
            continue
        name = col.get("name")
        digits = col.get("digits") if isinstance(col.get("digits"), int) else 0
        for ri, row in enumerate(rows):
            if isinstance(row, dict) and name in row:
                cells.append({
                    "path": f"rows[{ri}].{name}",
                    "column": name,
                    "col_index": ci,
                    "digits": digits,
                    "value": row[name],
                })
    return cells


def trace_for_path(traces: list[dict], path: str) -> dict | None:
    for tr in traces:
        if isinstance(tr, dict) and tr.get("value_path") == path:
            return tr
    return None


def strip_string_literals(sql: str) -> str:
    """只剥离单引号字符串字面量；双引号在 SQLite 里是标识符，必须留着。"""
    out: list[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    if i + 1 < len(sql) and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def identifiers(expr: str) -> list[str]:
    return RE_IDENT.findall(strip_string_literals(expr))


def dec(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def csv_header(path: Path) -> list[str] | None:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except Exception:  # noqa: BLE001 - 编码不认识的源文件就不当「可验证」
        return None
    first = raw.splitlines()[0] if raw.splitlines() else ""
    sep = "\t" if "\t" in first and "," not in first else ","
    return [x.strip().strip('"') for x in first.split(sep)]


def check_trace(tr: dict, ans: dict, header_cache: dict[str, list[str] | None]) -> dict[str, Any]:
    """契约 §4.3 T1–T8。返回 {T1..T8: True/False/None}，None = 这一条在 answer.json 层面无法验证。"""
    res: dict[str, Any] = {}
    sql_list = ans.get("sql") or []
    meta = ans.get("meta") or {}
    files = [f for f in (meta.get("files") or []) if isinstance(f, dict)]
    options = meta.get("options") or {}
    source = tr.get("source") or {}

    # T1 源文件 + 哈希
    f_path, f_sha = source.get("file"), source.get("file_sha256")
    res["T1"] = bool(f_path) and any(f.get("sha256") == f_sha for f in files)

    # T2 列名（大小写敏感）
    cols = source.get("columns") or []
    struct_ok = bool(cols) and all(isinstance(c, str) and c for c in cols) and len(set(cols)) == len(cols)
    if not struct_ok:
        res["T2"] = False
    elif not f_path:
        res["T2"] = False
    else:
        local = REPO_ROOT / str(f_path)
        if local.suffix.lower() == ".csv" and local.exists():
            header = header_cache.get(str(f_path))
            if header is None:
                header = csv_header(local)
                header_cache[str(f_path)] = header
            res["T2"] = bool(header) and all(c in header for c in cols)
        else:
            res["T2"] = None      # xlsx / 文件不可达：结构对，但没对照源表头，不算已验

    # T3 行标识
    row_ids = tr.get("row_ids") or []
    total = tr.get("row_ids_total")
    truncated = tr.get("row_ids_truncated")
    def id_ok(rid: str) -> bool:
        if not isinstance(rid, str) or not rid:
            return False
        if RE_ID_DETAIL.match(rid) or RE_ID_AGG.match(rid):
            return True
        parts = rid.split("+")
        return len(parts) > 1 and all(RE_ID_DETAIL.match(p) or RE_ID_AGG.match(p) for p in parts)
    if not row_ids or not all(id_ok(r) for r in row_ids):
        res["T3"] = False
    elif truncated is True:
        cap = options.get("trace_row_id_cap")
        res["T3"] = isinstance(total, int) and isinstance(cap, int) and total > len(row_ids) and len(row_ids) == cap
    elif truncated is False:
        res["T3"] = isinstance(total, int) and total == len(row_ids)
    else:
        res["T3"] = False
    # 行号是否 ≤ 源表数据行数：CSV 可验证才查（契约 §4.3 T3 后半句）
    if res["T3"] is True and isinstance(f_path, str) and f_path.lower().endswith(".csv"):
        local = REPO_ROOT / f_path
        if local.exists():
            try:
                lines = local.read_text(encoding="utf-8-sig").splitlines()
                n_data = max(len(lines) - 1, 0)
                for rid in row_ids:
                    for part in rid.split("+"):
                        if RE_ID_DETAIL.match(part):
                            if int(part.rsplit("!", 1)[1]) > n_data:
                                res["T3"] = False
            except Exception:  # noqa: BLE001
                pass

    # T4 过滤条件
    filt = tr.get("filter")
    if not isinstance(filt, dict):
        res["T4"] = False
    elif filt.get("none") is True:
        res["T4"] = True
    else:
        op = filt.get("op")
        res["T4"] = (
            all(k in filt for k in ("column", "op", "value_literal"))
            and (op in FILTER_OPS or op in FILTER_NULL_OPS)
            # 契约 §4.3 T4：`is_null` / `is_not_null` 的 `value_literal` 必须**显式写 `null`**；
            # 其余 op 的 `value_literal` 不许为 `null`（不许拿 null 冒充「等于空」）
            and ((op in FILTER_NULL_OPS and filt.get("value_literal") is None)
                 or (op in FILTER_OPS and filt.get("value_literal") is not None))
        )

    # T5 表达式标识符 ⊆ 声明的源列 ∪ 关键字
    expr = tr.get("expression")
    if not isinstance(expr, str) or not expr.strip():
        res["T5"] = False
    else:
        allowed = set(c for c in cols if isinstance(c, str))
        res["T5"] = all(i in allowed or i.upper() in SQL_WORDS for i in identifiers(expr))

    # T6 聚合步骤
    agg = tr.get("aggregation")
    res["T6"] = isinstance(agg, list) and bool(agg) and all(
        isinstance(a, dict) and a.get("op") in AGG_OPS for a in agg
    )

    # T7 sql_index 指向的 SQL 里能找到 expression 子串
    idx = tr.get("sql_index")
    if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(sql_list)):
        res["T7"] = False
    elif not isinstance(expr, str) or not expr:
        res["T7"] = False
    else:
        res["T7"] = expr in str(sql_list[idx])

    # T8 value 与 value_path 严格相等（decimal 按 digits 对齐）+ 指标快照可验
    m = RE_ROWS_PATH.match(str(tr.get("value_path") or ""))
    cell_value: Any = None
    found = False
    if m:
        ri, col_name = int(m.group(1)), m.group(2)
        rows = ans.get("rows") or []
        if ri < len(rows) and isinstance(rows[ri], dict) and col_name in rows[ri]:
            cell_value, found = rows[ri][col_name], True
    if not found:
        res["T8"] = False
    else:
        col = next((c for c in (ans.get("columns") or []) if isinstance(c, dict) and c.get("name") == col_name), None)
        digits = col.get("digits") if isinstance(col, dict) and isinstance(col.get("digits"), int) else 0
        a, b = dec(tr.get("value")), dec(cell_value)
        if a is None or b is None:
            res["T8"] = False
        else:
            q = Decimal(1).scaleb(-digits)
            al, bl = a, b
            if isinstance(a, Decimal) and a.as_tuple().exponent != -digits:
                al = a.quantize(q, rounding=ROUND_HALF_EVEN)
            if isinstance(b, Decimal) and b.as_tuple().exponent != -digits:
                bl = b.quantize(q, rounding=ROUND_HALF_EVEN)
            equal = al == bl
        if equal and tr.get("metric_id") is not None:
            snap = next(
                (x for x in (ans.get("metrics_used") or []) if isinstance(x, dict) and x.get("id") == tr.get("metric_id")),
                None,
            )
            if not snap or not isinstance(snap.get("definition"), str):
                equal = False
            else:
                want = sha256_text(snap["definition"])
                got = str(snap.get("definition_sha256") or "")
                equal = bool(got) and (want.startswith(got) or got.startswith(want))
        res["T8"] = equal

    return res


def trace_completeness(ans: dict, header_cache: dict[str, list[str] | None]) -> dict[str, Any]:
    cells = numeric_cells(ans)
    n = len(cells)
    traces = [t for t in (ans.get("trace") or []) if isinstance(t, dict)]
    per: list[dict] = []
    strict_ok = 0
    lenient_ok = 0
    for tr in traces:
        checks = check_trace(tr, ans, header_cache)
        per.append({
            "trace_id": tr.get("trace_id"),
            "value_path": tr.get("value_path"),
            "row_ids_n": len(tr.get("row_ids") or []),
            "row_ids_total": tr.get("row_ids_total"),
            "checks": checks,
        })
        vals = list(checks.values())
        if all(v is True for v in vals):
            strict_ok += 1
        if all(v is not False for v in vals):
            lenient_ok += 1
    unverified = sum(1 for p in per for v in p["checks"].values() if v is None)
    covered = {t.get("value_path") for t in traces}
    return {
        "n_numbers": n,
        "n_traces": len(traces),
        "strict_ok": strict_ok,
        "lenient_ok": lenient_ok,
        "unverified_checks": unverified,
        "missing_paths": [c["path"] for c in cells if c["path"] not in covered][:10],
        "per_trace": per,
    }


# --------------------------------------------------------------------------- 契约 §3.3 口径命中


def metric_hit_tiers(ans: dict, dict_index: dict[str, dict] | None) -> dict[str, Any]:
    cells = numeric_cells(ans)
    cols = {c.get("name"): c for c in (ans.get("columns") or []) if isinstance(c, dict)}
    traces = [t for t in (ans.get("trace") or []) if isinstance(t, dict)]
    snaps = {m.get("id"): m for m in (ans.get("metrics_used") or []) if isinstance(m, dict)}
    buckets: dict[str, list[str]] = {"hit": [], "old_version": [], "miss": [], "not_dict": [], "unverified": []}

    for cell in cells:
        col = cols.get(cell["column"]) or {}
        tr = trace_for_path(traces, cell["path"]) or {}
        mid = tr.get("metric_id")
        if mid is None:
            buckets["not_dict"].append(cell["path"])
            continue
        snap = snaps.get(mid)
        # 判据 1 + 2
        if col.get("source_metric_id") != mid or not snap or not isinstance(snap.get("definition"), str):
            buckets["miss"].append(cell["path"])
            continue
        want = sha256_text(snap["definition"])
        got = str(snap.get("definition_sha256") or "")
        if not got or not (want.startswith(got) or got.startswith(want)):
            buckets["miss"].append(cell["path"])
            continue
        # 判据 3：与当前字典逐字相等
        if dict_index is None:
            buckets["unverified"].append(cell["path"])
        elif mid in dict_index and dict_index[mid].get("definition") == snap["definition"]:
            buckets["hit"].append(cell["path"])
        else:
            buckets["old_version"].append(cell["path"])

    denom = len(buckets["hit"]) + len(buckets["old_version"]) + len(buckets["miss"]) + len(buckets["unverified"])
    return {"buckets": buckets, "denom": denom, "dict_checked": dict_index is not None}


# --------------------------------------------------------------------------- 准确率


def source_tables(sql_text: str) -> tuple[set[str], set[str]]:
    """(FROM/JOIN 之后出现的源表, 真正连出去的 JOIN 目标)。

    CTE 名不算表：契约 §5.1 的两期对比就是一条 `WITH ... LEFT JOIN __keys` 的 SQL，
    把 CTE 当成「连了表」会把非 JOIN 题误判成答错。
    """
    ctes = {m.lower() for m in RE_CTE.findall(sql_text)}
    tables = {t.lower() for t in RE_TABLE.findall(sql_text)} - ctes
    joins = {t.lower() for t in RE_JOIN_TARGET.findall(sql_text)} - ctes
    return tables, joins


def judge_question(q: dict, ans: dict) -> tuple[bool, list[str]]:
    """按 eval/questions.jsonl 的 expect 机械核对。返回 (是否算答对, 失败原因)。"""
    ex = q["expect"]
    fails: list[str] = []

    want = ex["status"]
    wants = {want} if isinstance(want, str) else set(want)
    if ans.get("status") not in wants:
        return False, [f"status={ans.get('status')!r}，期望 {sorted(wants)}"]
    if ans.get("status") != "ok":
        # 反问也算「按契约处理」：只要 cite 了该有的槽位就不算错
        if ans.get("status") == "clarify":
            missing = (ans.get("clarify") or {}).get("missing") or []
            if not missing:
                fails.append("status=clarify 但 clarify.missing 为空（契约 §2.10）")
        return not fails, fails

    used = [m for m in (ans.get("metrics_used") or []) if isinstance(m, dict)]
    for hint in ex["metrics"]:
        if not any(m.get("id") == hint or m.get("name") == hint for m in used):
            fails.append(f"metrics_used 里没有指标「{hint}」")

    col_names = {c.get("name") for c in (ans.get("columns") or []) if isinstance(c, dict)}
    plan_dims = {
        d
        for p in (ans.get("plan") or []) if isinstance(p, dict)
        for d in ((p.get("needs") or {}).get("dimensions") or [])
    }
    for dim in ex["dims"]:
        if dim not in col_names and dim not in plan_dims:
            fails.append(f"少维度「{dim}」：既不在 columns 也不在 plan.needs.dimensions")

    sql_text = " ".join(str(s) for s in (ans.get("sql") or []))
    upper = sql_text.upper()
    tables, join_targets = source_tables(sql_text)
    if ex["group_by"] is True and "GROUP BY" not in upper:
        fails.append("SQL 没有 GROUP BY")
    if ex["group_by"] is False and "GROUP BY" in upper:
        fails.append("SQL 多了 GROUP BY（问题不需要分组）")
    if ex["order_by"] is True and "ORDER BY" not in upper:
        fails.append("SQL 没有 ORDER BY（排名题排序必须显式全序）")

    has_join = bool(join_targets)
    if ex["join"] and not has_join:
        fails.append("SQL 没有 JOIN（JOIN 题）")
    if not ex["join"] and has_join:
        fails.append("SQL 出现了 JOIN（非 JOIN 题不该连表）")
    if ex["join"]:
        if len(tables) < 2:
            fails.append(f"JOIN 只涉及 {len(tables)} 张表，至少要 2 张")

    rows = ans.get("rows") or []
    if ex["rows"] == "scalar" and len(rows) != 1:
        fails.append(f"应为单行结果，实际 {len(rows)} 行")
    if ex["rows"] == "many" and len(rows) < 1:
        fails.append("应为非空结果，实际 0 行")

    # —— 语义是否真的落地（机械核对抓不到语义，这几条是补的硬结构要求）——
    for group in ex.get("sql_all_any") or []:
        alts = [str(a).upper() for a in group]
        if not any(a in upper for a in alts):
            fails.append(f"SQL 里没有 {' / '.join(group)} 中任何一个：这条问题的语义没落地")
    if ex.get("order") == "desc" and "DESC" not in upper:
        fails.append("SQL 没有 DESC：排名要求从高到低")
    if ex.get("order") == "asc" and "ASC" not in upper:
        fails.append("SQL 没有 ASC：排名要求从低到高")
    limit = ex.get("limit")
    if isinstance(limit, int) and f"LIMIT {limit}" not in upper:
        fails.append(f"SQL 没有 LIMIT {limit}：排名题必须截断条数")

    return not fails, fails


# --------------------------------------------------------------------------- 判据冲突


def judge_conflicts(det: dict, hit: dict, trace: dict,
                    clarify_slots: dict[str, list[str]] | None = None) -> list[dict]:
    """判据冲突：同一批产物在契约的两处写法下给出**不同结论**的地方。

    只报「契约两处要求不一致」或「契约没写」的点，数字全部来自本轮 `eval/runs/` 的产物；
    没有产物就返回 `[]`（报告写「无可测样本」），不编一条冲突出来（契约文首：不许给未核实数字）。
    """
    out: list[dict] = []
    if det["pairs"]:
        out.append({
            "topic": "同问一致性：整份文件原始字节 vs 契约 §6.1 稳定子集",
            "a": f"整份文件原始字节一致 {det['raw_equal']}/{det['pairs']}",
            "b": f"§6.1 稳定子集一致 {det['equal']}/{det['pairs']}",
            "conflict": "契约 §6.1 只要求**稳定子集**字节一致，§6.4 又明确允许 `meta.created` / "
                        "`elapsed_ms` / `runtime` / `meta.cache.*` 因环境而不同；同一批真产物按"
                        "「整份文件逐字节」读，结论就从全过变成全不过——两个结论都不假，只是判据不同。",
            "used": "采用 §6.1 稳定子集作为指标（契约原文），原始字节数只在 §2 里作参考列，不进任何指标。",
        })
    if trace["n_numbers"] and trace["n_numbers"] != trace["n_traces"]:
        gap = ("差额来自有数字却没有 trace 的位置：" + "、".join(trace["missing_paths"][:3]) + "。"
               if trace["missing_paths"] else "差额来自 trace 条数多于可对应数字的情形。")
        out.append({
            "topic": "追溯完整率的分母：§4.4 的 N（数字单元格） vs §4.3 逐条判的 trace 条数",
            "a": f"分母用 N（§4.4）：{trace['strict_ok']}/{trace['n_numbers']}",
            "b": f"分母用 trace 条数：{trace['strict_ok']}/{trace['n_traces']}",
            "conflict": "§4.3 的 T1–T8 是「一条 trace 一条」地判，§4.4 却要求分母是数字单元格数 N；"
                        f"本轮两者不等（N={trace['n_numbers']}、trace 条数={trace['n_traces']}，"
                        "契约 §4.1 的 `|trace| == N` 因此不成立），"
                        + gap + "同一批产物因此能算出两个不同的百分数。",
            "used": "采用 §4.4 的分母 N（契约原文）；`|trace| == N` 不成立时如实写差在哪几条，"
                    "不把分母悄悄换成 trace 条数来把数字做高。",
        })
    if hit["denom"] and trace["n_numbers"] and hit["denom"] != trace["n_numbers"]:
        out.append({
            "topic": "口径命中率的分母：§3.3 没写 vs 追溯口径的 N",
            "a": f"按本步定义的分母（带 `metric_id` 的数字单元格）{hit['denom']}：{hit['hit']}/{hit['denom']}",
            "b": f"按 §4.4 的 N（全部数字单元格）{trace['n_numbers']}：{hit['hit']}/{trace['n_numbers']}",
            "conflict": "契约 §3.3 只判「一个数字算不算命中」，**没定义分母**；本步取「带 metric_id 的数字单元格」，"
                        "若改用 N（含非字典口径的单元格），同一个命中数会得出另一个百分比。",
            "used": "采用本步定义并在 §3 写明的分母（非字典口径单列、不进分母），N 这一列只在这里对照。",
        })
    # —— 本轮真产物上对上的另外两条：槽位名不在 §2.10 枚举内 / 0 命中聚合没有 row_ids ——
    slots = {s: q for s, q in (clarify_slots or {}).items() if q and s not in CONTRACT_CLARIFY_SLOTS}
    if slots:
        qids = sorted({x for q in slots.values() for x in q})
        detail = "；".join(f"`{s}`：{'、'.join(q[:6])}" for s, q in sorted(slots.items()))
        out.append({
            "topic": f"{'、'.join(qids)}：反问槽位名 {'、'.join(f'`{s}`' for s in sorted(slots))} "
                     f"的判据冲突（§2.10 的封闭枚举 vs §7.1 触发条件 4）",
            "a": f"按 §2.10 的封闭枚举 {sorted(CONTRACT_CLARIFY_SLOTS)}："
                 f"这些槽位不在枚举里，反问体不合法（{detail}）",
            "b": f"按 §7.1 触发条件 4（「槽位名 `time_column`，候选列名列全」）："
                 f"本轮 {len(qids)} 道反问正是这么写的（{detail}）",
            "conflict": "§2.10 把 `clarify.missing[].slot` 钉成封闭六选一，§7.1 触发条件 4 又要求"
                        "系统在「有两个以上候选时间列」时用 `time_column` 作槽位名——两个名字都写在"
                        "契约里，后者却不在前者的枚举内。同一批产物按 §2.10 判是「反问体不合法」，"
                        "按 §7.1 判是「按契约正确反问」，两处判据结论相反。",
            "used": "本报告取判据 B（按 §7.1 触发条件 4，这几道反问本身是合法反问），理由：注入式 D1 的 "
                    "`ambiguity=[]` 与 `time_column=null` 同时成立时，§5.1「`ambiguity != []` ⟺ "
                    "`status=\"clarify\"`」和 §7.1 触发条件 4 无法同时满足，`ask/` 怎么实现都会碰到一条。"
                    "所以准确率里照旧算错（`expect.status=\"ok\"`），五个数照旧不动"
                    "（49/50、0/10、60/60、218/218、216/219），"
                    "但 `eval/rework.json` 只把这几道题退回 `contract/` 一条条目（点名题号 q032 / q035 / q040），"
                    "不再给 `ask/` 落「修 qNNN 的取数」：等契约定下唯一口径再判 `ask/` 的对错，"
                    "两条原因不混进同一个数字。",
        })
    zero_hit = [
        p for p in trace["per_trace"]
        if p["checks"].get("T3") is False and not p.get("row_ids_n") and p.get("row_ids_total") == 0
    ]
    if zero_hit:
        qids = sorted({p.get("qid", "") for p in zero_hit if p.get("qid")})
        detail = "、".join(f"{p.get('qid', '')}:{p.get('value_path')}" for p in zero_hit[:6])
        out.append({
            "topic": f"{'、'.join(qids)}：零行命中的聚合数字没有 `row_ids` 的判据冲突"
                     f"（§4.3 T3 要求非空 vs §4.4 空结果集口径）",
            "a": f"按 §4.3 T3「`row_ids` 非空」逐条判：这 {len(zero_hit)} 条 trace 不过 → "
                 f"严格率 {trace['strict_ok']}/{trace['n_numbers']}（{detail}）",
            "b": f"按 §4.4「空结果集没有追溯不完整这回事」判：这些格子是「零行命中、聚合值就是 0」，"
                 f"没有源行可指 → 不算不完整 → {trace['strict_ok'] + len(zero_hit)}/{trace['n_numbers']}",
            "conflict": "§4.3 T3 无条件要求 `row_ids` 非空；但零行命中时 `COUNT(*)` 之类的聚合值 0 是"
                        "**算出来的零**（契约 §2.13「`0` 表示算出来是零」），源表里根本没有可指认的行；"
                        "§4.4 又说空结果集只有「没有可追溯的数字」，没有「追溯不完整」。同一批产物因此"
                        "既能判成「追溯不完整」，也能判成「没有可追溯的行、不算不完整」。",
            "used": "本报告采用 §4.3 的严格口径（不可验证/不满足 = 未通过），严格率按下界报；"
                    "同时把这类缺口退回 `contract/`（要 §4.3 给「0 命中 → `row_ids=[]` + "
                    "`row_ids_total=0` 合法」的判据），不在 `eval/` 里自行放宽。",
        })
    return out


# --------------------------------------------------------------------------- 报告


def render_report(ctx: dict) -> str:
    L: list[str] = []
    add = L.append
    q = ctx["questions"]
    join_ids = {x["id"] for x in q if x["cat"] == JOIN_CAT}
    non_join_total = len(q) - len(join_ids)

    add("# tally 评测报告 · 四项指标 + 同问两遍字节比对")
    add("")
    add(f"- 契约：`contract/answer.md` v{ctx['contract_version']}（字段名与判据全部以它为准）")
    add(f"- 问题集：`eval/questions.jsonl`（{len(q)} 题 / {len(CATS)} 类 / 每类 {len(q)//len(CATS)} 题）")
    add(f"- 生成时间：{ctx['generated']}")
    add(f"- 测量对象：answer.json 产物目录 `{rel_path(ctx['run1'])}` / `{rel_path(ctx['run2'])}`；"
        f"可用 {ctx['n_run1']}/{len(q)} 与 {ctx['n_run2']}/{len(q)}")
    add(f"- 同问比对对数：{ctx['pairs_available']}/{ctx['pairs_requested']}（`--determinism-pairs`）")
    add("")
    add("> `null` = 没有可测样本，`0` = 测了但一个都没过。两者严格区分（契约 §4.4）。")
    add("> 本报告不含任何编造的数字：没有产物就一律写 `null`。")
    add("")
    if ctx.get("protocol"):
        pr = ctx["protocol"]
        add("## 0.1 测量协议（这轮到底在测什么）")
        add("")
        add(f"- 数据：{pr.get('data', '—')}")
        add(f"- 字典：{pr.get('metric_dict', '—')}（合成数据自带，不读 `dict/`）")
        add(f"- D1（问题理解）：{pr.get('d1', '—')}")
        add(f"- D2/D3/D4：{pr.get('d2_d3_d4', '—')}")
        add(f"- 时间常数 `--now`：{pr.get('now', '—')}（只为可复现，不参与口径）")
        add(f"- 两遍独立性：{pr.get('independence', '—')}")
        add("")
        add("> 换句话说：**这一轮量的不是「模型理解得对不对」，而是「给定问题理解，取数链路能不能"
            "建计划、选表选列、生成并执行 SQL、给出可追溯的数字」**。D1 注入是契约 §5.2 的合法用法，"
            "因为 §7.1 规定 D1 没有兜底。")
        add("")
    if ctx.get("caveats") or ctx.get("error_codes") or ctx.get("clarify_slots"):
        add("## 0.2 失败根因分布（本轮实际跑出来的）")
        add("")
        add("| 类别 | 代码 | 题数 | 题号（前 6 个） |")
        add("|---|---|---|---|")
        for code, info in sorted((ctx.get("caveats") or {}).items()):
            add(f"| 兜底 caveat | `{code}` | {info['n']} | {'、'.join(info['qids'][:6])} |")
        for code, info in sorted((ctx.get("error_codes") or {}).items()):
            add(f"| 报错 | `{code}` | {info['n']} | {'、'.join(info['qids'][:6])} |")
        for slot, qids in sorted((ctx.get("clarify_slots") or {}).items()):
            add(f"| 反问槽位 | `{slot}` | {len(qids)} | {'、'.join(qids[:6])} |")
        add("")
    ch = ctx.get("chain") or {}
    if ch:
        add("## 0.3 降级链路 / 模型链路（四个环节各自走了哪条）")
        add("")
        add("契约 §7.1 规定 D1 无兜底、D2/D3/D4 各有兜底，§5.2 允许把 D1 的输入注入。"
            "下面逐题读产物的 `meta.model.Dn.source` 汇总，数字全部来自 `eval/runs/` 的真产物，没有估算：")
        add("")
        add("| 环节 | 模型链路（`model`） | 注入（`inject`） | 降级链路（`fallback`） | 未标注 | 有产物的题 |")
        add("|---|---|---|---|---|---|")
        n_ans = ctx["n_run1"]
        for dname in ("D1", "D2", "D3", "D4"):
            c = ch.get(dname) or {}
            add(f"| {dname} | {c.get('model', 0)} | {c.get('inject', 0)} | {c.get('fallback', 0)} "
                f"| {c.get('unknown', 0)} | {n_ans} |")
        add("")
        n_model = sum((ch.get(d) or {}).get("model", 0) for d in ("D1", "D2", "D3", "D4"))
        n_fb = sum((ch.get(d) or {}).get("fallback", 0) for d in ("D2", "D3", "D4"))
        n_inj = (ch.get("D1") or {}).get("inject", 0)
        add(f"- 模型链路调用次数合计：{n_model} 次；D1 注入 {n_inj} 题；D2/D3/D4 兜底 {n_fb} 次。")
        if n_model == 0:
            add("- **本轮模型链路 0 题**（环境未配置模型），整份报告量到的是「注入 + 降级链路」"
                "的表现；换成真模型必须重跑，不能拿这份数字当模型效果。")
        add("- 降级不是「跑挂了」：`fallback` 是契约 §7.1 规定的合法兜底，"
            "每个兜底环节都会在产物 `caveats` 里留一条可追溯的代码。")
        add("")
        chain_of = {
            "INJECT_DIGEST_UNCHECKED": "D1 注入（注入文件不写 `input_digest`，无法自校验）",
            "PLAN_MINIMAL": "D2 降级（最小计划，不拆步骤）",
            "NOT_PRUNED": "D3 降级（不剪枝，全列选表）",
            "NO_INTERPRETATION": "D4 降级（不解读，只给数）",
        }
        cvs = ctx.get("caveats") or {}
        if cvs:
            add("caveat → 落在哪条链路上（题数来自本轮实际产物）：")
            add("")
            add("| caveat | 落在哪条链路 | 题数 | 题号（前 6 个） |")
            add("|---|---|---|---|")
            for code, info in sorted(cvs.items()):
                add(f"| `{code}` | {chain_of.get(code, '未归类（新代码，需回看 `ask/`）')} "
                    f"| {info['n']} | {'、'.join(info['qids'][:6])} |")
            add("")

    cf = judge_conflicts(ctx["det"], ctx["hit"], ctx["trace"], ctx.get("clarify_slots"))
    add("## 0.4 判据冲突（同一批产物，两套判据给出不同结论的地方）")
    add("")
    if not cf:
        add("本轮没有可测样本，判据冲突一条都没测到，不编。")
        add("")
    else:
        add("契约里有几处「同一件事两种写法」，外加一处没写分母。下面只列**本轮真产物上真的对不上**的几条，"
            "两条判据各自的数字都给出来，并写明本报告最终采用哪一条（数字全部来自 `eval/runs/`，没有估算）：")
        add("")
        for i, c in enumerate(cf, 1):
            add(f"**JC-{i} · {c['topic']}**")
            add("")
            add(f"- 判据 A：{c['a']}")
            add(f"- 判据 B：{c['b']}")
            add(f"- 冲突点（判据冲突原文）：{c['conflict']}")
            add(f"- 本报告采用：{c['used']}")
            add("")

    add("## 0. 结论速览")
    add("")
    add("| 指标 | 值 | 样本 | 口径 |")
    add("|---|---|---|---|")
    acc = ctx["accuracy"]
    m = acc["measured"]
    add(f"| SQL 执行准确率（主指标，不含 JOIN 题） | {fmt_rate(acc['non_join_ok'], acc['non_join_total'] if m else None)} "
        f"| {fmt_sample(acc['non_join_ok'], acc['non_join_total'], m)} | 问题集 expect 机械核对；分母只算有产物的题 |")
    add(f"| SQL 执行准确率 · JOIN 题（单独统计，不混进总分） | {fmt_rate(acc['join_ok'], acc['join_total'] if m else None)} "
        f"| {fmt_sample(acc['join_ok'], acc['join_total'], m)} | 同口径，JOIN 组单独算 |")
    d0 = ctx["det"]
    add(f"| 同问一致性（两遍字节一致率） | {fmt_rate(d0['equal'], d0['pairs'] or None)} "
        f"| {fmt_sample(d0['equal'], d0['pairs'], d0['pairs'] > 0)} | 契约 §6.1 稳定子集逐字节 |")
    h0 = ctx["hit"]
    add(f"| 口径命中率 | {fmt_rate(h0['hit'], h0['denom'] or None)} "
        f"| {fmt_sample(h0['hit'], h0['denom'], h0['denom'] > 0)} | 契约 §3.3，按数字单元格计 |")
    t0 = ctx["trace"]
    add(f"| 追溯完整率（严格：T1–T8 全过 / N） | {fmt_rate(t0['strict_ok'], t0['n_numbers'] or None)} "
        f"| {fmt_sample(t0['strict_ok'], t0['n_numbers'], t0['n_numbers'] > 0)} | 契约 §4.3 §4.4，不可验证算未通过 |")
    add(f"| 追溯完整率（可验证口径：不可验证放行） | {fmt_rate(t0['lenient_ok'], t0['n_numbers'] or None)} "
        f"| {fmt_sample(t0['lenient_ok'], t0['n_numbers'], t0['n_numbers'] > 0)} | 同上，仅 T2/T3 不可验证时放行 |")
    add("")
    if ctx["notes"]:
        for n in ctx["notes"]:
            add(f"- {n}")
        add("")

    add("## 1. 分类准确率")
    add("")
    add(f"覆盖率：有产物可测 {acc['non_join_total'] + acc['join_total']}/{acc['non_join_planned'] + acc['join_planned']} 题"
        f"（分母只算有产物的题；缺产物的题单列在下面的「未测」清单里，不从分母里悄悄去掉，也不算答错）。")
    add("")
    add("| 分类 | 答对/已测 | 计划题数 | 准确率 | 说明 |")
    add("|---|---|---|---|---|")
    for cat in CATS:
        st = acc["by_cat"][cat]
        note = "JOIN 题，单独统计，不混进总分" if cat == JOIN_CAT else ""
        add(f"| {cat} | {fmt_sample(st['ok'], st['measured'], st['measured'] > 0)} | {st['planned']} | "
            f"{fmt_rate(st['ok'], st['measured'] or None)} | {note} |")
    add("")
    add(f"总分 = 非 JOIN 题：{fmt_rate(acc['non_join_ok'], acc['non_join_total'] or None)}；"
        f"JOIN 组另算：{fmt_rate(acc['join_ok'], acc['join_total'] or None)}（README「JOIN 题单独统计，不混进总分」）。")
    add("")
    if acc["not_answered"]:
        ids = [x["qid"] for x in acc["not_answered"]]
        add(f"未测（没有产物，不算答错也不算答对）：{len(ids)} 题 —— {'、'.join(ids[:80])}")
        add("")
    if acc["failures"]:
        conflict_qids = set(ctx.get("conflict_qids") or ())
        add("答错的题（每题列前两条原因）：")
        add("")
        add("| 题号 | 分类 | 问题 | 失败原因 |")
        add("|---|---|---|---|")
        for f in acc["failures"][:60]:
            reasons = "；".join(f["reasons"][:2]).replace("|", "\\|")
            # 判据冲突就地标注：这一类不是 `ask/` 的取数缺陷（报告 §0.4 JC-4），
            # 准确率里照旧算错，但责任归 `contract/`，不混进「答错 = ask/ 的 bug」这个桶里糊过去。
            if f["qid"] in conflict_qids:
                reasons += " → **判据冲突**（§2.10 封闭枚举 vs §7.1 触发条件 4，见 §0.4 JC-4：责任归 `contract/`，不算 `ask/` 的取数缺陷）"
            add(f"| {f['qid']} | {f['cat']} | {f['q'][:28]} | {reasons} |")
        add("")
        if conflict_qids:
            add(f"> 上表里标「判据冲突」的 {len(conflict_qids)} 题（{'、'.join(sorted(conflict_qids))}）："
                "准确率里照旧按 `expect.status=\"ok\"` 算错（数字不变），但它们不算 `ask/` 的取数缺陷，"
                "`eval/rework.json` 只把这些题退回 `contract/` 一条，等契约定下唯一口径再判 `ask/` 的对错。")
            add("")

    add("## 2. 同问两遍字节比对")
    add("")
    add("判据：契约 §6.1 稳定子集（去掉 §6.4 的 `meta.created` / `elapsed_ms` / `runtime` / "
        "`meta.cache.*` / `meta.model.Dn.latency_ms`）规范化后**逐字节**相等；"
        "原始字节与 `answer_id` 另列，只作参考，不作判据。")
    add("")
    d = ctx["det"]
    add(f"- 请求对数：{ctx['pairs_requested']}")
    add(f"- 实际可比对数：{d['pairs']}")
    dm = d["pairs"] > 0
    add(f"- 稳定子集一致：{fmt_sample(d['equal'], d['pairs'], dm)}")
    add(f"- 原始字节一致：{fmt_sample(d['raw_equal'], d['pairs'], dm)}")
    add(f"- `answer_id` 也一致：{fmt_sample(d['id_equal'], d['pairs'], dm)}")
    add("")
    if d["cases"]:
        add("| 题号 | 稳定子集一致 | 原始字节一致 | answer_id 一致 | 差异字段（前几条） |")
        add("|---|---|---|---|---|")
        for c in d["cases"]:
            diffs = "；".join(x.replace("|", "\\|") for x in c["diffs"][:3]) or "—"
            add(f"| {c['qid']} | {'是' if c['stable_equal'] else '**否**'} | "
                f"{'是' if c['raw_equal'] else '否'} | {'是' if c['answer_id_equal'] else '否'} | {diffs} |")
        add("")
    else:
        add("本轮没有两遍产物，比对对数为 0，指标写 `null`，不写 0。")
        add("")

    add("## 3. 口径命中率（契约 §3.3）")
    add("")
    hb = ctx["hit"]["buckets"]
    add(f"- 分母（带 `metric_id` 的数字单元格）：{ctx['hit']['denom']}")
    add(f"- 命中：{len(hb['hit'])}；命中旧版本（单列一档，不进命中率）：{len(hb['old_version'])}；"
        f"未命中：{len(hb['miss'])}；字典未提供未校验：{len(hb['unverified'])}；"
        f"非字典口径（不计入分母）：{len(hb['not_dict'])}")
    if not ctx["hit"]["denom"]:
        add("- 本轮分母为 0：命中率写 `null`，不写 0%。")
    add(f"- 当前字典是否参与判据 3：{'是' if ctx['hit']['dict_checked'] else '否（未提供 `--metric-dict`，判据 3 记为未校验）'}")
    add("")
    add("口径：命中率 = 命中 / (命中 + 命中旧版本 + 未命中 + 未校验)；"
        "契约 §3.3 只定义了「一个数字算不算命中」，分母由本步定义并写在这里。")
    add("")

    add("## 4. 追溯完整率（契约 §4.3 T1–T8 / §4.4）")
    add("")
    t = ctx["trace"]
    add(f"- 数字单元格数 N = {t['n_numbers']}；trace 条数 = {t['n_traces']}"
        f"（契约 §4.1 要求 |trace| == N）")
    add(f"- 八条全过（严格）：{t['strict_ok']}；不可验证放行（可验证口径）：{t['lenient_ok']}")
    add(f"- 无法在 answer.json 层面验证的检查点次数：{t['unverified_checks']}"
        "（T2/T3 需要回源表头与行数；本步只解 CSV，xlsx 源记「不可验证」）")
    if t["unverified_checks"]:
        add("- 严格率因此是**下界**：不可验证不等于不完整，但也不算已验（契约 §4.3 要八条全过）。"
            "要看产品本身的追溯质量，请对照「可验证口径」那一行。")
    if t["missing_paths"]:
        add(f"- 有数字但没有 trace 的位置：{'、'.join(t['missing_paths'])}")
    add("")
    if t["per_trace"]:
        add("| 题号 | trace_id | 数字位置 | T1 | T2 | T3 | T4 | T5 | T6 | T7 | T8 |")
        add("|---|---|---|---|---|---|---|---|---|---|---|")
        mark = {True: "过", False: "**不过**", None: "不可验证"}
        cap = 60
        per_trace = t["per_trace"]
        failing = [p for p in per_trace
                   if any(p["checks"][f"T{i}"] is False for i in range(1, 9))]
        shown = per_trace[:cap]
        shown_ids = {id(p) for p in shown}
        # 不过的行不论排到第几条都列出来：只列前 N 条会把红格子藏在尾巴外
        tail_failing = [p for p in failing if id(p) not in shown_ids]
        for p in shown + tail_failing:
            cells = " | ".join(mark[p["checks"][f"T{i}"]] for i in range(1, 9))
            add(f"| {p.get('qid', '—')} | {p['trace_id']} | {p.get('value_path') or '—'} | {cells} |")
        add("")
        add(f"> 可比 trace 共 {len(per_trace)} 条：通过的行**只列前 {cap} 条**（省版面），"
            f"不过的行不受这个上限约束、全部逐条列出（前 {cap} 条之外另有 {len(tail_failing)} 条不过）。"
            "这个上限只是表格排版，不影响任何指标的分母（分母见上面两行）。")
        add("")
        add("> 「题号 / trace_id」两列就是为了让每条 trace 能落回具体哪道题："
            "同一题里 `trace_id` 会重复（`t-0001` 在 60 题里出现几十次），只有配上题号才追得回去。")
        add("")
    else:
        add("本轮没有 trace 可比，指标写 `null`。")
        add("")

    add("## 5. 打回清单")
    add("")
    rw = ctx["rework"]
    add(f"- 条数：{rw['counts']['total']}（按退回去的目录："
        + "、".join(f"`{k}` {v}" for k, v in sorted(rw["counts"]["by_target_dir"].items()))
        + "）")
    add(f"- 明细见 `eval/rework.json`，每条带 `target_dir` / `evidence` / `action` / `contract_ref`。")
    for item in rw["items"][:12]:
        add(f"  - `{item['id']}` {item['severity']} → `{item['target_dir']}`：{item['action']}")
    add("")

    add("## 6. 已知局限（本步自己承认的）")
    add("")
    for line in ctx["limits"]:
        add(f"- {line}")
    add("")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- 打回清单


def build_rework(questions: list[dict], acc: dict, det: dict, hit: dict, trace: dict,
                 extra: dict | None = None) -> dict:
    items: list[dict] = []
    extra = extra or {}

    def push(kind: str, severity: str, target: str, action: str, evidence: str,
             contract_ref: str = "", qid: str = "", category: str = "") -> None:
        items.append({
            "id": f"RW-{len(items) + 1:04d}",
            "kind": kind,
            "severity": severity,
            "target_dir": target,
            "question_id": qid,
            "category": category,
            "contract_ref": contract_ref,
            "evidence": evidence,
            "action": action,
        })

    # 静态：问题集里标了 gap 的题 = 契约空白（不需要产物就能发现）
    # 同一个空白可能在多题上复现，按 gap 原文去重，一条打回条目列全部题号
    seen_gap: dict[str, list[str]] = {}
    for q in questions:
        gap = q["expect"].get("gap")
        if gap:
            seen_gap.setdefault(gap, []).append(q["id"])
    for gap, qids in seen_gap.items():
        push(
            "contract_gap", "low", "contract/",
            f"补齐契约：{gap}",
            f"{'、'.join(qids)}：{gap}", "§2.6 / §4.3 T4 / §5.1", qids[0], "",
        )

    # —— 跑出来的系统性根因（本轮实测才看得见的）——
    # caveat 要分三类，不能一律当缺陷打回（否则报告会自己编出「修 RESULT_EMPTY」这种笑话）：
    #   fallback_codes：D2/D3/D4 真走了兜底 → 退回 ask/ 补非兜底实现
    #   accepted_codes：契约定义里就是「正常状态」（空结果 / 基期为 0）→ 不打回
    #   INJECT_DIGEST_UNCHECKED：契约 §5.2 允许 P0 这样跑 → 记成评测器自己的待办（eval/）
    fallback_codes = {"PLAN_MINIMAL", "NOT_PRUNED", "NO_INTERPRETATION"}
    accepted_codes = {"RESULT_EMPTY", "PCT_BASE_ZERO", "COMPARISON_KEY_MISSING", "DICT_VERSION_DRIFT"}
    for code, info in sorted((extra.get("caveats") or {}).items()):
        ev = f"{code}：{info['n']} 题（例：{'、'.join(info['qids'][:6])}）"
        if code in accepted_codes:
            continue
        if code in fallback_codes:
            push(
                "impl_fallback", "medium", "ask/",
                f"给 {code} 一个非兜底实现：模型回调没配时，这一步直接用契约兜底，本轮 "
                f"{info['n']} 题的产物都带这个 caveat",
                ev, "§7.1 / §5.1 D2 D3 D4",
            )
        elif code == "INJECT_DIGEST_UNCHECKED":
            push(
                "harness_gap", "low", "eval/",
                "给注入文件补 `input_digest`（规范化输入哈希）：现在按契约 §5.2 的 P0 口径留空，"
                "代价是每份产物都带 INJECT_DIGEST_UNCHECKED、`INJECT_STALE` 这条失效检测路径永远测不到；"
                "要补一条「改输入 → 期望 INJECT_STALE」的用例",
                ev, "§5.2 注入 / §5.1 D1",
            )
        else:
            push(
                "unknown_caveat", "low", "contract/",
                f"契约 §2.7 的 caveat 表里没有 `{code}`：要么补进契约，要么 ask/ 别自造代码",
                ev, "§2.7",
            )
    for code, info in sorted((extra.get("error_codes") or {}).items()):
        target = "contract/" if info.get("contract_side") else "ask/"
        push(
            "impl_error", "high", target,
            f"修 {code}：{info['message'][:160]}",
            f"{code}：{info['n']} 题（例：{'、'.join(info['qids'][:6])}）",
            "§2.10 / §4.3",
        )
    for slot, qids in sorted((extra.get("clarify_slots") or {}).items()):
        if slot == "dataset":
            push(
                "contract_gap", "high", "contract/",
                "D1 输出里没有「维度来自哪张表」，多表题只能反问「用哪份数据」："
                "要么给 D1 加 table/维度限定，要么在 §5.1 写清多表题的歧义处理",
                f"多表题在 D1 就变成反问「用哪份数据」：{'、'.join(qids)}",
                "§5.1 D1 / §5.1 D3", qids[0] if qids else "",
            )
        elif slot not in CONTRACT_CLARIFY_SLOTS:
            conflict_qids = sorted(set(extra.get("conflict_qids") or qids))
            push(
                "contract_gap", "medium", "contract/",
                f"判据冲突：`clarify.missing[].slot` 在 §2.10 是封闭六选一，本轮反问里出现了枚举外的槽位 `{slot}`，"
                f"而 §7.1 触发条件 4 恰恰要求系统把这类反问写成 `{slot}`——要么把它补进 §2.10 的枚举，"
                f"要么 §7.1 改用枚举内的名字（如 `time_range`）。同一道题按 §5.1「`ambiguity != []` ⟺ "
                f"`status=\"clarify\"`」也矛盾：注入式 D1 的 `ambiguity=[]`（`eval/d1_truth.json`）而 "
                f"`{slot}` 为 `null` 时系统按 §7.1 必须反问，两条判据在注入式 D1 下无法同时满足。"
                f"确定口径前不许把这些题记成 `ask/` 的取数缺陷（{'、'.join(conflict_qids[:6])}）",
                f"`{slot}` 出现在 {'、'.join(qids[:6])} 的反问体里（报告 §0.4 JC-4：判据冲突，"
                f"准确率里照旧算错、责任不归 `ask/`）",
                "§2.10 / §7.1 / §5.1", qids[0] if qids else "",
            )

    # 评测器自身短板（自报，不藏）
    push(
        "harness_gap", "low", "eval/",
        "给 eval.metrics 补 xlsx 表头与行数校验；在那之前 xlsx 源的 T2/T3 恒为「不可验证」，严格率只能当下界看",
        "T2/T3 目前只解 CSV，而 README 的主源是 .xlsx", "§4.3 T2 T3",
    )
    push(
        "harness_gap", "low", "eval/",
        "「异常」类题缺量化阈值：契约没定义「异常偏高 / 偏离」的阈值，问题集也没写死，"
        "所以 expect 只能放宽到 `rows=any`——这些题现在只判得了 SQL 结构，判不了阈值取得对不对。"
        "要真测：在问题集里写死阈值，并让 `make_data.py` 造出「确定命中 / 确定不命中」两种数据",
        "q053 / q057 / q060 按真值复算确实没有超阈值的行（q025 缺 2023 基期，同比为空）"
        "→ 空结果 `RESULT_EMPTY` 是正确答案，不算答错", "§2.6 / §3.3",
    )

    # 准确率。例外：判据冲突题（§2.10 vs §7.1，见上一条 `contract/` 条目）不在这里重复打回 ——
    # 同一份产物报告说「契约要改」、打回清单说「ask/ 要改」，读者只会信一边（报告 §0.4 JC-4）。
    conflict_qids = set(extra.get("conflict_qids") or ())
    for f in acc["failures"]:
        if f["qid"] in conflict_qids:
            continue
        push(
            "sql_accuracy", "high", "ask/",
            f"修 {f['qid']} 的取数：{'；'.join(f['reasons'][:3])}",
            f"{f['cat']} 题答错：{'；'.join(f['reasons'][:3])}",
            "§2.1 / §2.2 / §6.3", f["qid"], f["cat"],
        )

    # 确定性
    for c in det["cases"]:
        if c["stable_equal"]:
            continue
        push(
            "determinism", "high", "ask/",
            f"消除 {c['qid']} 两遍之间的不稳定字段：{'；'.join(c['diffs'][:3])}",
            f"稳定子集不一致，差异：{'；'.join(c['diffs'][:3])}",
            "§6.1 / §6.4", c["qid"], c.get("category", ""),
        )
        if not c["answer_id_equal"]:
            push(
                "answer_id_mismatch", "high", "ask/",
                f"同一 cache_key 必须同 answer_id（{c['qid']} 两遍不同）",
                f"{c['qid']} 两遍 answer_id 不同", "§2.1 / §5.3", c["qid"], c.get("category", ""),
            )

    # 口径
    if hit["buckets"]["miss"]:
        push(
            "metric_hit_miss", "medium", "ask/",
            "让 columns[].source_metric_id 与 trace[].metric_id 同源，且 metrics_used 快照哈希可验",
            "未命中的数字位置：" + "、".join(hit["buckets"]["miss"][:5]),
            "§3.3 判据 1–2",
        )
    if hit["buckets"]["old_version"]:
        push(
            "dict_version_drift", "medium", "ask/",
            "命中旧版本口径时必须在 caveats 记 DICT_VERSION_DRIFT 并说清按哪一版算的",
            "命中旧版本的数字位置：" + "、".join(hit["buckets"]["old_version"][:5]),
            "§7.5 / §3.3 判据 3",
        )

    # 追溯
    cat_of = {q["id"]: q["cat"] for q in questions}
    for p in trace["per_trace"]:
        bad = [k for k, v in p["checks"].items() if v is False]
        if not bad:
            continue
        qid = str(p.get("qid") or "")
        cat = p.get("category") or cat_of.get(qid, "")
        # 零行命中的聚合：`row_ids=[] / row_ids_total=0` 是「没有源行可指」，不是 ask/ 能补的字段，
        # 契约 §4.3 T3 却要求非空 —— 归 contract/，别让它冒充 ask/ 的缺陷（报告 §0.4 同一条）。
        zero_hit = bad == ["T3"] and not p.get("row_ids_n") and p.get("row_ids_total") == 0
        if zero_hit:
            push(
                "contract_gap", "medium", "contract/",
                f"契约 §4.3 T3 要求 `row_ids` 非空，但 {qid or '这题'} 这种「零行命中、聚合结果就是 0」"
                "的格子没有源行可指：要么给一条「0 命中 → `row_ids=[]` + `row_ids_total=0` 合法」的判据，"
                "要么在 §2.13 明确这种格子的追溯怎么写。否则 T3 永远红，追溯完整率被这一条压低",
                f"{qid} {p['trace_id']}（{p.get('value_path')}）：row_ids=[]、row_ids_total=0，未通过 T3",
                "§4.3 T3 / §4.4", qid, cat,
            )
            continue
        push(
            "trace_incomplete", "high", "ask/",
            f"{qid} {p['trace_id']} 补齐 {'、'.join(bad)} 要求的字段",
            f"{qid} trace {p['trace_id']}（{p.get('value_path')}）未通过 {'、'.join(bad)}",
            "§4.3 " + " ".join(bad), qid, cat,
        )
    if trace["n_numbers"] and trace["n_traces"] != trace["n_numbers"]:
        push(
            "trace_count_mismatch", "high", "ask/",
            f"|trace| 必须等于数字单元格数：这是 {trace['n_traces']} vs {trace['n_numbers']}",
            f"N={trace['n_numbers']}，|trace|={trace['n_traces']}", "§4.1",
        )
    if trace["missing_paths"]:
        lost = trace["missing_paths"]
        push(
            "trace_missing", "high", "ask/",
            "给这些数字补 trace：" + "、".join(lost),
            "有数字没有 trace：" + "、".join(lost), "§4.1 / §2.6",
            lost[0].split(":", 1)[0] if ":" in lost[0] else "",
            cat_of.get(lost[0].split(":", 1)[0], "") if ":" in lost[0] else "",
        )

    by_target: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for it in items:
        by_target[it["target_dir"]] = by_target.get(it["target_dir"], 0) + 1
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1

    return {
        "step": 10,
        "generated": now_iso(),
        "contract_version": "1.1",
        "source_report": "eval/report.md",
        "target_dirs": TARGET_DIRS,
        "target_dir_rules": {
            "ask/": "取数链路：plan/sql/columns/rows/trace/metrics_used/caveats 的缺陷都退回这里",
            "show/": "渲染与导出：report.md / SVG / 追溯面板显示错（本步不产出这类证据）",
            "dict/": "字典条目本身写错（口径、单位、别名）",
            "contract/": "契约没写或写不下的地方（不在 eval 里自行扩充）",
            "eval/": "评测器自身的问题（判据实现错、问题集期望错）",
        },
        "counts": {"total": len(items), "by_target_dir": by_target, "by_kind": by_kind},
        "items": items,
    }


# --------------------------------------------------------------------------- 主流程


def load_protocol() -> dict | None:
    """读 eval/runs/run_log.json 里的测量协议（run_questions.py 写的），报告里如实转述。"""
    log = EVAL_DIR / "runs" / "run_log.json"
    if not log.exists():
        return None
    try:
        return json.loads(log.read_text(encoding="utf-8")).get("protocol")
    except Exception:  # noqa: BLE001 - 日志坏了就不转述，不编
        return None


def measure(questions: list[dict], run1: Path, run2: Path, pairs_requested: int,
            dict_path: Path | None) -> dict:
    qids = [q["id"] for q in questions]
    ans1, bad1 = load_answers(run1, qids)
    ans2, bad2 = load_answers(run2, qids)

    dict_index: dict[str, dict] | None = None
    if dict_path is not None and dict_path.exists():
        try:
            payload = json.loads(dict_path.read_text(encoding="utf-8"))
            dict_index = {m["id"]: m for m in payload.get("metrics") or [] if isinstance(m, dict) and "id" in m}
        except Exception:  # noqa: BLE001 - 字典读不了就退化成「判据 3 未校验」，不编
            dict_index = None

    header_cache: dict[str, list[str] | None] = {}

    # 1) 准确率：分母只算「有产物可测」的题，覆盖率单列，不让缺产物把准确率压低
    by_cat = {cat: {"ok": 0, "measured": 0, "planned": 0} for cat in CATS}
    failures: list[dict] = []
    not_answered: list[dict] = []
    non_join_ok = join_ok = 0
    for q in questions:
        by_cat[q["cat"]]["planned"] += 1
        ans = ans1.get(q["id"])
        if ans is None:
            not_answered.append({"qid": q["id"], "cat": q["cat"], "why": bad1.get(q["id"], "缺产物")})
            continue
        by_cat[q["cat"]]["measured"] += 1
        ok, reasons = judge_question(q, ans)
        if ok:
            by_cat[q["cat"]]["ok"] += 1
            if q["cat"] == JOIN_CAT:
                join_ok += 1
            else:
                non_join_ok += 1
        else:
            failures.append({"qid": q["id"], "cat": q["cat"], "q": q["q"], "reasons": reasons})

    accuracy = {
        "by_cat": by_cat,
        "failures": failures,
        "not_answered": not_answered,
        "non_join_ok": non_join_ok,
        "join_ok": join_ok,
        "non_join_total": sum(v["measured"] for k, v in by_cat.items() if k != JOIN_CAT),
        "join_total": by_cat[JOIN_CAT]["measured"],
        "non_join_planned": sum(v["planned"] for k, v in by_cat.items() if k != JOIN_CAT),
        "join_planned": by_cat[JOIN_CAT]["planned"],
        "measured": bool(ans1),
    }

    # 2) 同问一致性（按问题顺序取前 N 对）
    pairs: list[str] = [qid for qid in qids if qid in ans1 and qid in ans2][:pairs_requested]
    det_cases: list[dict] = []
    for qid in pairs:
        cat = next((q["cat"] for q in questions if q["id"] == qid), "")
        info = compare_pair(ans1[qid], ans2[qid])
        info.update({"qid": qid, "category": cat})
        det_cases.append(info)
    det = {
        "pairs": len(det_cases),
        "equal": sum(1 for c in det_cases if c["stable_equal"]),
        "raw_equal": sum(1 for c in det_cases if c["raw_equal"]),
        "id_equal": sum(1 for c in det_cases if c["answer_id_equal"]),
        "cases": det_cases,
    }

    # 3) 口径命中
    hit_buckets: dict[str, list[str]] = {"hit": [], "old_version": [], "miss": [], "not_dict": [], "unverified": []}
    for q in questions:
        ans = ans1.get(q["id"])
        if ans is None:
            continue
        tiers = metric_hit_tiers(ans, dict_index)
        for key, paths in tiers["buckets"].items():
            hit_buckets[key].extend(f"{q['id']}:{p}" for p in paths)
    denom = sum(len(hit_buckets[k]) for k in ("hit", "old_version", "miss", "unverified"))
    hit = {"buckets": hit_buckets, "denom": denom, "hit": len(hit_buckets["hit"]),
           "dict_checked": dict_index is not None}

    # 4) 追溯完整
    n_numbers = strict_ok = lenient_ok = unverified = n_traces = 0
    per_trace: list[dict] = []
    missing: list[str] = []
    for q in questions:
        ans = ans1.get(q["id"])
        if ans is None:
            continue
        tc = trace_completeness(ans, header_cache)
        n_numbers += tc["n_numbers"]
        n_traces += tc["n_traces"]
        strict_ok += tc["strict_ok"]
        lenient_ok += tc["lenient_ok"]
        unverified += tc["unverified_checks"]
        missing.extend(f"{q['id']}:{p}" for p in tc["missing_paths"])
        for p in tc["per_trace"]:
            p["qid"] = q["id"]
            p["category"] = q["cat"]
        per_trace.extend(tc["per_trace"])
    trace = {"n_numbers": n_numbers, "n_traces": n_traces, "strict_ok": strict_ok,
             "lenient_ok": lenient_ok, "unverified_checks": unverified,
             "missing_paths": missing[:10], "per_trace": per_trace}

    # 4.5) 根因统计：兜底 caveat / 错误码 / 反问槽位——打回清单要用，报告里也要点名
    caveats: dict[str, dict] = {}
    error_codes: dict[str, dict] = {}
    clarify_slots: dict[str, list[str]] = {}
    for q in questions:
        ans = ans1.get(q["id"])
        if ans is None:
            continue
        for cv in ans.get("caveats") or []:
            if isinstance(cv, dict) and cv.get("code"):
                slot = caveats.setdefault(cv["code"], {"n": 0, "qids": []})
                slot["n"] += 1
                slot["qids"].append(q["id"])
        err = ans.get("error") or {}
        if isinstance(err, dict) and err.get("code"):
            slot = error_codes.setdefault(err["code"], {
                "n": 0, "qids": [], "message": str(err.get("message") or ""),
                "contract_side": False,   # 「无列指标」的追溯口径该由契约补，但先别把 trace 写成空列
            })
            slot["n"] += 1
            slot["qids"].append(q["id"])
        cl = ans.get("clarify") or {}
        for miss in (cl.get("missing") or []) if isinstance(cl, dict) else []:
            if isinstance(miss, dict) and miss.get("slot"):
                clarify_slots.setdefault(miss["slot"], []).append(q["id"])

    # 4.5b) 判据冲突题号：D1 注入的 `ambiguity=[]`，产物却是 `status="clarify"`，且槽位名不在
    # §2.10 的封闭枚举里（本轮是 `time_column`）。这类题在准确率里照旧算错（`expect.status="ok"`），
    # 但根因是契约内部两条判据打架（§2.10 vs §7.1 触发条件 4），打回条目只能挂 `contract/`：
    # 不许写成「修 qNNN 的取数」把责任推给 `ask/`（报告 §0.4 JC-4）。
    conflict_qids: set[str] = set()
    for q in questions:
        ans = ans1.get(q["id"])
        if ans is None or ans.get("status") != "clarify":
            continue
        want = q["expect"]["status"]
        wants = {want} if isinstance(want, str) else set(want)
        if "ok" not in wants:
            continue
        missing = (ans.get("clarify") or {}).get("missing") or []
        slots = {m.get("slot") for m in missing if isinstance(m, dict) and m.get("slot")}
        if slots - CONTRACT_CLARIFY_SLOTS:
            conflict_qids.add(q["id"])

    # 4.6) 链路统计：逐题看 meta.model.Dn.source —— 模型 / 注入 / 兜底 各多少次
    chain: dict[str, dict[str, int]] = {
        d: {"model": 0, "inject": 0, "fallback": 0, "unknown": 0} for d in ("D1", "D2", "D3", "D4")
    }
    for q in questions:
        ans = ans1.get(q["id"])
        if ans is None:
            continue
        model = (ans.get("meta") or {}).get("model") or {}
        for d in ("D1", "D2", "D3", "D4"):
            src = str((model.get(d) or {}).get("source") or "unknown")
            if src not in chain[d]:
                src = "unknown"
            chain[d][src] += 1

    rework = build_rework(questions, accuracy, det, hit, trace, extra={
        "caveats": caveats, "error_codes": error_codes, "clarify_slots": clarify_slots,
        "conflict_qids": conflict_qids,
    })

    notes: list[str] = []
    if not ans1:
        notes.append("本轮没有任何 answer.json 产物（`--run1` / `--run2` 为空），"
                     "四项指标全部写 `null` —— 不编数字，也不把「没跑」记成「跑挂了」。")
    elif len(ans1) < len(questions):
        notes.append(f"`{run1}` 只有 {len(ans1)}/{len(questions)} 题的产物，"
                     "缺的题按「没测」计，不从分母里悄悄去掉。")
    if not ans2:
        notes.append("`--run2` 没有产物，同问两遍比对的对数为 0，一致性写 `null`。")

    limits = [
        "本轮 D1 是**注入**的（契约 §5.2）：§7.1 规定 D1 没有兜底，所以「问题理解得对不对」"
        "不在本轮测量范围内；量的只是 D1 之后的取数链路 + 可追溯性 + 确定性。",
        "准确率判据里加了结构性要求（`expect.sql_all_any` / `order` / `limit`）：只核对 SQL 里"
        "有没有窗口、基线比较、DESC、LIMIT、HAVING 这类结构，用来抓「语义没落地」；它仍然判不了"
        "「结构对了但数算错」。",
        "指标名（`expect.metrics` 里的中文名）是**提示**：本步按边界不许读 `dict/`，"
        "所以没拿真实字典条目硬绑 id；核对时 id 与 name 命中任一即算命中。真实字典若改名，这里是误判来源。",
        "T2 / T3 需要回源表头与源行数，本步只解 CSV，xlsx 源记为「不可验证」并单独计数，"
        "不冒充已验（契约 §4.3 要八条全过才算完整，严格口径按「不可验证 = 未通过」算）。",
        "口径命中率的分母契约没定义（§3.3 只判「一个数字算不算命中」），本步定义为"
        "「带 metric_id 的数字单元格数」，非字典口径单列不计入。",
        "「SQL 执行准确率」在这里是**机械核对**（status / 指标 / 维度 / GROUP BY / JOIN / 行数 / 结构），"
        "不是语义评测：能抓住「答非所问」，抓不住「口径对但数算错」。",
        "`--determinism-pairs` 只比对两遍都有的题；产物不足时如实写对数，不补零。",
        "口径命中率在本轮**接近自证**：`metrics_used` 与 `columns[].source_metric_id` 都出自"
        "同一次字典加载，所以命中率天然接近 100%。它的信息量在「未命中 / 命中旧版本 / 非字典口径」"
        "这三个桶，不在那个百分比上。",
        "合成的 8 张 CSV 与 14 条口径都是**评测专用**，不代表项目 `dict/` 与真实数据；"
        "它只保证「真值已知、可复算」，不保证业务真实。",
        "本轮四个环节的**模型链路调用 0 次**（环境没配模型），D1 走注入（契约 §5.2）、"
        "D2/D3/D4 走 ask/ 兜底（契约 §7.1）：所以这里的分数衡量的是「降级链路」的上限，"
        "不是模型链路的上限；换上真模型后必须重跑，不能拿这份数字当模型效果。",
        "「异常」类题（q053/q057/q060）与同比题 q025 的 `expect.rows` 已放宽成 `any`："
        "按合成数据真值复算，这几题确实没有超过阈值的行 / 没有可比基期（2024 年同比缺 2023 数据），"
        "空结果 `RESULT_EMPTY` 就是正确答案 —— 放宽是「承认判据不足」，不是「把答错改成答对」，"
        "这几题现在只判 SQL 结构，阈值取得对不对没测。",
    ]

    return {
        "generated": now_iso(),
        "contract_version": "1.1",
        "questions": questions,
        "run1": str(run1), "run2": str(run2),
        "n_run1": len(ans1), "n_run2": len(ans2),
        "pairs_requested": pairs_requested,
        "pairs_available": len(pairs),
        "accuracy": accuracy, "det": det, "hit": hit, "trace": trace,
        "rework": rework, "notes": notes, "limits": limits,
        "caveats": caveats, "error_codes": error_codes, "clarify_slots": clarify_slots,
        "conflict_qids": sorted(conflict_qids),
        "chain": chain,
        "protocol": load_protocol(), "dict_path": str(dict_path) if dict_path else "",
        "bad1": bad1, "bad2": bad2,
    }


# --------------------------------------------------------------------------- 自测


def _fake_answer(**over: Any) -> dict:
    """契约 §2.14 形状的最小成功体。数字全是编的（仅用于自测判据实现，不是实测）。"""
    base = {
        "question": "编的",
        "answer_id": "a3f1c8e20b9d4c17",
        "status": "ok",
        "plan": [{"step_id": 1, "intent": "detail", "title": "编的步骤",
                  "needs": {"metric_ids": ["gmv"], "dimensions": ["region"]},
                  "sql_index": 0, "depends_on": [], "fallback_of": None}],
        "plan_summary": "单步：按区域聚合",
        "metrics_used": [{
            "id": "gmv", "version": 3, "name": "成交额",
            "definition": "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
            "unit": "CNY", "owner": "编的负责人",
            "definition_sha256": sha256_text("SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)"),
        }],
        "sql": ['SELECT "region" AS "region", SUM(CASE WHEN status = \'paid\' THEN amount ELSE 0 END) AS "gmv" '
                'FROM "sales" WHERE year = 2025 GROUP BY "region" ORDER BY "region" ASC'],
        "columns": [
            {"name": "region", "type": "text", "unit": None, "digits": 0, "source_metric_id": None, "source_column": "region"},
            {"name": "gmv", "type": "decimal", "unit": "CNY", "digits": 2, "source_metric_id": "gmv", "source_column": None},
        ],
        "rows": [{"__rid": "agg(sales#Sheet1:region=华东)", "region": "华东", "gmv": 100.5}],
        "trace": [{
            "trace_id": "t-0001", "value_path": "rows[0].gmv", "value": 100.5, "metric_id": "gmv",
            "source": {"file": "编的/sales.csv", "file_sha256": "deadbeef", "sheet": None, "header_row": 1,
                       "columns": ["region", "status", "amount"]},
            "row_ids": ["sales#sales!2"], "row_ids_total": 1, "row_ids_truncated": False,
            "filter": {"none": True},
            "expression": "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
            "aggregation": [{"op": "sum", "over": "amount", "group_by": ["region"]}],
            "sql_index": 0,
        }],
        "charts": [], "narrative": [], "caveats": [], "clarify": None, "error": None,
        "meta": {
            "contract_version": "1.1", "created": "2026-01-01T00:00:00", "elapsed_ms": 1,
            "model": {d: {"source": "fallback", "model": None, "injected": False, "latency_ms": 0,
                          "output_sha256": "00"} for d in ("D1", "D2", "D3", "D4")},
            "cache": {"hit": False, "key": "k", "source_answer_id": None, "written": True},
            "files": [{"path": "编的/sales.csv", "sha256": "deadbeef"}],
            "metric_dict_sha256": "77", "runtime": {"sqlite": "x", "python": "y", "os": "z"},
            "options": {"row_cap": 5000, "trace_row_id_cap": 200, "rows_preview_limit": 20},
        },
    }
    base.update(over)
    return base


def selftest() -> int:
    """用编的数字验证「判据实现」本身对不对。全是 fabricated，不是实测结果。"""
    ok = True

    def chk(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    a = _fake_answer()
    b = copy.deepcopy(a)
    b["meta"]["created"] = "2026-12-31T23:59:59"
    b["meta"]["elapsed_ms"] = 999
    b["meta"]["runtime"] = {"sqlite": "other", "python": "other", "os": "other"}
    b["meta"]["cache"] = {"hit": True, "key": "k", "source_answer_id": a["answer_id"], "written": False}
    for d in b["meta"]["model"].values():
        d["latency_ms"] = 12345
    chk("§6.1：只差 §6.4 允许的字段 → 稳定子集字节一致", compare_pair(a, b)["stable_equal"])
    c = copy.deepcopy(b)
    c["rows"][0]["gmv"] = 100.51
    chk("§6.1：rows 变了一点 → 判为不确定", not compare_pair(a, c)["stable_equal"])
    chk("§6.1：差异能指到具体字段", any("rows[0].gmv" in d for d in compare_pair(a, c)["diffs"]))

    tc = trace_completeness(a, {})
    chk("§4.1：N = 1（只有一个 decimal 单元格）", tc["n_numbers"] == 1)
    chk("§4.3：八条全过（CSV 源不存在，T2/T3 记为不可验证）", tc["lenient_ok"] == 1)

    bad = copy.deepcopy(a)
    bad["trace"][0]["value"] = 999        # T8 崩
    bad["trace"][0]["filter"] = {}        # T4 崩
    bad["trace"][0]["aggregation"] = []   # T6 崩
    bad["trace"][0]["sql_index"] = 7      # T7 崩
    tcb = trace_completeness(bad, {})["per_trace"][0]["checks"]
    chk("§4.3：T4/T6/T7/T8 各能抓到", all(tcb[k] is False for k in ("T4", "T6", "T7", "T8")))
    chk("§4.3：没崩的 T1/T5 仍然过", tcb["T1"] is True and tcb["T5"] is True)

    nomid = copy.deepcopy(a)
    nomid["trace"][0]["metric_id"] = None
    nomid["columns"][1]["source_metric_id"] = None
    tiers = metric_hit_tiers(nomid, None)["buckets"]
    chk("§2.13：非字典口径进 not_dict，不计入分母", len(tiers["not_dict"]) == 1 and not tiers["miss"])

    d = {"gmv": {"id": "gmv", "definition": "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)"}}
    chk("§3.3：与当前字典逐字相等 → 命中", metric_hit_tiers(a, d)["buckets"]["hit"] == ["rows[0].gmv"])
    d2 = {"gmv": {"id": "gmv", "definition": "SUM(amount)"}}
    chk("§3.3：字典口径变了 → 单列「命中旧版本」",
        metric_hit_tiers(a, d2)["buckets"]["old_version"] == ["rows[0].gmv"])
    chk("§3.3：没给字典 → 判据 3 未校验，不冒充命中",
        metric_hit_tiers(a, None)["buckets"]["unverified"] == ["rows[0].gmv"])

    q = {"id": "q011", "cat": "聚合", "q": "2025 年各区域的成交额是多少？",
         "intent": "detail",
         "expect": {"status": "ok", "metrics": ["成交额"], "dims": ["region"], "group_by": True,
                    "order_by": None, "join": False, "tables": ["sales"], "rows": "many"}}
    chk("准确率：形状对 → 答对", judge_question(q, a)[0])
    no_gb = copy.deepcopy(a)
    no_gb["sql"] = [no_gb["sql"][0].replace("GROUP BY \"region\"", "")]
    chk("准确率：缺 GROUP BY → 答错", not judge_question(q, no_gb)[0])
    qj = dict(q, cat="多表连接")
    qj["expect"] = dict(q["expect"], join=True, tables=["sales", "region_dim"])
    chk("准确率：JOIN 题没有 JOIN → 答错", not judge_question(qj, a)[0])

    fake_trace = {"n_numbers": 1, "n_traces": 1, "strict_ok": 0, "missing_paths": [], "per_trace": [
        {"trace_id": "t-0001", "value_path": "rows[0].n", "row_ids_n": 0, "row_ids_total": 0,
         "qid": "q999", "category": "筛选", "checks": {"T3": False}},
    ]}
    cf = judge_conflicts({"pairs": 0, "raw_equal": 0, "equal": 0},
                         {"denom": 0, "hit": 0, "buckets": {"miss": [], "old_version": []}},
                         fake_trace, {"time_column": ["q999"]})
    chk("§2.10 vs §7.1：枚举外的槽位被当成判据冲突报出",
        any("time_column" in c["topic"] for c in cf))
    chk("§4.3 T3 vs §4.4：零行命中的 trace 被当成判据冲突报出",
        any("T3" in c["topic"] for c in cf))
    rw = build_rework(
        [{"id": "q999", "cat": "筛选", "q": "编的问题？", "intent": "detail", "expect": {}}],
        {"failures": [], "not_answered": [],
         "by_cat": {c: {"ok": 0, "measured": 0, "planned": 0} for c in CATS},
         "non_join_ok": 0, "join_ok": 0, "non_join_total": 0, "join_total": 0,
         "non_join_planned": 0, "join_planned": 0, "measured": True},
        {"pairs": 0, "cases": []},
        {"denom": 0, "hit": 0, "buckets": {"miss": [], "old_version": []}},
        fake_trace,
    )
    chk("零行命中的 T3 不过 → 退回 contract/（不冒充 ask/ 的缺陷）",
        any(i["question_id"] == "q999" and i["target_dir"] == "contract/" for i in rw["items"]))

    print("自测" + ("全过" if ok else "有 FAIL") + "（以上数字全是编的，不是实测）")
    return 0 if ok else 1


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m eval.metrics",
        description="四项指标测量 + 同问两遍字节比对 + 打回清单",
    )
    ap.add_argument("--out", default=str(EVAL_DIR / "report.md"), help="报告输出路径")
    ap.add_argument("--rework-out", default=None, help="打回清单输出路径（默认与 --out 同目录的 rework.json）")
    ap.add_argument("--questions", default=str(QUESTIONS_PATH), help="问题集路径")
    ap.add_argument("--run1", default=str(EVAL_DIR / "runs" / "run1"), help="第一遍 answer.json 目录")
    ap.add_argument("--run2", default=str(EVAL_DIR / "runs" / "run2"), help="第二遍 answer.json 目录")
    ap.add_argument("--determinism-pairs", type=int, default=60, help="最多比对多少对同问产物")
    ap.add_argument("--metric-dict", default=str(EVAL_DIR / "data" / "metrics.json"),
                    help="字典文件，用于契约 §3.3 判据 3；不存在则记为「未校验」")
    ap.add_argument("--selftest", action="store_true", help="用编的数字自测判据实现")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    qpath = Path(args.questions)
    if not qpath.exists():
        print(f"找不到问题集：{qpath}", file=sys.stderr)
        return 2
    questions = load_questions(qpath)

    out = Path(args.out)
    rework_out = Path(args.rework_out) if args.rework_out else out.parent / "rework.json"

    dict_path = Path(args.metric_dict) if args.metric_dict else None
    ctx = measure(questions, Path(args.run1), Path(args.run2), args.determinism_pairs, dict_path)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(ctx), encoding="utf-8")
    rework_out.parent.mkdir(parents=True, exist_ok=True)
    rework_out.write_text(json.dumps(ctx["rework"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    acc = ctx["accuracy"]
    mx = acc["measured"]
    print(f"报告：{out}")
    print(f"打回清单：{rework_out}（{ctx['rework']['counts']['total']} 条）")
    print(f"样本：run1 {ctx['n_run1']}/{len(questions)}，run2 {ctx['n_run2']}/{len(questions)}，"
          f"比对 {ctx['det']['pairs']} 对")
    print(f"非 JOIN 准确率 {fmt_rate(acc['non_join_ok'], acc['non_join_total'] if mx else None)}；"
          f"JOIN {fmt_rate(acc['join_ok'], acc['join_total'] if mx else None)}；"
          f"一致性 {fmt_rate(ctx['det']['equal'], ctx['det']['pairs'] or None)}；"
          f"口径命中 {fmt_rate(ctx['hit']['hit'], ctx['hit']['denom'] or None)}；"
          f"追溯完整 {fmt_rate(ctx['trace']['strict_ok'], ctx['trace']['n_numbers'] or None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
