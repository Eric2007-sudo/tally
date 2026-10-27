"""ask.schema · 表头行推断 + 列类型推断（契约 §2.4 列类型、§2.6 `source.header_row`、§5.1 D1 输入）

本层吃 `ask.ingest` 的原始网格，产出两样东西：

1. **schema 快照**：形状对齐契约 §5.1 D1 输入
   `[{"file","sha256","sheets":[{"name","header_row","header_row_guessed",
     "columns":[{"name","type","null_ratio","unique_count"}]}]}]`
   —— 一个字段都不多加（`digits` 这类下游要用的东西留在 `Table` 对象里，不进快照）。

2. **`Table`**：列类型 + `digits` + 数据行（带 `__rid`，规则见契约 §2.5）。

契约点名的两件事在这里落地：
- 列类型只能取 `int / decimal / text / date / bool / null` 六选一（§2.4）；
- 表头行推断不出时 `header_row` 必须写 `null`，并且必须记 `HEADER_ROW_GUESSED`（§2.13），
  **不许写 1 蒙过去**；表头行是推测出来的（跳过了标题行之类）同样记这条 caveat。

CLI：
    python -m ask.schema --dump PATH [PATH ...]   # 打印 schema 快照（JSON）
    python -m ask.schema --table PATH             # 打印带类型的表（含 __rid）
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .ingest import (
    FIXTURE_REL_PATH,
    FileData,
    IngestError,
    Row,
    Sheet,
    read_source,
)

# 表头行只在开头这么多行里找（再往下就是数据了，硬找只会把第一条数据当表头）
HEADER_SCAN_ROWS = 20

# 表头行判定阈值：低于这个分数宁可判「没有表头」，也不写 1 蒙过去（§2.13）
HEADER_SCORE_THRESHOLD = 2.0

COLUMN_TYPES = ("int", "decimal", "text", "date", "bool", "null")

_NUM_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_LEADING_ZERO_RE = re.compile(r"^[+-]?0\d")  # "007" / "01" 是文本，不是数
_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), True),
    (re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"), True),
    (re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$"), True),
    (re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$"), True),
    (re.compile(r"^(\d{4})-(\d{1,2})$"), False),  # 年月：当月 1 日
)


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def cell_kind(value) -> str:
    """单元格在「类型推断」眼里的类别：empty / bool / date / number / text。"""
    if _is_blank(value):
        return "empty"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (dt.date, dt.datetime)):
        return "date"
    if _as_decimal(value) is not None:
        return "number"
    return "text"


def _as_decimal(value):
    """→ Decimal 或 None。字符串只在「看起来就是一个数」时才转（§6.2：不用 float）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        s = value.strip()
        if not s or _LEADING_ZERO_RE.match(s) or not _NUM_RE.match(s):
            return None
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    return None


def _as_date(value):
    """→ `datetime.date` 或 None。"""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    for pattern, has_day in _DATE_PATTERNS:
        m = pattern.match(s)
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        day = int(m.group(3)) if has_day else 1
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None
    return None


def _scale(d: Decimal) -> int:
    exp = d.as_tuple().exponent
    if not isinstance(exp, int):  # NaN / Infinity 的指数是字符串
        return 0
    return max(0, -exp)


def _freeze(value) -> str:
    """唯一值计数用的稳定键（Decimal 归一化，日期转 ISO）。"""
    if isinstance(value, Decimal):
        return f"n:{value.normalize()}"
    if isinstance(value, dt.datetime):
        return f"d:{value.isoformat()}"
    if isinstance(value, dt.date):
        return f"d:{value.isoformat()}"
    if isinstance(value, bool):
        return f"b:{value}"
    return f"t:{value}"


def format_decimal_plain(value: Decimal) -> str:
    """定点形式，禁止科学计数法（§6.2）。"""
    text = format(value, "f")
    return text


