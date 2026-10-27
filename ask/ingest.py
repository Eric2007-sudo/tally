"""ask.ingest · 文件接入层（契约 §1 功能 1、§8.3 文件侧、§2.5 源行号）

职责只有一件：把 `.xlsx` / `.csv` 读成**原始网格**（保留物理行号、保留单元格原始值），
不做表头推断、不做列类型推断——那两件事归 `ask.schema`。

硬约束（都来自契约，不在这里加戏）：
- `.xlsx` 用 zip + XML 解析，**不解压到磁盘**，不执行宏、不解析外部链接（§8.3）；
- 文件路径不许出现 `..`（目录穿越，§8.3）；
- 数值一律 `Decimal`（§6.2），不用二进制 float；
- CSV 的 sheet 语义为 `null`（§2.13），本层直接给 `Sheet.name = None`；
- 出错用契约 §2.11 的 `error.code` 枚举值，不发明错误码。

CLI：
    python -m ask.ingest --selftest [PATH]   # 自检；PATH 存在就真读它
    python -m ask.ingest --json PATH         # 打印接入摘要（sheet / 行数 / 列数 / sha256）
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .decide import CONTRACT_VERSION  # 契约版本只留一处真源（`ask.decide`）

# §8.3：单文件大小上限与行列上限必须显式配置，不许「不设上限」。
LIMITS = {
    "max_file_bytes": 32 * 1024 * 1024,
    "max_rows": 200_000,
    "max_columns": 512,
    "max_cell_chars": 32_768,
}

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_XLSX_SUFFIXES = {".xlsx", ".xlsm"}
_CSV_SUFFIXES = {".csv"}

# Excel 内置的日期/时间数字格式 id（含 14–22 与中文区的 27–36、45–47 常见值）
_BUILTIN_DATE_FMT_IDS = frozenset(
    list(range(14, 23)) + list(range(27, 37)) + [45, 46, 47, 50, 57]
)


class IngestError(Exception):
    """接入期错误；`code` 取自契约 §2.11 的 `error.code` 枚举（不新增）。"""

    def __init__(self, code: str, message: str, readable_hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.readable_hint = readable_hint

    def as_error_body(self, stage: str = "ingest") -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "readable_hint": self.readable_hint or self.message,
            "stage": stage,
        }

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class Row:
    """一行原始数据。`row_no` 是**物理行号**（1 基，含表头行）。"""

    row_no: int
    cells: tuple

    @property
    def is_blank(self) -> bool:
        return all(_is_blank_cell(c) for c in self.cells)


@dataclass
class Sheet:
    """一个工作表（CSV 视为只有一个 sheet，`name=None`）。"""

    name: str | None
    rows: list

    @property
    def n_cols(self) -> int:
        return max((len(r.cells) for r in self.rows), default=0)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    def row(self, row_no: int) -> Row | None:
        for r in self.rows:
            if r.row_no == row_no:
                return r
        return None


@dataclass
class FileData:
    """一个已接入文件。`path` 是相对项目根的正斜杠路径（§2.6 `source.file`）。"""

    path: str
    abs_path: str
    sha256: str
    kind: str  # "xlsx" | "csv"
    sheets: list

    def sheet(self, name: str | None) -> Sheet:
        for s in self.sheets:
            if s.name == name:
                return s
        raise IngestError(
            "SHEET_NOT_FOUND",
            f"工作表不存在: {name!r}（文件里有 {[s.name for s in self.sheets]}）",
            f"这个文件里没有「{name}」这张表",
        )

    @property
    def basename(self) -> str:
        """`__rid` 规则里的 `<文件basename>`（§2.5）：含扩展名。"""
        return self.path.rsplit("/", 1)[-1]

    def rid(self, sheet_name: str | None, data_row_no: int) -> str:
        """明细行 `__rid`：`<文件basename>#<sheet>!<源行号>`，源行号 1 基指数据行。"""
        tag = ""
        if self.kind == "csv":
            tag = self.basename.rsplit(".", 1)[0]
        else:
            tag = sheet_name or ""
        return f"{self.basename}#{tag}!{data_row_no}"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _is_blank_cell(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _to_decimal(raw: str):
    try:
        d = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return d


def _column_index(ref: str) -> int:
    """`C12` → 3（1 基列号）。"""
    letters = "".join(ch for ch in ref if ch.isalpha())
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - 64)
    return idx


def _serial_to_datetime(raw: str):
    """Excel 序列值 → date / datetime（1900 基准，减去 Excel 的闰年 bug）。"""
    serial = _to_decimal(raw)
    if serial is None:
        return None
    days = int(serial)
    frac = serial - days
    base = dt.datetime(1899, 12, 30)
    if frac == 0:
        return (base + dt.timedelta(days=days)).date()
    seconds = int((frac * Decimal(86400)).quantize(Decimal("1")))
    return base + dt.timedelta(days=days, seconds=seconds)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _guard_path(path: str | Path) -> Path:
    """§8.3：路径不许出现 `..`；只接受已存在的普通文件。"""
    raw = str(path)
    if ".." in Path(raw).parts or ".." in raw.replace("\\", "/").split("/"):
        raise IngestError(
            "FILE_PARSE_ERROR",
            f"拒绝目录穿越路径: {raw}",
            "这个路径不合法（含 `..`），不允许读项目外的文件",
        )
    p = Path(raw)
    if not p.exists():
        raise IngestError("FILE_NOT_FOUND", f"文件不存在: {raw}", "找不到这个数据文件")
    if not p.is_file():
        raise IngestError("FILE_PARSE_ERROR", f"不是普通文件: {raw}", "这个路径不是文件")
    size = p.stat().st_size
    if size > LIMITS["max_file_bytes"]:
        raise IngestError(
            "FILE_PARSE_ERROR",
            f"文件超上限: {size} > {LIMITS['max_file_bytes']} 字节",
            "文件太大，超出单文件上限",
        )
    return p


def _relative_posix(path: Path, root: Path | None) -> str:
    try:
        if root is not None:
            return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        pass
    return path.as_posix()


# --------------------------------------------------------------------------- #
# xlsx：zip + XML（不落盘）
# --------------------------------------------------------------------------- #


def _m(tag: str) -> str:
    return f"{_NS_MAIN}{tag}"


def _check_members(zf: zipfile.ZipFile) -> set:
    names = set(zf.namelist())
    for n in names:
        parts = Path(n.replace("\\", "/")).parts
        if n.startswith("/") or ".." in parts:
            raise IngestError(
                "FILE_PARSE_ERROR",
                f"xlsx 内含可疑成员路径: {n}",
                "这个 xlsx 内部结构可疑（可能被篡改），已拒绝读取",
            )
    return names


def _read_shared_strings(zf: zipfile.ZipFile, names: set) -> list:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = xml_root(zf.read("xl/sharedStrings.xml"))
    return [_si_text(si) for si in root.findall(_m("si"))]


def _si_text(si) -> str:
    """`<si>` → 文本。可能是单个 `<t>`，也可能是 `<r><t>` 富文本；`rPh` 是注音，必须排除。"""
    chunks = []
    for child in si:
        if child.tag == _m("rPh"):
            continue
        if child.tag == _m("t"):
            chunks.append(child.text or "")
        elif child.tag == _m("r"):
            for t in child.findall(_m("t")):
                chunks.append(t.text or "")
    return "".join(chunks)


def xml_root(data: bytes):
    from xml.etree import ElementTree as ET

    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:  # pragma: no cover - 坏文件
        raise IngestError(
            "FILE_PARSE_ERROR", f"XML 解析失败: {exc}", "文件内容损坏，读不出来"
        ) from exc


def _read_styles(zf: zipfile.ZipFile, names: set) -> list:
    """返回 `cellXfs` 下标 → 是否日期格式（用于把序列值转成日期）。"""
    if "xl/styles.xml" not in names:
        return []
    root = xml_root(zf.read("xl/styles.xml"))
    custom: dict = {}
    numfmts = root.find(_m("numFmts"))
    if numfmts is not None:
        for nf in numfmts.findall(_m("numFmt")):
            try:
                custom[int(nf.get("numFmtId", "-1"))] = nf.get("formatCode", "")
            except ValueError:
                continue
    flags = []
    cellxfs = root.find(_m("cellXfs"))
    if cellxfs is None:
        return flags
    for xf in cellxfs.findall(_m("xf")):
        try:
            fid = int(xf.get("numFmtId", "0"))
        except ValueError:
            fid = 0
        if fid in _BUILTIN_DATE_FMT_IDS:
            flags.append(True)
        elif fid in custom:
            flags.append(_is_date_format_code(custom[fid]))
        else:
            flags.append(False)
    return flags


def _is_date_format_code(code: str) -> bool:
    if not code:
        return False
    s = re.sub(r'"[^"]*"', "", code)  # 去掉引号里的字面量
    s = re.sub(r"\[[^\]]*\]", "", s)  # 去掉 [$-409] / [h] 这类修饰
    s = s.replace("\\", "")
    core = re.sub(r"[^A-Za-z]", "", s)
    if not core:
        return False
    low = core.lower()
    if "y" in low or "d" in low:
        return True
    # 只有 m 时：`mm:ss` 是时间、`m/d` 是日期，单独 `0.00` 里没有 m
    return "m" in low and any(ch in s for ch in "-/:.")


def _read_workbook_sheets(zf: zipfile.ZipFile, names: set) -> list:
    """→ [(sheet 名, zip 内路径)]，按工作簿里的声明顺序。"""
    if "xl/workbook.xml" not in names:
        raise IngestError("FILE_PARSE_ERROR", "缺少 xl/workbook.xml", "这个 xlsx 不完整")
    root = xml_root(zf.read("xl/workbook.xml"))
    rels: dict = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rroot = xml_root(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rroot.findall(f"{_NS_PKG}Relationship"):
            target = rel.get("Target", "")
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            rels[rel.get("Id")] = re.sub(r"/+", "/", target)
    out = []
    sheets_el = root.find(_m("sheets"))
    if sheets_el is None:
        raise IngestError("FILE_PARSE_ERROR", "workbook 里没有 sheets", "这个 xlsx 里没有工作表")
    for idx, sh in enumerate(sheets_el.findall(_m("sheet")), start=1):
        rid = sh.get(f"{_NS_REL}id")
        target = rels.get(rid) or f"xl/worksheets/sheet{idx}.xml"
        if target not in names:
            raise IngestError(
                "FILE_PARSE_ERROR",
                f"工作表 XML 缺失: {target}",
                "这个 xlsx 不完整（sheet 内容缺失）",
            )
        out.append((sh.get("name") or f"Sheet{idx}", target))
    return out


def _parse_sheet_xml(data: bytes, shared: list, date_flags: list, sheet_name: str) -> list:
    root = xml_root(data)
    rows = []
    seq = 0
    for row_el in root.iter(_m("row")):
        seq += 1
        try:
            row_no = int(row_el.get("r") or seq)
        except ValueError:
            row_no = seq
        cells: dict = {}
        col_seq = 0
        for c in row_el.iter(_m("c")):
            ref = c.get("r")
            col = _column_index(ref) if ref else col_seq + 1
            col_seq = col
            if col > LIMITS["max_columns"]:
                raise IngestError(
                    "FILE_PARSE_ERROR",
                    f"列数超上限: {col} > {LIMITS['max_columns']}",
                    "表格列数太多，超出上限",
                )
            ctype = c.get("t", "n")
            style = c.get("s")
            v_el = c.find(_m("v"))
            is_el = c.find(_m("is"))
            raw = v_el.text if v_el is not None else None

            if ctype == "s":
                try:
                    value = shared[int(raw)] if raw is not None else None
                except (ValueError, IndexError):
                    raise IngestError(
                        "FILE_PARSE_ERROR",
                        f"{sheet_name}!{ref} 引用了不存在的共享字符串 {raw!r}",
                        "文件里的文本索引坏了，读不出来",
                    )
            elif ctype == "inlineStr":
                value = "".join(t.text or "" for t in is_el.iter(_m("t"))) if is_el is not None else None
            elif ctype == "str":
                value = raw
            elif ctype == "b":
                value = raw == "1"
            elif ctype == "e":
                # 源文件里的 #DIV/0! 之类：这是「没有值」，不是 0（§2.13 的原则）
                value = None
            else:
                if raw is None:
                    value = None
                elif (
                    style is not None
                    and date_flags
                    and style.isdigit()
                    and int(style) < len(date_flags)
                    and date_flags[int(style)]
                ):
                    value = _serial_to_datetime(raw)
                else:
                    value = _to_decimal(raw)

            if isinstance(value, str) and len(value) > LIMITS["max_cell_chars"]:
                raise IngestError(
                    "FILE_PARSE_ERROR",
                    f"{sheet_name}!{ref} 文本超上限 {LIMITS['max_cell_chars']} 字符",
                    "某个单元格文本太长，超出上限",
                )
            cells[col] = value
        if not cells:
            rows.append(Row(row_no, ()))
        else:
            width = max(cells)
            rows.append(Row(row_no, tuple(cells.get(i) for i in range(1, width + 1))))
        if len(rows) > LIMITS["max_rows"]:
            raise IngestError(
                "FILE_PARSE_ERROR",
                f"行数超上限: > {LIMITS['max_rows']}",
                "表格行数太多，超出上限",
            )
    rows.sort(key=lambda r: r.row_no)
    return rows


def read_xlsx(path: Path) -> list:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise IngestError(
            "FILE_PARSE_ERROR", f"不是合法 xlsx（zip 打开失败）: {exc}", "这个文件不是有效的 xlsx"
        ) from exc
    with zf:
        names = _check_members(zf)
        shared = _read_shared_strings(zf, names)
        date_flags = _read_styles(zf, names)
        sheets = []
        for sheet_name, target in _read_workbook_sheets(zf, names):
            rows = _parse_sheet_xml(zf.read(target), shared, date_flags, sheet_name)
            sheets.append(Sheet(name=sheet_name, rows=rows))
    if not sheets:
        raise IngestError("SCHEMA_UNREADABLE", "读不出任何工作表", "这个文件里没有可读的表")
    return sheets


# --------------------------------------------------------------------------- #
# csv
# --------------------------------------------------------------------------- #


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_csv(path: Path) -> list:
    text = _decode_text(path.read_bytes())
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    rows = []
    for row_no, raw in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), start=1):
        cells = tuple(None if (c is None or c.strip() == "") else c for c in raw)
        while cells and cells[-1] is None:
            cells = cells[:-1]
        rows.append(Row(row_no, cells))
        if len(rows) > LIMITS["max_rows"]:
            raise IngestError(
                "FILE_PARSE_ERROR", f"行数超上限: > {LIMITS['max_rows']}", "表格行数太多，超出上限"
            )
    # §2.13：CSV 的 sheet 是 null，不许写 "" 或 "Sheet1"
    return [Sheet(name=None, rows=rows)]


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def read_source(path: str | Path, root: str | Path | None = None) -> FileData:
    """读一个文件 → `FileData`。`root` 给了就把 `path` 写成相对 root 的正斜杠路径。"""
    p = _guard_path(path)
    suffix = p.suffix.lower()
    if suffix in _XLSX_SUFFIXES:
        kind = "xlsx"
        sheets = read_xlsx(p)
    elif suffix in _CSV_SUFFIXES:
        kind = "csv"
        sheets = read_csv(p)
    else:
        raise IngestError(
            "FILE_PARSE_ERROR",
            f"不支持的文件类型: {suffix or '(无扩展名)'}",
            "只支持 .xlsx / .xlsm / .csv",
        )
    return FileData(
        path=_relative_posix(p, Path(root) if root is not None else None),
        abs_path=str(p.resolve()),
        sha256=sha256_file(p),
        kind=kind,
        sheets=sheets,
    )


def summary(fd: FileData) -> dict:
    return {
        "path": fd.path,
        "sha256": fd.sha256,
        "kind": fd.kind,
        "sheets": [
            {
                "name": s.name,
                "rows": s.n_rows,
                "columns": s.n_cols,
                "first_row_no": s.rows[0].row_no if s.rows else None,
                "last_row_no": s.rows[-1].row_no if s.rows else None,
                "blank_rows": sum(1 for r in s.rows if r.is_blank),
            }
            for s in fd.sheets
        ],
    }


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

_FIXTURE_HEADER = ["region", "status", "amount", "sale_date", "qty"]
_FIXTURE_ROWS = [
    ["华东", "paid", "1200.50", "2025-01-05", "3"],
    ["华东", "paid", "800.00", "2025-02-11", "1"],
    ["华南", "refund", "300.00", "2025-02-12", ""],
    ["华南", "paid", "450.25", "2025-03-01", "2"],
    ["", "", "", "", ""],
    ["华北", "paid", "60.00", "2025-03-02", "1"],
]

SMOKE_FIXTURE_NAME = "smoke.xlsx"
# 自检 fixture 的落盘位置：真文件，签进仓库（断言 `file:exists:ask/fixtures/smoke.xlsx`）
FIXTURE_REL_PATH = "ask/fixtures/smoke.xlsx"


def fixture_bytes() -> bytes:
    """smoke fixture 的字节（`ask/fixtures/smoke.xlsx` 就是这些字节，逐字节可复现）。"""
    return build_minimal_xlsx(_FIXTURE_HEADER, _FIXTURE_ROWS)


def write_fixture(path: str | Path = FIXTURE_REL_PATH) -> Path:
    """把 zip+XML 真的写成一个文件落盘（自检和 `--dump` 都读这个文件，不读内存）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(fixture_bytes())
    return p



