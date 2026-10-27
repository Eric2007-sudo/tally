"""ask.static_check · SQL 只读静态检查（契约 §8.1 的第一道闸，S1–S6）

契约出处（`contract/answer.md`）：
- §8.1 S1 写操作关键字（`REPLACE(` 作为函数放行是唯一例外）
- §8.1 S2 事务/权限/管理语句
- §8.1 S3 语句形态（只许 SELECT/WITH 开头、单语句、注释一律拒、`SELECT ... INTO` 拒、UNION 后每段都要 SELECT）
- §8.1 S4 副作用函数 / 打破确定性的东西（`random()`、`'now'` ……）
- §8.1 S5 表名白名单（`FROM`/`JOIN` 之后；`sqlite_*` 一律拒）
- §8.1 S6 标识符注入（双引号包裹 + 内部 `"` 转义成 `""`）
- §2.11 拒绝时用 `SQL_STATIC_REJECT`（不新增错误码）

本模块只做**文本层**判断，不碰数据库、不改写 SQL（§8.1：「命中即拒绝，直接拒绝，不尝试改写」）。
第二道闸（只读连接）在 `ask.execute`。

CLI：
    python -m ask.static_check --selftest        # 自检（S1–S6 正例反例各一条）
    python -m ask.static_check "SELECT ..."      # 单条检查，通过打印 PASS，拒绝打印错误体
"""

from __future__ import annotations

import sys

# --------------------------------------------------------------------------- #
# 契约 §8.1 的封闭词表（照抄，不扩充）
# --------------------------------------------------------------------------- #

# S1 写操作关键字。`REPLACE` 既是关键字也是字符串函数：只有 `REPLACE(` 形态放行（§8.1 唯一例外）
WRITE_WORDS = frozenset(
    {
        "insert",
        "replace",
        "update",
        "delete",
        "upsert",
        "create",
        "alter",
        "drop",
        "truncate",
        "rename",
        "attach",
        "detach",
        "vacuum",
        "reindex",
        "analyze",
    }
)

# S1 里「后面跟 ( 就是函数、可以放行」的唯一一个词
FUNCTION_EXEMPT = frozenset({"replace"})

# S2 事务 / 权限 / 管理语句
TXN_WORDS = frozenset(
    {
        "begin",
        "commit",
        "rollback",
        "savepoint",
        "release",
        "pragma",
        "grant",
        "revoke",
        "set",
        "copy",
        "merge",
        "call",
        "exec",
        "execute",
    }
)

# S4 副作用函数 / 破坏确定性
SIDE_EFFECT_FUNCS = frozenset(
    {
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
    }
)

# S3：只允许以这两个词开头
ALLOWED_STARTS = frozenset({"select", "with"})

# 语句里允许出现的「不是列名」的词（T5 判据用；只用于「标识符是不是列」这一件事）
SQL_WORDS = frozenset(
    """
    select from where group by order having as and or not in is null case when then else end
    distinct on join left right inner outer full cross union all limit offset asc desc
    between like glob escape cast collate exists natural using values with recursive
    sum avg count min max total cast round coalesce ifnull nullif abs lower upper substr
    trim ltrim rtrim length replace instr printf format date time datetime julianday
    strftime unixepoch date_exists iif true false
    """.split()
)

# S5：任何 sqlite_* 前缀的表名一律拒
SQLITE_PREFIX = "sqlite_"

MAX_LEN = 20_000  # 单条语句长度上限（防「一条语句塞一整篇」；不设上限就是没上限）