def infer_column_type(values) -> tuple:
    """→ `(type, digits)`，`type` 只可能是 `COLUMN_TYPES` 之一（§2.4）。"""
    vals = [v for v in values if not _is_blank(v)]
    if not vals:
        return ("null", 0)
    if all(isinstance(v, bool) for v in vals):
        return ("bool", 0)
    decs = [_as_decimal(v) for v in vals]
    if all(d is not None for d in decs):
        if all(d == d.to_integral_value() for d in decs):
            return ("int", 0)  # int 的 digits 必须是 0（§2.4）
        return ("decimal", max(_scale(d) for d in decs))
    if all(_as_date(v) is not None for v in vals):
        return ("date", 0)
    return ("text", 0)


def normalize_value(value, column_type: str, digits: int):
    """把原始值按声明类型归一化；`None` 表示「没有」，不许变成 0 / ""（§2.13）。"""
    if _is_blank(value) or column_type == "null":
        return None
    if column_type == "int":
        d = _as_decimal(value)
        return None if d is None else int(d)
    if column_type == "decimal":
        d = _as_decimal(value)
        if d is None:
            return None
        exponent = Decimal(1).scaleb(-max(digits, 0))
        from decimal import ROUND_HALF_EVEN

        return d.quantize(exponent, rounding=ROUND_HALF_EVEN)
    if column_type == "date":
        return _as_date(value)
    if column_type == "bool":
        if isinstance(value, bool):
            return value
        d = _as_decimal(value)
        return None if d is None else bool(int(d))
    return str(value) if not isinstance(value, str) else value


# --------------------------------------------------------------------------- #
# 表头行
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HeaderGuess:
    row_no: int | None  # 物理行号（1 基）；推断不出就是 None
    guessed: bool  # True = 「猜的」，必须记 HEADER_ROW_GUESSED
    reason: str


def detect_header_row(sheet: Sheet) -> HeaderGuess:
    """在前 20 行里找最像表头的一行。

    判据（都是可解释的，不调参猜）：
    - 一行里非空单元格 ≥ 2 个（否则连两列都对不上）；
    - 该行以**文本**为主（表头几乎不会是纯数字）；
    - 该行的**下面几行**更「数值化」（数据行才长数字/日期）；
    - 名字互不重复（重复列名通常说明它不是表头）。
    分数不够就判「没有表头」→ `row_no=None`，宁可 null 也不写 1 蒙（§2.13）。
    """
    scored = []
    for pos, row in enumerate(sheet.rows[:HEADER_SCAN_ROWS]):
        kinds = [cell_kind(c) for c in row.cells]
        filled = [k for k in kinds if k != "empty"]
        if len(filled) < 2:
            continue
        text_ratio = filled.count("text") / len(filled)
        values_ratio = (
            filled.count("number") + filled.count("date") + filled.count("bool")
        ) / len(filled)
        names = [str(c).strip() for c, k in zip(row.cells, kinds) if k != "empty"]
        unique_ratio = len(set(names)) / len(names) if names else 0.0
        below = [r for r in sheet.rows[pos + 1: pos + 6] if not r.is_blank]
        below_values = 0.0
        if below:
            ks = [cell_kind(c) for r in below for c in r.cells]
            ks = [k for k in ks if k != "empty"]
            if ks:
                below_values = (ks.count("number") + ks.count("date")) / len(ks)
        score = (
            3.0 * text_ratio
            + 1.0 * min(len(filled), 8) / 8.0
            + 2.0 * below_values
            + 0.5 * unique_ratio
            - 2.0 * values_ratio
        )
        scored.append((score, pos, row.row_no))

    if not scored:
        return HeaderGuess(
            None,
            True,
            f"前 {HEADER_SCAN_ROWS} 行里没有一行像表头（非空单元格不足两个），header_row 记 null",
        )
    scored.sort(key=lambda item: (-item[0], item[1]))
    score, pos, row_no = scored[0]
    if score < HEADER_SCORE_THRESHOLD:
        return HeaderGuess(
            None,
            True,
            f"最像表头的是第 {row_no} 行，但分数 {score:.2f} < {HEADER_SCORE_THRESHOLD}，"
            "判为没有表头：header_row 记 null，列名靠位置生成",
        )
    if pos == 0:
        return HeaderGuess(row_no, False, "第 1 行是表头（全文本 + 下方以数值为主）")
    return HeaderGuess(
        row_no,
        True,
        f"表头在第 {row_no} 行（前面还有标题/空行），行号是推断的 → 记 HEADER_ROW_GUESSED",
    )