def _col_letters(idx: int) -> str:
    out = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_minimal_xlsx(header: list, rows: list) -> bytes:
    """用标准库拼一个最小合法 xlsx（只含 1 张表 + 共享字符串 + 一个日期格式）。

    仅用于自检：证明「zip + XML 解析」这条路径真能跑通，且不引入任何第三方库。
    第 3 列（amount）写成数值，第 4 列（sale_date）写成日期格式的序列值。
    """
    shared: list = []
    index: dict = {}

    def sst(text: str) -> int:
        if text not in index:
            index[text] = len(shared)
            shared.append(text)
        return index[text]

    xml_rows = []
    all_rows = [header] + rows
    for r_i, row in enumerate(all_rows, start=1):
        cells = []
        for c_i, value in enumerate(row, start=1):
            if value == "" or value is None:
                continue
            ref = f"{_col_letters(c_i)}{r_i}"
            if 1 < r_i and c_i == 3:  # amount → 数值
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif 1 < r_i and c_i == 4:  # sale_date → 日期样式（s="1"）
                day = dt.date.fromisoformat(str(value))
                serial = (day - dt.date(1899, 12, 30)).days
                cells.append(f'<c r="{ref}" s="1"><v>{serial}</v></c>')
            elif 1 < r_i and c_i == 5:  # qty → 数值
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{sst(str(value))}</v></c>')
        xml_rows.append(f'<row r="{r_i}">{"".join(cells)}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS_MAIN[1:-1]}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )
    sst_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{_NS_MAIN[1:-1]}" count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{_escape(s)}</t></si>" for s in shared)
        + "</sst>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS_MAIN[1:-1]}" xmlns:r="{_NS_REL[1:-1]}">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_PKG[1:-1]}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{_NS_MAIN[1:-1]}">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        "<borders count=\"1\"><border><left/><right/><top/><bottom/><diagonal/></border></borders>"
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        "<cellXfs count=\"2\">"
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_PKG[1:-1]}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    # 固定时间戳 + 不压缩：同一份数据永远同一串字节（否则「同问同答」的第一步就漂了）
    stamp = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, text in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", root_rels),
            ("xl/workbook.xml", workbook_xml),
            ("xl/_rels/workbook.xml.rels", rels_xml),
            ("xl/sharedStrings.xml", sst_xml),
            ("xl/styles.xml", styles_xml),
            ("xl/worksheets/sheet1.xml", sheet_xml),
        ):
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.external_attr = 0o600 << 16
            zf.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