class StaticReject(Exception):
    """静态检查拒绝。`code` 固定 `SQL_STATIC_REJECT`（§2.11 封闭集），`stage` 固定 `static_check`。"""

    code = "SQL_STATIC_REJECT"
    stage = "static_check"

    def __init__(self, message: str, readable_hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.readable_hint = readable_hint or message

    def as_error_body(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "readable_hint": self.readable_hint,
            "stage": self.stage,
        }

    def __str__(self) -> str:  # pragma: no cover - 只为报错好看
        return f"[{self.code}] {self.message}"


# --------------------------------------------------------------------------- #
# 词法：把 SQL 切成 token（字符串字面量 / 双引号标识符 / 词 / 符号）
# --------------------------------------------------------------------------- #


def tokenize(sql: str) -> list:
    """→ `[(kind, text, pos)]`，`kind ∈ {"lit", "num", "qident", "word", "punct", "comment"}`。

    注释一律被识别成 `comment` token（随后被拒），这样「注释藏关键字」这条路从一开始就封死（§8.1 S3）。
    字符串字面量与双引号标识符单独成 token，所以 `WHERE note = 'DELETE'` 不会被 S1 误杀。
    """
    toks: list = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "'":
            start = i
            i += 1
            buf = []
            closed = False
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                buf.append(sql[i])
                i += 1
            if not closed:
                raise StaticReject(
                    f"字符串字面量没有闭合（位置 {start}）",
                    "这条查询引号没配对，先修语法再跑",
                )
            toks.append(("lit", "".join(buf), start))
            continue
        if ch == '"':
            start = i
            i += 1
            buf = []
            closed = False
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                buf.append(sql[i])
                i += 1
            if not closed:
                raise StaticReject(
                    f"双引号标识符没有闭合（位置 {start}）",
                    "这条查询的列名引号没配对",
                )
            toks.append(("qident", "".join(buf), start))
            continue
        if ch.isdigit():
            start = i
            while i < n and sql[i].isdigit():
                i += 1
            if i + 1 < n and sql[i] == "." and sql[i + 1].isdigit():
                i += 1
                while i < n and sql[i].isdigit():
                    i += 1
            toks.append(("num", sql[start:i], start))
            continue
        if ch.isalpha() or ch in "_$":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] in "_$"):
                i += 1
            toks.append(("word", sql[start:i], start))
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            toks.append(("comment", "--", i))
            i = n
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            toks.append(("comment", "/*", i))
            i = n
            continue
        if ch == "#":  # sqlite 不支持 # 注释，但把「像注释的东西」按注释处理，宁拒勿猜
            toks.append(("comment", "#", i))
            i = n
            continue
        toks.append(("punct", ch, i))
        i += 1
    return toks


def _words(toks) -> list:
    return [t for t in toks if t[0] == "word"]


# --------------------------------------------------------------------------- #
# 检查
# --------------------------------------------------------------------------- #


def _reject(where: str, message: str, hint: str) -> None:
    raise StaticReject(f"{where}: {message}", hint)