def header_names(row: Row | None, n_cols: int) -> tuple:
    """→ `(names, synthetic)`；空列名/重名列会补 `column_i` / `_2` 后缀并标记非原始。"""
    names = []
    synthetic = False
    seen: dict = {}
    for i in range(n_cols):
        raw = row.cells[i] if (row is not None and i < len(row.cells)) else None
        if isinstance(raw, Decimal):
            name = format_decimal_plain(raw)
        elif raw is None:
            name = ""
        elif isinstance(raw, (dt.date, dt.datetime)):
            name = raw.isoformat()
        else:
            name = str(raw)
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            name = f"column_{i + 1}"
            synthetic = True
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
            synthetic = True
        else:
            seen[name] = 1
        names.append(name)
    return names, synthetic


# --------------------------------------------------------------------------- #
# Table
# --------------------------------------------------------------------------- #


@dataclass
class Column:
    name: str
    type: str
    digits: int
    index: int  # 原始列序（1 基）
    null_ratio: float
    unique_count: int

    def as_schema_dict(self) -> dict:
        """契约 §5.1 D1 输入里的列形状，一个字段都不多加。"""
        return {
            "name": self.name,
            "type": self.type,
            "null_ratio": self.null_ratio,
            "unique_count": self.unique_count,
        }


@dataclass
class Table:
    file: str
    file_sha256: str
    sheet: str | None
    header_row: int | None
    header_row_guessed: bool
    header_reason: str
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # 每行 dict：列名 + `__row_no` + `__rid`
    caveats: list = field(default_factory=list)

    def column(self, name: str) -> Column:
        for col in self.columns:
            if col.name == name:
                return col
        raise IngestError("COLUMN_NOT_FOUND", f"列不存在: {name!r}", f"表里没有「{name}」这一列")

    def as_schema_dict(self) -> dict:
        return {
            "file": self.file,
            "sha256": self.file_sha256,
            "sheets": [
                {
                    "name": self.sheet,
                    # §2.6 source.header_row：推断不出时必须 null（同时由 header_row_guessed 触发 caveat）
                    "header_row": self.header_row,
                    "header_row_guessed": self.header_row_guessed,
                    "columns": [c.as_schema_dict() for c in self.columns],
                }
            ],
        }


def table_from_sheet(fd: FileData, sheet: Sheet) -> Table:
    guess = detect_header_row(sheet)
    header_row = guess.row_no
    data_rows = [
        r
        for r in sheet.rows
        if (header_row is None or r.row_no > header_row) and not r.is_blank
    ]
    n_cols = max([sheet.n_cols] + [len(r.cells) for r in data_rows])
    names, synthetic = header_names(sheet.row(header_row) if header_row else None, n_cols)

    columns = []
    for i, name in enumerate(names):
        values = [(r.cells[i] if i < len(r.cells) else None) for r in data_rows]
        ctype, digits = infer_column_type(values)
        filled = [v for v in values if not _is_blank(v)]
        null_ratio = 0.0 if not values else round((len(values) - len(filled)) / len(values), 4)
        columns.append(
            Column(
                name=name,
                type=ctype,
                digits=digits,
                index=i + 1,
                null_ratio=null_ratio,
                unique_count=len({_freeze(v) for v in filled}),
            )
        )

    rows = []
    for seq, r in enumerate(data_rows, start=1):
        row = {"__row_no": seq}
        for col in columns:
            raw = r.cells[col.index - 1] if col.index - 1 < len(r.cells) else None
            row[col.name] = normalize_value(raw, col.type, col.digits)
        row["__rid"] = fd.rid(sheet.name, seq)
        rows.append(row)

    caveats = []
    guessed_flag = guess.guessed or synthetic
    if guessed_flag:
        # §2.13：表头行是猜的 → 必须显式声明，不许藏
        reason = guess.reason + ("；有列名靠位置生成/去重" if synthetic else "")
        caveats.append(
            {
                "code": "HEADER_ROW_GUESSED",
                "message": f"表头行是推断的：{reason}",
                "affects": {"fields": [f"{fd.path}#{sheet.name or ''}:header_row"], "trace_ids": []},
            }
        )

    return Table(
        file=fd.path,
        file_sha256=fd.sha256,
        sheet=sheet.name,
        header_row=header_row,
        header_row_guessed=guessed_flag,
        header_reason=guess.reason + ("；列名有补全/去重" if synthetic else ""),
        columns=columns,
        rows=rows,
        caveats=caveats,
    )