def _selftest(target: str | None = None) -> int:
    """自检。**只读盘上的真文件**：默认 `ask/fixtures/smoke.xlsx`，不做任何内存替换。"""
    target = str(target or FIXTURE_REL_PATH)
    fixture = Path(target)
    if not fixture.exists():
        print(
            f"FAIL 自检文件不存在: {target}\n"
            f"     先落盘: python -m ask.ingest --write-fixture {FIXTURE_REL_PATH}",
            file=sys.stderr,
        )
        return 1
    if not fixture.is_file():
        print(f"FAIL 自检目标不是文件: {target}", file=sys.stderr)
        return 1

    results: list = []

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            results.append((True, name, detail or ""))
        except AssertionError as exc:
            results.append((False, name, str(exc) or "断言不成立"))
        except IngestError as exc:
            results.append((False, name, f"{exc.code}: {exc.message}"))
        except Exception as exc:  # pragma: no cover
            results.append((False, name, f"{type(exc).__name__}: {exc}"))

    with tempfile.TemporaryDirectory(prefix="tally-ingest-") as tmp:
        tmpdir = Path(tmp)

        # ---- 0. 盘上的文件确实是「zip + XML」写出来的 xlsx，且字节可复现 ----
        def _on_disk():
            raw = fixture.read_bytes()
            assert raw[:2] == b"PK", "落盘文件不是 zip（xlsx 的头两个字节必须是 PK）"
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = set(zf.namelist())
                for need in (
                    "xl/workbook.xml",
                    "xl/worksheets/sheet1.xml",
                    "xl/sharedStrings.xml",
                    "xl/styles.xml",
                ):
                    assert need in names, f"zip 里缺少 {need}"
            assert raw == fixture_bytes(), "落盘字节与 write_fixture() 不一致（fixture 漂了，重新落盘）"
            return f"{fixture} {len(raw)} 字节 sha256={sha256_bytes(raw)[:12]}…"

        check(f"落盘 fixture: 真 zip+XML（PK 头 / 4 个 part）+ 字节可复现: {target}", _on_disk)

        # ---- 1. 解析盘上那一份：表头/共享字符串/数值/日期/空格 ----
        def _parse_fixture():
            fd = read_source(fixture)
            assert fd.kind == "xlsx", f"kind={fd.kind}"
            assert len(fd.sheets) == 1, f"sheet 数={len(fd.sheets)}"
            sh = fd.sheets[0]
            assert sh.name == "Sheet1", f"sheet 名={sh.name!r}"
            assert len(sh.rows) == len(_FIXTURE_ROWS) + 1, f"行数={len(sh.rows)}"
            head = sh.rows[0].cells
            assert list(head) == _FIXTURE_HEADER, f"表头={head}"
            # 共享字符串：文本是真字符串，不是索引数字
            assert head[0] == "region", f"表头首列={head[0]!r}"
            assert sh.rows[1].cells[0] == "华东", f"首行首列={sh.rows[1].cells[0]!r}"
            # 数值一律 Decimal（§6.2）
            amount = sh.rows[1].cells[2]
            assert isinstance(amount, Decimal) and amount == Decimal("1200.50"), f"amount={amount!r}"
            # 日期经 numFmt 认出并转成 date
            sale_date = sh.rows[1].cells[3]
            assert isinstance(sale_date, dt.date), f"sale_date={sale_date!r}"
            assert str(sale_date) == "2025-01-05", f"sale_date={sale_date}"
            # 空单元格是 None，不是 "" 也不是 0（§2.13）
            qty = sh.rows[3].cells[4] if len(sh.rows[3].cells) > 4 else None
            assert qty is None, f"空 qty={qty!r}"
            return f"{len(sh.rows)} 行 × {sh.n_cols} 列，amount={amount} sale_date={sale_date}"

        check("xlsx: zip+XML 解析 / sharedStrings / 数值 Decimal / 日期 numFmt / 空格 None", _parse_fixture)

        # ---- 2. sha256 与 __rid 的确定性 ----
        def _determinism():
            fd1 = read_source(fixture)
            fd2 = read_source(fixture)
            assert fd1.sha256 == fd2.sha256, "同一文件两次 sha256 不一致"
            assert sha256_file(fixture) == fd1.sha256, "sha256_file 与 read_source 不一致"
            rid1 = fd1.rid("Sheet1", 1)
            rid2 = fd2.rid("Sheet1", 1)
            assert rid1 == rid2 == "smoke.xlsx#Sheet1!1", f"__rid={rid1!r}"
            return f"sha256={fd1.sha256[:12]}… __rid={rid1}"

        check("确定性: sha256 稳定 + __rid 规则（<basename>#<sheet>!<数据行号>）", _determinism)

        # ---- 3. 物理行号保真（空行不吞行号）----
        def _row_numbers():
            fd = read_source(fixture)
            sh = fd.sheets[0]
            nums = [r.row_no for r in sh.rows]
            assert nums == list(range(1, len(_FIXTURE_ROWS) + 2)), f"物理行号={nums}"
            blank = [r.row_no for r in sh.rows if r.is_blank]
            assert blank == [1 + 5], f"全空行={blank}"
            return f"物理行号 {nums[0]}..{nums[-1]}，空行行号={blank}"

        check("物理行号保真: 全空行保留在网格里（否则追溯的行号会错位）", _row_numbers)

        # ---- 4. csv：sheet 必须是 null（§2.13）----
        csv_path = tmpdir / "smoke.csv"
        csv_path.write_bytes(
            "region,amount\n华东,100.50\n华南,200\n".encode("utf-8")
        )

        def _csv():
            fd = read_source(csv_path)
            assert fd.kind == "csv", f"kind={fd.kind}"
            sh = fd.sheets[0]
            assert sh.name is None, f"CSV sheet 必须是 None，实际 {sh.name!r}"
            assert sh.rows[0].cells == ("region", "amount"), f"表头={sh.rows[0].cells}"
            assert sh.rows[1].cells == ("华东", "100.50"), f"行={sh.rows[1].cells}"
            assert fd.rid(None, 1) == "smoke.csv#smoke!1", f"CSV __rid={fd.rid(None, 1)!r}"
            return f"sheet=None，__rid={fd.rid(None, 1)}"

        check("csv: sheet=None（§2.13）+ 扩展名去点做 __rid 标签", _csv)

        # ---- 5. 错误路径 ----
        def _errors():
            try:
                read_source(tmpdir / "nope.xlsx")
            except IngestError as exc:
                assert exc.code == "FILE_NOT_FOUND", f"code={exc.code}"
            else:
                raise AssertionError("缺文件没有报错")
            try:
                read_source("../escape.xlsx")
            except IngestError as exc:
                assert exc.code == "FILE_PARSE_ERROR", f"code={exc.code}"
            else:
                raise AssertionError("含 .. 的路径没有被拒")
            bad = tmpdir / "bad.xlsx"
            bad.write_bytes(b"not a zip")
            try:
                read_source(bad)
            except IngestError as exc:
                assert exc.code == "FILE_PARSE_ERROR", f"code={exc.code}"
            else:
                raise AssertionError("坏 zip 没有报错")
            unsupported = tmpdir / "x.txt"
            unsupported.write_text("hi", encoding="utf-8")
            try:
                read_source(unsupported)
            except IngestError as exc:
                assert exc.code == "FILE_PARSE_ERROR", f"code={exc.code}"
            else:
                raise AssertionError("不支持的扩展名没有报错")
            return "FILE_NOT_FOUND / 目录穿越 / 坏 zip / 不支持类型 各一条"

        check("错误路径: 错误码取自契约 §2.11 枚举，不新增", _errors)

        # ---- 6. 上限显式配置 ----
        def _limits():
            assert LIMITS["max_file_bytes"] > 0 and LIMITS["max_rows"] > 0, "上限必须显式配置"
            assert LIMITS["max_columns"] > 0 and LIMITS["max_cell_chars"] > 0, "上限必须显式配置"
            return ", ".join(f"{k}={v}" for k, v in LIMITS.items())

        check("§8.3: 单文件大小 / 行列 / 单元格长度上限显式配置", _limits)

        # ---- 7. 回写一份到临时目录，证明落盘可复现（不覆盖盘上那份）----
        def _rewrite():
            regen = write_fixture(tmpdir / "regen.xlsx")
            assert regen.read_bytes() == fixture.read_bytes(), "重新落盘的字节与盘上那份不一致"
            assert sha256_file(regen) == sha256_file(fixture), "sha256 不一致"
            return f"重写 {regen.name} 与盘上 {fixture.name} 字节一致"

        check("可复现落盘: write_fixture() 与 ask/fixtures/smoke.xlsx 字节相同", _rewrite)

    ok = sum(1 for r in results if r[0])
    print(f"ask.ingest 自检（读盘上的 {target}）")
    for passed, name, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    print(f"结果: {ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


USAGE = """ask.ingest · 文件接入（.xlsx / .csv）

  python -m ask.ingest --selftest [PATH]   自检（默认读盘上的 ask/fixtures/smoke.xlsx）
  python -m ask.ingest --write-fixture [PATH]  把 smoke fixture 真写成文件（默认 ask/fixtures/smoke.xlsx）
  python -m ask.ingest --json PATH         打印接入摘要（JSON）
"""


def _utf8_stdio() -> None:
    """Windows 控制台默认 cp936，中文输出在重定向时可能炸；统一按 UTF-8 且不抛。"""
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
    cmd = argv[0]
    try:
        if cmd == "--selftest":
            return _selftest(argv[1] if len(argv) > 1 else None)
        if cmd == "--write-fixture":
            p = write_fixture(argv[1] if len(argv) > 1 else FIXTURE_REL_PATH)
            size = p.stat().st_size
            print(f"已落盘 {p.as_posix()} （{size} 字节，sha256={sha256_file(p)[:12]}…）")
            return 0
        if cmd == "--json":
            if len(argv) < 2:
                print(USAGE, file=sys.stderr)
                return 2
            fd = read_source(argv[1])
            print(json.dumps(summary(fd), ensure_ascii=False, indent=2))
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