def check_sql(sql: str, *, tables=None, require_select: bool = True, where: str = "SQL") -> dict:
    """对一条 SQL（或一条指标定义表达式）做 S1–S6 检查。不通过就抛 `StaticReject`。

    - `tables`：本次导入的表名（S5 白名单）。传 `None` 表示「此刻拿不到 schema，跳过表名白名单」
      （只用于校验字典条目这种「表达式形态」，`sqlite_*` 前缀照拒）。
    - `require_select`：指标定义是聚合表达式（`SUM(...)`），不以 SELECT 开头，所以此时置 False。
      契约 V3 对定义的要求正是「不许含 `;`、注释、DML 关键字」，不含「必须以 SELECT 开头」。
    返回 `{"tables": [...], "identifiers": [...], "literals": [...]}` 供调用方核对（不用于放行）。
    """
    if not isinstance(sql, str) or not sql.strip():
        _reject(where, "空语句", "这条 SQL 是空的，没什么可执行的")
    if len(sql) > MAX_LEN:
        _reject(where, f"语句长度 {len(sql)} 超过上限 {MAX_LEN}", "单条语句太长，超出上限")

    toks = tokenize(sql)

    # ---- S3：注释一律拒 ----
    for kind, text, pos in toks:
        if kind == "comment":
            _reject(
                where,
                f"出现注释 {text!r}（位置 {pos}）",
                "查询里不许带注释：注释是藏关键字的常用手段，一律拒绝",
            )

    code_toks = [t for t in toks]
    if not code_toks:
        _reject(where, "只有空白", "这条 SQL 是空的")

    # ---- S3：只允许以 SELECT / WITH 开头 ----
    first = code_toks[0]
    if require_select:
        if first[0] != "word" or first[1].lower() not in ALLOWED_STARTS:
            _reject(
                where,
                f"语句以 {first[1]!r} 开头，只允许 SELECT / WITH",
                "只能向数据提问（SELECT），不许干别的",
            )

    # ---- S3：多语句（除末尾单个 `;` 之外，任何 `;` 都拒）----
    semis = [i for i, t in enumerate(toks) if t[0] == "punct" and t[1] == ";"]
    if semis:
        last = semis[-1] == len(toks) - 1
        if len(semis) > 1 or not last:
            _reject(where, "出现不止一个语句（`;` 只能有一个且必须在末尾）", "一次只许跑一条查询")

    # ---- S1 / S2：写操作与事务/管理关键字 ----
    for idx, (kind, text, pos) in enumerate(toks):
        if kind != "word":
            continue
        low = text.lower()
        if low in WRITE_WORDS:
            prev_is_dot = idx > 0 and toks[idx - 1][0] == "punct" and toks[idx - 1][1] == "."
            next_is_paren = idx + 1 < len(toks) and toks[idx + 1][0] == "punct" and toks[idx + 1][1] == "("
            if low in FUNCTION_EXEMPT and next_is_paren and not prev_is_dot:
                continue  # §8.1 S1 唯一例外：REPLACE( 是字符串函数，不是写操作
            _reject(
                where,
                f"出现写操作关键字 {text!r}（位置 {pos}）",
                f"「{text}」会改数据，这里只许查不许写",
            )
        if low in TXN_WORDS:
            _reject(
                where,
                f"出现事务/权限/管理关键字 {text!r}（位置 {pos}）",
                f"「{text}」是管理类语句，只读查询里不该出现",
            )

    # ---- S3：SELECT ... INTO 拒；UNION 后每段必须以 SELECT 开头 ----
    for idx, (kind, text, pos) in enumerate(toks):
        if kind != "word":
            continue
        low = text.lower()
        if low == "into":
            _reject(where, f"出现 INTO（位置 {pos}）", "`SELECT ... INTO` 会把结果写出去，拒绝")
        if low == "union":
            j = idx + 1
            if j < len(toks) and toks[j][0] == "word" and toks[j][1].lower() in ("all", "distinct"):
                j += 1
            if j >= len(toks) or toks[j][0] != "word" or toks[j][1].lower() != "select":
                _reject(where, f"UNION 之后不是 SELECT（位置 {pos}）", "UNION 的每一段都必须是 SELECT")

    # ---- S4：副作用函数 + 'now' ----
    for idx, (kind, text, pos) in enumerate(toks):
        if kind == "word":
            low = text.lower()
            if low in SIDE_EFFECT_FUNCS:
                prev_is_dot = idx > 0 and toks[idx - 1][0] == "punct" and toks[idx - 1][1] == "."
                next_is_paren = idx + 1 < len(toks) and toks[idx + 1][0] == "punct" and toks[idx + 1][1] == "("
                if next_is_paren and not prev_is_dot:
                    _reject(
                        where,
                        f"出现副作用/不确定函数 {text}()（位置 {pos}）",
                        f"「{text}()」每次结果都不一样（或能碰文件系统），会破坏「同问同答」",
                    )
        elif kind == "lit" and text.strip().lower() == "now":
            _reject(
                where,
                f"出现时间常量 'now'（位置 {pos}）",
                "时间范围只能来自问题理解（D1 的 time_range），不许在 SQL 里取当前时间",
            )

    # ---- S5：FROM / JOIN 之后的表名白名单 ----
    found_tables: list = []
    cte_names: set = set()
    if code_toks and code_toks[0][0] == "word" and code_toks[0][1].lower() == "with":
        for idx, (kind, text, pos) in enumerate(toks[:-1]):
            if kind == "word" and text.lower() == "as":
                prev = toks[idx - 1] if idx > 0 else None
                nxt = toks[idx + 1]
                if prev and prev[0] in ("word", "qident") and nxt[0] == "punct" and nxt[1] == "(":
                    cte_names.add(str(prev[1]).lower())

    for idx, (kind, text, pos) in enumerate(toks):
        if kind != "word":
            continue
        low = text.lower()
        if low not in ("from", "join"):
            continue
        j = idx + 1
        if j >= len(toks):
            _reject(where, f"{text.upper()} 后面什么都没有（位置 {pos}）", "FROM/JOIN 后面缺表名")
        nkind, ntext, npos = toks[j]
        if nkind == "punct" and ntext == "(":
            continue  # 子查询：里面还会有 FROM，由同一循环接住
        if nkind not in ("word", "qident"):
            _reject(where, f"{text.upper()} 后面不是表名（位置 {npos}）", "FROM/JOIN 后面必须是一个表名")
        name = str(ntext)
        low_name = name.lower()
        if low_name.startswith(SQLITE_PREFIX):
            _reject(
                where,
                f"引用了系统表 {name!r}（位置 {npos}）",
                "查询不许直接读 sqlite 的系统表",
            )
        found_tables.append(name)
        if tables is not None and low_name not in cte_names:
            allowed = {str(t).strip('"').lower() for t in tables}
            if low_name not in allowed:
                _reject(
                    where,
                    f"表 {name!r} 不在本次导入的表里（允许：{sorted(allowed)}）",
                    f"这份数据里没有「{name}」这张表",
                )

    idents = identifiers(toks)
    literals = [t[1] for t in toks if t[0] == "lit"]
    return {"tables": found_tables, "identifiers": idents, "literals": literals}