def tables_for_file(fd: FileData) -> list:
    return [table_from_sheet(fd, sheet) for sheet in fd.sheets]


def open_source(path, root: str | Path | None = None) -> FileData:
    """读盘上的文件。**不做任何替换/兜底**：文件不在就报 FILE_NOT_FOUND，
    让断言红在真相上，而不是红在「我替你造了一份」。"""
    return read_source(path, root=root)


def build_schema(paths, root: str | Path | None = None) -> list:
    """→ 契约 §5.1 D1 输入里的 `schema` 数组（按 file 路径字节序）。"""
    out = []
    for path in sorted({str(p) for p in paths}, key=lambda s: s.encode("utf-8")):
        fd = open_source(path, root=root)
        for table in tables_for_file(fd):
            out.append(table.as_schema_dict())
    return out


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


def _write_text(path: Path, lines) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _selftest() -> int:
    """把表头行 / 列类型 / caveat / __rid / 快照形状这些判定规则钉成可复跑的断言。"""
    import tempfile

    results: list = []

    def check(name: str, fn) -> None:
        try:
            results.append((True, name, fn() or ""))
        except AssertionError as exc:
            results.append((False, name, str(exc) or "断言不成立"))
        except Exception as exc:  # pragma: no cover
            results.append((False, name, f"{type(exc).__name__}: {exc}"))

    with tempfile.TemporaryDirectory(prefix="tally-schema-") as tmp:
        tmpdir = Path(tmp)

        # ---- 0. 盘上的 smoke fixture：真读它（不造内存副本）----
        def _disk_fixture():
            p = Path(FIXTURE_REL_PATH)
            assert p.exists(), f"{FIXTURE_REL_PATH} 不存在（先跑 python -m ask.ingest --write-fixture）"
            snap = build_schema([FIXTURE_REL_PATH])[0]
            sheet = snap["sheets"][0]
            assert sheet["header_row"] == 1, f"header_row={sheet['header_row']}"
            assert sheet["header_row_guessed"] is False, "第 1 行表头不该标成猜的"
            types = {c["name"]: c["type"] for c in sheet["columns"]}
            assert types == {
                "region": "text",
                "status": "text",
                "amount": "decimal",
                "sale_date": "date",
                "qty": "int",
            }, f"类型={types}"
            return f"{FIXTURE_REL_PATH} → {types}"

        check(f"盘上 fixture: {FIXTURE_REL_PATH} 真读 + 五列类型推断", _disk_fixture)

        # ---- 1. 表头就在第 1 行：不记 caveat ----
        plain = _write_text(
            tmpdir / "plain.csv",
            ["region,amount,sale_date,qty", "华东,100.50,2025-01-05,3", "华南,200.00,2025-02-06,4"],
        )

        def _plain_header():
            fd = read_source(plain)
            table = tables_for_file(fd)[0]
            assert table.header_row == 1, f"header_row={table.header_row}"
            assert table.header_row_guessed is False, "第 1 行表头不该标成猜的"
            assert table.caveats == [], f"不该有 caveat: {table.caveats}"
            assert [c.name for c in table.columns] == ["region", "amount", "sale_date", "qty"]
            return f"header_row=1，无 caveat，列={[c.name for c in table.columns]}"

        check("表头在第 1 行: header_row=1 + 不记 HEADER_ROW_GUESSED", _plain_header)

        # ---- 2. 前面有标题行：表头是推断的 → 必须记 caveat ----
        titled = _write_text(
            tmpdir / "title.csv",
            ["2025年销售报表", "", "region,amount", "华东,100", "华南,200"],
        )

        def _titled():
            fd = read_source(titled)
            table = tables_for_file(fd)[0]
            assert table.header_row == 3, f"header_row={table.header_row}"
            assert table.header_row_guessed is True, "跳过标题行推出的表头必须标成猜的"
            codes = [c["code"] for c in table.caveats]
            assert codes == ["HEADER_ROW_GUESSED"], f"caveats={codes}"
            assert table.caveats[0]["message"], "caveat.message 不许为空（§2.9）"
            return f"header_row={table.header_row}，caveat={codes[0]}"

        check("标题行占位: 表头在第 3 行 → header_row=3 + HEADER_ROW_GUESSED", _titled)

        # ---- 3. 推断不出表头：header_row 必须 null，不许写 1 蒙（§2.13）----
        headerless = _write_text(tmpdir / "headerless.csv", ["1,2", "3,4", "5,6"])

        def _headerless():
            fd = read_source(headerless)
            table = tables_for_file(fd)[0]
            assert table.header_row is None, f"header_row={table.header_row!r}，必须是 None"
            assert table.header_row_guessed is True, "猜的表头行必须标出来"
            assert [c.name for c in table.columns] == ["column_1", "column_2"], [c.name for c in table.columns]
            assert "HEADER_ROW_GUESSED" in [c["code"] for c in table.caveats]
            assert len(table.rows) == 3, f"没有表头时 3 行全是数据，实际 {len(table.rows)}"
            return "header_row=null + 位置列名 column_1/column_2 + caveat"

        check("推断不出表头: header_row=null（不写 1 蒙）+ HEADER_ROW_GUESSED", _headerless)

        # ---- 4. 列类型六选一 + digits ----
        def _types():
            cases = [
                ((Decimal("1"), Decimal("2"), None), ("int", 0)),
                ((Decimal("1.50"), Decimal("2.25")), ("decimal", 2)),
                ((dt.date(2025, 1, 5), "2025-02-06", "2025年3月7日"), ("date", 0)),
                (("华东", "华南"), ("text", 0)),
                ((True, False, None), ("bool", 0)),
                ((None, "", None), ("null", 0)),
                (("007", "008"), ("text", 0)),  # 前导零是编号，不是数
                ((100, 200), ("int", 0)),
            ]
            for values, expect in cases:
                got = infer_column_type(values)
                assert got == expect, f"{values} → {got}，期望 {expect}"
                assert got[0] in COLUMN_TYPES, f"类型 {got[0]} 不在 §2.4 六选一里"
                if got[0] == "int":
                    assert got[1] == 0, "int 的 digits 必须是 0（§2.4）"
            return f"{len(cases)} 组类型判定全过（int/decimal/date/text/bool/null + 前导零）"

        check("列类型: 六选一 + digits（int 必为 0）+ 前导零判 text", _types)

        # ---- 5. int 不许写成 1.0；decimal 按 digits 定点 ----
        def _values():
            assert normalize_value(Decimal("42.0"), "int", 0) == 42, "int 列必须是 int，不是 Decimal"
            assert isinstance(normalize_value(Decimal("42.0"), "int", 0), int)
            assert str(normalize_value(Decimal("1.005"), "decimal", 2)) == "1.00", "ROUND_HALF_EVEN 2 位"
            assert normalize_value(None, "decimal", 2) is None, "缺值是 None，不是 0"
            assert normalize_value("", "text", 0) is None, "空串是「没有」，不是空文本"
            assert format_decimal_plain(Decimal("1E+3")) == "1000", "禁止科学计数法（§6.2）"
            return "int/decimal/None/空串/定点输出 各一条"

        check("值归一化: 缺值→null、int 不写成 1.0、定点无科学计数法（§2.13/§6.2）",
              _values)

        # ---- 6. schema 快照形状：一个字段都不多加 ----
        def _shape():
            snap = build_schema([str(plain)])
            assert isinstance(snap, list) and len(snap) == 1, f"{type(snap)}"
            item = snap[0]
            assert set(item) == {"file", "sha256", "sheets"}, f"顶层键多/少了: {set(item)}"
            sheet = item["sheets"][0]
            assert set(sheet) == {
                "name",
                "header_row",
                "header_row_guessed",
                "columns",
            }, f"sheet 键多/少了: {set(sheet)}"
            for col in sheet["columns"]:
                assert set(col) == {"name", "type", "null_ratio", "unique_count"}, f"列键: {set(col)}"
            assert len(item["sha256"]) == 64, "sha256 长度不对"
            return f"顶层={sorted(item)} sheets={sorted(sheet)} columns={sorted(sheet['columns'][0])}"

        check("快照形状: 对齐 §5.1 D1 输入，列只留 name/type/null_ratio/unique_count",
              _shape)

        # ---- 7. __rid 与数据行号（§2.5：源行号 1 基且指数据行，表头不计）----
        def _rid():
            fd = read_source(titled)
            table = tables_for_file(fd)[0]
            rids = [r["__rid"] for r in table.rows]
            assert rids == ["title.csv#title!1", "title.csv#title!2"], rids
            assert [r["__row_no"] for r in table.rows] == [1, 2], "数据行号必须从 1 起"
            return "，".join(rids)

        check("__rid: <basename>#<sheet>!<数据行号>，表头不计", _rid)

        # ---- 8. CSV 的 sheet 必须是 null（§2.13）----
        def _csv_sheet():
            snap = build_schema([str(plain)])[0]
            assert snap["sheets"][0]["name"] is None, f"CSV sheet={snap['sheets'][0]['name']!r}"
            return "sheets[0].name = null"

        check("CSV: sheet 写 null，不写 '' 也不写 'Sheet1'", _csv_sheet)

        # ---- 9. null_ratio 是真比例，不是 0 占位 ----
        def _null_ratio():
            p = _write_text(tmpdir / "holes.csv", ["a,b", "1,x", ",y", ",z"])
            table = tables_for_file(read_source(p))[0]
            col_a = table.column("a")
            assert col_a.null_ratio == round(2 / 3, 4), f"null_ratio={col_a.null_ratio}"
            assert col_a.unique_count == 1, f"unique_count={col_a.unique_count}"
            assert col_a.type == "int", f"type={col_a.type}（有空值不影响数值列判定）"
            return f"a: null_ratio={col_a.null_ratio} unique={col_a.unique_count} type={col_a.type}"

        check("null_ratio / unique_count: 空值算比例但不影响类型判定", _null_ratio)

    ok = sum(1 for r in results if r[0])
    print("ask.schema 自检")
    for passed, name, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    print(f"结果: {ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

USAGE = """ask.schema · 表头行推断 + 列类型推断

  python -m ask.schema --dump PATH [PATH ...]   打印 schema 快照（JSON，对齐契约 §5.1 D1 输入）
  python -m ask.schema --table PATH             打印带类型的表（含 __rid、digits）
  python -m ask.schema --selftest               自检表头行/列类型/caveat/__rid 判定规则
"""


def _utf8_stdio() -> None:
    """Windows 控制台默认 cp936，中文输出在重定向时可能炸；统一按 UTF-8 且不抛。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass


def _json_default(obj):
    if isinstance(obj, Decimal):
        return format_decimal_plain(obj)
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    raise TypeError(f"不能序列化: {type(obj).__name__}")


def main(argv: list | None = None) -> int:
    _utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    cmd = argv[0]
    try:
        if cmd == "--selftest":
            return _selftest()
        if cmd == "--dump":
            if len(argv) < 2:
                print(USAGE, file=sys.stderr)
                return 2
            print(json.dumps(build_schema(argv[1:]), ensure_ascii=False, indent=2))
            return 0
        if cmd == "--table":
            if len(argv) < 2:
                print(USAGE, file=sys.stderr)
                return 2
            out = []
            for path in argv[1:]:
                fd = open_source(path)
                for table in tables_for_file(fd):
                    out.append(
                        {
                            "file": table.file,
                            "sheet": table.sheet,
                            "header_row": table.header_row,
                            "header_row_guessed": table.header_row_guessed,
                            "header_reason": table.header_reason,
                            "columns": [
                                {
                                    "name": c.name,
                                    "type": c.type,
                                    "digits": c.digits,
                                    "null_ratio": c.null_ratio,
                                    "unique_count": c.unique_count,
                                }
                                for c in table.columns
                            ],
                            "rows": table.rows,
                            "caveats": table.caveats,
                        }
                    )
            print(json.dumps(out, ensure_ascii=False, indent=2, default=_json_default))
            return 0
    except IngestError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        if exc.readable_hint:
            print(f"  {exc.readable_hint}", file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