def index_of_word(toks, idx) -> bool:
    return 0 <= idx < len(toks) and toks[idx][0] == "word"


def identifiers(toks_or_sql) -> list:
    """→ 表达式里出现的**标识符**（词法意义上的词，去掉 SQL 关键字/函数 + 字符串字面量）。

    契约 T5 用这个判据：`expression` 里出现的所有标识符都要属于 `source.columns` ∪ 关键字白名单。
    """
    toks = tokenize(toks_or_sql) if isinstance(toks_or_sql, str) else toks_or_sql
    out: list = []
    for kind, text, _pos in toks:
        if kind == "qident":
            if text not in out:
                out.append(text)
        elif kind == "word" and text.lower() not in SQL_WORDS:
            if text not in out:
                out.append(text)
    return out


def check_identifiers(expression: str, allowed_columns, *, where: str = "expression") -> None:
    """契约 T5：`expression` 里的标识符只能是 `source.columns` 里的列名。"""
    allowed = {str(c) for c in allowed_columns}
    bad = [i for i in identifiers(expression) if i not in allowed]
    if bad:
        _reject(
            where,
            f"表达式 {expression!r} 用了没声明在 source.columns 里的标识符 {bad}",
            f"表达式只许引用 {sorted(allowed)}",
        )


# --------------------------------------------------------------------------- #
# S6：标识符注入
# --------------------------------------------------------------------------- #


def quote_ident(name: str) -> str:
    """S6：双引号包裹，内部 `"` 转义成 `""`。"""
    if not isinstance(name, str) or not name.strip():
        raise StaticReject("空标识符不能进 SQL", "列名/表名是空的，没法拼进查询")
    return '"' + name.replace('"', '""') + '"'


def check_identifier(name: str, allowed, *, where: str = "identifier") -> str:
    """S6：列名/表名必须来自 schema 白名单（不接受任意字符串），通过则返回带引号的形态。"""
    allowed_map = {str(a).lower(): str(a) for a in (allowed or [])}
    low = str(name).lower()
    if allowed is not None and low not in allowed_map:
        _reject(
            where,
            f"{name!r} 不是本次 schema 里的标识符（允许：{sorted(allowed_map.values())}）",
            f"「{name}」在这一列清单里找不到，不能凭字符串拼进 SQL",
        )
    # 白名单校验通过后才拼引号；原样保留调用方写下的拼写（sqlite 标识符大小写不敏感）
    return quote_ident(str(name))


def literal(value) -> str:
    """SQL 字面量（字符串用单引号并把 `'` 写成 `''`；数字原样；布尔换 1/0）。"""
    import datetime as dt
    from decimal import Decimal

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):  # 只在调试路径上出现；SQL 里不许有浮点常量
        return format(Decimal(str(value)), "f")
    if isinstance(value, (dt.date, dt.datetime)):
        return "'" + value.isoformat() + "'"
    return "'" + str(value).replace("'", "''") + "'"


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


def _selftest() -> int:
    results: list = []

    def check(name, fn):
        try:
            results.append((True, name, fn() or ""))
        except AssertionError as exc:
            results.append((False, name, str(exc) or "断言不成立"))
        except Exception as exc:  # pragma: no cover
            results.append((False, name, f"{type(exc).__name__}: {exc}"))

    TABLES = ["Sheet1"]

    def gone(sql, *keywords):
        """拒绝即可，且报错里至少要命中 `keywords` 中的一个（多判据会按检查顺序先命中一个）。"""
        try:
            check_sql(sql, tables=TABLES)
        except StaticReject as exc:
            if keywords and not any(kw in exc.message for kw in keywords):
                raise AssertionError(
                    f"{sql!r} 的报错里没有 {list(keywords)} 中任何一个：{exc.message}"
                ) from None
            return exc
        raise AssertionError(f"应被拒绝：{sql!r}")

    def fine(sql, **kw):
        return check_sql(sql, tables=TABLES, **kw)

    # 1. S1 写操作
    def _s1():
        for sql, kw in [
            ("INSERT INTO Sheet1 VALUES (1)", ("写操作", "开头")),
            ("UPDATE Sheet1 SET region = 'x'", ("写操作", "开头")),
            ("DELETE FROM Sheet1", ("写操作", "开头")),
            ("DROP TABLE Sheet1", ("写操作", "开头")),
            ("CREATE TABLE t (a int)", ("写操作", "开头")),
            ("SELECT * FROM Sheet1; VACUUM", ("写操作", "不止一个语句")),
        ]:
            gone(sql, *kw)
        # 反例里的正例：REPLACE( 是函数，必须放行
        out = fine("SELECT REPLACE(region, '华', 'H') AS r FROM Sheet1")
        assert "Sheet1" in out["tables"], out
        # 没有括号的 REPLACE 是关键字，必须拒
        gone("SELECT replace FROM Sheet1", "写操作")
        # 字符串字面量里的关键字不算
        fine("SELECT region FROM Sheet1 WHERE status = 'DELETE'")
        return "6 条写操作被拒；REPLACE( 放行、裸 replace 被拒、字面量里的 DELETE 放行"

    check("§8.1 S1 写操作关键字（REPLACE( 唯一例外）", _s1)

    # 2. S2 事务/管理
    def _s2():
        for sql in (
            "PRAGMA query_only = 0",
            "BEGIN",
            "COMMIT",
            "SELECT * FROM Sheet1 WHERE a = 1 SAVEPOINT x",
            "ATTACH DATABASE 'x.db' AS y",
            "SELECT load_extension('x')",
        ):
            gone(sql, "事务", "开头", "副作用")
        return "PRAGMA / BEGIN / COMMIT / SAVEPOINT / ATTACH / load_extension 都被拒"

    check("§8.1 S2 事务/权限/管理语句", _s2)

    # 3. S3 语句形态
    def _s3():
        gone("EXPLAIN SELECT 1", "开头")
        gone("SELECT 1; SELECT 2", "语句")
        gone("SELECT 1 FROM Sheet1 -- 注释", "注释")
        gone("/* hi */ SELECT 1 FROM Sheet1", "注释")
        gone("SELECT region INTO t2 FROM Sheet1", "INTO")
        gone("SELECT 1 UNION INSERT INTO Sheet1 VALUES (1)", "UNION", "写操作")
        fine("SELECT 1;")
        fine("SELECT 1 UNION SELECT 2")
        fine("WITH x AS (SELECT 1 AS a) SELECT a FROM x")
        try:
            check_sql("WITH x AS (SELECT 1) DELETE FROM Sheet1", tables=TABLES)
        except StaticReject as exc:
            assert "写操作" in exc.message, exc.message
        else:
            raise AssertionError("WITH 里带写操作必须被拒（§8.1 S3）")
        return "开头/多语句/注释/INTO/UNION/CTE 六条判据各一条"

    check("§8.1 S3 语句形态：只许 SELECT/WITH、单语句、无注释、无 INTO、UNION 分段", _s3)

    # 4. S4 副作用 / 不确定
    def _s4():
        for sql in (
            "SELECT random() FROM Sheet1",
            "SELECT randomblob(8) FROM Sheet1",
            "SELECT date('now') FROM Sheet1",
            "SELECT total_changes() FROM Sheet1",
            "SELECT sum(changes()) FROM Sheet1",
        ):
            gone(sql, "副作用" if "'now'" not in sql else "now")
        fine("SELECT SUM(amount) FROM Sheet1")
        return "random()/randomblob()/date('now')/total_changes()/changes() 被拒；SUM(amount) 放行"

    check("§8.1 S4 副作用函数与 'now'（破坏「同问同答」的东西）", _s4)

    # 5. S5 表名白名单
    def _s5():
        gone("SELECT * FROM sqlite_master", "系统表")
        gone("SELECT * FROM Sheet2", "不在本次导入")
        out = fine("SELECT a FROM Sheet1 JOIN Sheet1 AS b ON b.a = Sheet1.a")
        assert out["tables"] == ["Sheet1", "Sheet1"], out
        # CTE 名不算表名
        fine("WITH t AS (SELECT 1 AS a) SELECT a FROM t")
        # 拿不到 schema 时（tables=None）跳过白名单，但 sqlite_ 前缀照拒
        check_sql("SELECT a FROM anything_at_all", tables=None)
        try:
            check_sql("SELECT * FROM sqlite_schema", tables=None)
        except StaticReject:
            pass
        else:
            raise AssertionError("tables=None 时 sqlite_ 前缀也必须拒")
        return "陌生表/sqlite_master 被拒；白名单内 JOIN 通过；CTE 名不误杀"

    check("§8.1 S5 表名白名单 + sqlite_* 一律拒", _s5)

    # 6. S6 标识符
    def _s6():
        assert quote_ident('region') == '"region"'
        assert quote_ident('a"b') == '"a""b"', quote_ident('a"b')
        assert check_identifier("region", ["Region", "amount"]) == '"region"'
        try:
            check_identifier("amount); DROP TABLE x; --", ["region"])
        except StaticReject:
            pass
        else:
            raise AssertionError("不在白名单的标识符必须拒")
        assert literal("华东") == "'华东'", literal("华东")
        assert literal("O'Brien") == "'O''Brien'", literal("O'Brien")
        from decimal import Decimal

        assert literal(Decimal("1.50")) == "1.50", literal(Decimal("1.50"))
        return "双引号包裹 + \" 转义 + 白名单 + 字面量转义（' → ''）"

    check("§8.1 S6 标识符注入：白名单 + 双引号 + 转义", _s6)

    # 7. 指标定义（表达式形态）也要过 S1/S2/S3/S4
    def _definition():
        ok = check_sql(
            "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
            tables=None,
            require_select=False,
        )
        assert set(ok["identifiers"]) == {"status", "amount"}, ok
        for bad in ("SUM(amount); DROP TABLE x", "SUM(amount) -- x", "SUM(random())"):
            try:
                check_sql(bad, tables=None, require_select=False)
            except StaticReject:
                continue
            raise AssertionError(f"指标定义里的危险形态没被拒：{bad}")
        return "聚合表达式通过（identifiers=status/amount）；带 ; / 注释 / random() 被拒"

    check("§8.1 V3 指标定义也过静态检查（不带 SELECT 要求）", _definition)

    # 8. T5：表达式标识符必须 ⊆ source.columns
    def _t5():
        check_identifiers("SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)", ["region", "status", "amount"])
        try:
            check_identifiers("SUM(amount)", ["region"])
        except StaticReject as exc:
            assert "amount" in exc.message, exc.message
        else:
            raise AssertionError("用了没声明的列必须拒（T5）")
        return "声明内通过；未声明的 amount 被拒"

    check("§4.3 T5 表达式只许引用 source.columns", _t5)

    ok = sum(1 for r in results if r[0])
    print("ask.static_check 自检（契约 §8.1 S1–S6）")
    for passed, name, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    print(f"结果: {ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


USAGE = """ask.static_check · SQL 只读静态检查（契约 §8.1 S1–S6）

  python -m ask.static_check --selftest         自检（S1–S6 正例反例各一条）
  python -m ask.static_check "SELECT ..."       检查单条语句（PASS / 拒绝）
"""


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass


def main(argv: list | None = None) -> int:
    _utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    if argv[0] == "--selftest":
        return _selftest()
    sql = " ".join(argv)
    try:
        info = check_sql(sql)
    except StaticReject as exc:
        import json

        print(json.dumps(exc.as_error_body(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    import json

    print("PASS")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
