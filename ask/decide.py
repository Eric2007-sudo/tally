"""ask.decide · 决策四段 D1–D4 + `--decide inject:` 注入 + 缓存键 + 中文别名表

契约出处（`contract/answer.md`）：
- §5.1 四个决策点的输入形状 / 输出形状 / 硬规则
- §5.2 `--decide dN=inject:PATH` 注入协议（`INJECT_SCHEMA_INVALID` / `INJECT_STALE` /
  `injected=true` / `meta.model.Dn.inject_path` 记相对路径）
- §5.3 缓存键（`cache_key` / `answer_id`）+ 提问归一化只做四件事
- §2.9 caveats 封闭集、§2.11 error.code 封闭集 + stage 枚举、§2.13 降级硬清单
- §7.1 反问形状（一次最多 3 个槽位，超了 `QUESTION_TOO_BROAD`）、§7.2 D3 兜底四处联动
- §6.2 浮点、§6.3 排序（caveats 按 code 再按 message 字节序）

**本模块只做决策层**：组装四个输入、跑模型或吃注入、校验输出、记 `meta.model.Dn`、
按 `--decide` 生成注入标记、算 `cache_key` / `answer_id`。
不生成 SQL、不执行、不拼 `answer.json`（那是 #4 的活；这里只交出已校验的决策输出）。

零依赖：只用标准库。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

# --------------------------------------------------------------------------- #
# 契约常量（名字与取值一律照抄契约，不新增）
# --------------------------------------------------------------------------- #

CONTRACT_VERSION = "1.1"

DECISIONS = ("D1", "D2", "D3", "D4")

INTENTS = ("distribution", "trend", "compare", "anomaly", "rank", "detail")
# §7.1 v1.1：删掉「数据集不唯一」（多表/多文件归 D3 的 joins[]），换成「时间落在哪一列」
AMBIGUITY_SLOTS = ("metric", "time_range", "time_column", "grain", "dimension", "filter")
CLAIM_KINDS = ("fact", "compare", "limit")
JOIN_TYPES = ("inner", "left")
PRUNE_STRATEGIES = ("minimal", "all")
# §4.3 T4 v1.1：`op` 白名单加两个空值判断
FILTER_OPS = ("=", "!=", ">", ">=", "<", "<=", "in", "like", "is_null", "is_not_null")
# §4.3 T4：这两个 op 的 `value_literal` 必须**显式**写 null
NULL_OPS = ("is_null", "is_not_null")

# §5.1 D1 v1.1：粒度枚举、两期对比的 kind 枚举、派生列名单（§2.4 封闭三种）
TIME_GRAINS = ("day", "week", "month", "quarter", "year")
COMPARISON_KINDS = ("period_over_period", "year_over_year")
PERIOD_COLUMN = "period"
DELTA_SUFFIX = "__delta"
PCT_SUFFIX = "__pct"
SOURCES = ("model", "inject", "fallback")

# §2.12 / §5.3：参与缓存键的选项，只有这三个
DEFAULT_OPTIONS = {"row_cap": 5000, "trace_row_id_cap": 200, "rows_preview_limit": 20}
OPTION_KEYS = ("row_cap", "trace_row_id_cap", "rows_preview_limit")

# §2.9 封闭集（新增必须升契约版本）
CAVEAT_CODES = frozenset(
    {
        "NOT_PRUNED",
        "METRIC_NOT_IN_DICT",
        "DICT_VERSION_DRIFT",
        "PLAN_MINIMAL",
        "NO_INTERPRETATION",
        "RESULT_EMPTY",
        "TRUNCATED_BY_ROW_CAP",
        "TRACE_ROW_IDS_TRUNCATED",
        "HEADER_ROW_GUESSED",
        "JOIN_ROW_MULTIPLIED",
        "ASSUMED_TIMEZONE",
        "COLUMN_ALIAS_FUZZY",
        "CLARIFY_TRUNCATED_SLOTS",
        # §2.9 v1.1：两期对比里基期为 0，变化率写 null（不许写 0% / ∞）
        "PCT_BASE_ZERO",
        # §2.9 v1.1：两期对比里某个键只在其中一期出现，缺失侧写 null（不许补 0）
        "COMPARISON_KEY_MISSING",
    }
)

# §5.2 要求「留 input_digest 为空时额外记 caveats 一条 INJECT_DIGEST_UNCHECKED」，
# 但 §2.9 的封闭集里没有这个 code。两处冲突，本模块按 §5.2 发这条 caveat（否则读注入的行为
# 无法在 answer.json 里被发现），同时把它单列在这里并进报告等契约升版——不自作主张塞进封闭集。
EXTRA_CAVEAT_CODES = {
    "INJECT_DIGEST_UNCHECKED": "§5.2 要求，§2.9 封闭集未收录；待契约升版本",
}

# §2.11 封闭集
ERROR_CODES = frozenset(
    {
        "FILE_NOT_FOUND",
        "FILE_PARSE_ERROR",
        "SHEET_NOT_FOUND",
        "SCHEMA_UNREADABLE",
        "COLUMN_NOT_FOUND",
        "METRIC_INVALID",
        "DICT_LOAD_FAILED",
        "SQL_STATIC_REJECT",
        "SQL_EXEC_ERROR",
        "SQL_RETRY_LOOP",
        "RESULT_NONFINITE",
        "ORDER_NOT_TOTAL",
        "CLARIFY_EXHAUSTED",
        "QUESTION_TOO_BROAD",
        "INJECT_STALE",
        "INJECT_SCHEMA_INVALID",
        "INTERNAL",
    }
)

# §2.11 error.stage 枚举
ERROR_STAGES = (
    "ingest",
    "D1",
    "D2",
    "D3",
    "static_check",
    "execute",
    "D4",
    "export",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALIASES_PATH = "ask/aliases.json"
DEFAULT_INJECT_DIR = "ask/inject"

MAX_CLARIFY_SLOTS = 3  # §7.1：一次最多问 3 个槽位
PLAN_TITLE_MAX = 40  # §2.2：title ≤40 字

# 问题里的时间词（§5.1 D1：time_range 为 null 时出现时间词必须进 ambiguity）
_TIME_WORD_RE = re.compile(
    r"\d{4}\s*年|今年|去年|前年|明年|本年|当年|"
    r"季度|Q[1-4]|上季度|下季度|"
    r"上个月|上月|本月|这个月|当月|每月|按月|"
    r"本周|这周|上周|最近|近\s*\d+|同期|同比|环比"
)
_YEAR_RE = re.compile(r"(\d{4})\s*年")

_PLACEHOLDER_RE = re.compile(r"\{\{\s*trace:(t-\d{4})\s*\}\}")
_TRACE_ID_RE = re.compile(r"^t-\d{4}$")
_DIGIT_RE = re.compile(r"[0-9０-９]")

# SQL 关键字/函数白名单（只用于 D3 覆盖检查时从指标定义里挑列名，不做 SQL 解析）
SQL_WORDS = frozenset(
    """
    select from where group by order having as and or not in is null case when then else end
    distinct on join left right inner outer union all limit offset asc desc sum avg count min max
    cast round coalesce ifnull nullif abs lower upper substr trim replace date datetime strftime
    julianday total length substr instr total_changes
    """.split()
)


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #


class DecisionError(Exception):
    """决策层错误；`code` / `stage` 一律取自契约 §2.11 的封闭枚举（不新增）。"""

    def __init__(self, code: str, message: str, *, stage: str, readable_hint: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"error.code 不在契约 §2.11 封闭集里: {code!r}")
        if stage not in ERROR_STAGES:
            raise ValueError(f"error.stage 不在契约 §2.11 枚举里: {stage!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.readable_hint = readable_hint or message

    def as_error_body(self) -> dict:
        """契约 §2.11 的 `error` 体；`attempts` / `partial` 由 #4 填。"""
        return {
            "code": self.code,
            "message": self.message,
            "readable_hint": self.readable_hint,
            "stage": self.stage,
        }

    def __str__(self) -> str:  # pragma: no cover - 只为报错好看
        return f"[{self.code}/{self.stage}] {self.message}"


class SchemaViolation(Exception):
    """决策输出不合 §5.1 形状。由调用方翻成 `INJECT_SCHEMA_INVALID` 或走兜底。"""

    def __init__(self, decision: str, message: str) -> None:
        super().__init__(f"{decision} 输出不合法：{message}")
        self.decision = decision
        self.message = message


# --------------------------------------------------------------------------- #
# JSON：规范化 / 哈希（§5.3）
# --------------------------------------------------------------------------- #


def _no_float(value, path: str = "$") -> None:
    """§5.3：缓存键里「不写浮点」。出现就报，不许偷偷 round。"""
    if isinstance(value, float):
        raise ValueError(f"缓存键里不许出现浮点（{path}）")
    if isinstance(value, dict):
        for k, v in value.items():
            _no_float(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _no_float(v, f"{path}[{i}]")


def canonical_json(obj) -> str:
    """§5.3 `canonical_json`：键按 UTF-8 码点升序、紧分隔、`ensure_ascii=False`、不许浮点。"""
    _no_float(obj)
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest_json(obj) -> str:
    """决策输入/输出的哈希用：同样规范化，但**允许**有限浮点（schema 里就有 null_ratio）。

    浮点只在哈希里出现，不进缓存键（缓存键只装 `output_sha256` 字符串）。`allow_nan=False`
    保证 NaN/Infinity 直接炸，不会算出个「稳定但没意义」的哈希。
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_canonical(obj) -> str:
    return sha256_text(canonical_json(obj))


def sha256_digest(obj) -> str:
    return sha256_text(digest_json(obj))


def input_digest(input_obj) -> str:
    """§5.2 `input_digest` = `sha256(规范化后的本次输入 JSON)`。"""
    return sha256_digest(input_obj)


def normalize_question(text: str) -> str:
    """§5.3 提问归一化**只做四件事**：NFC、去首尾空白、全角转半角、连续空白压成一个。

    不改大小写、不改标点、不换同义词——改了就变成「猜用户想问什么」。
    """
    if not isinstance(text, str):
        raise TypeError("question 必须是字符串（§2.1：逐字保存；归一化只发生在缓存键里）")
    s = unicodedata.normalize("NFC", text)
    s = "".join(_fw_to_hw(ch) for ch in s)
    s = s.strip()
    return re.sub(r"\s+", " ", s)


def _fw_to_hw(ch: str) -> str:
    code = ord(ch)
    if code == 0x3000:  # 全角空格
        return " "
    if 0xFF01 <= code <= 0xFF5E:  # ！～ 全角 ASCII
        return chr(code - 0xFEE0)
    return ch


# --------------------------------------------------------------------------- #
# 中文列别名表（README 功能 #3；契约 §2.9 的 COLUMN_ALIAS_FUZZY 落在这一层）
# --------------------------------------------------------------------------- #


def levenshtein(a: str, b: str) -> int:
    """编辑距离（标准 DP，单位：字符）。只用来判「模糊命中」，不用于排序打平。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@dataclass(frozen=True)
class AliasHit:
    """一次列名匹配的结果。`how` 取自别名表 `priority`：manual_alias / exact / fuzzy / none。"""

    term: str
    column: str | None
    how: str
    distance: int | None = None

    @property
    def fuzzy(self) -> bool:
        return self.how == "fuzzy"

    def caveat(self) -> dict | None:
        """§2.9：靠编辑距离匹配上的必须记 `COLUMN_ALIAS_FUZZY`；人工别名/精确匹配不记。"""
        if not self.fuzzy:
            return None
        return caveat(
            "COLUMN_ALIAS_FUZZY",
            f"「{self.term}」是按编辑距离（{self.distance}）匹配到列「{self.column}」的，不是人工别名",
            fields=[f"columns.{self.column}"],
        )


class AliasTable:
    """`ask/aliases.json` 的读取与匹配。数据格式是本模块与该 JSON 的内部约定（不进跨模块契约）。"""

    def __init__(self, columns: dict, fuzzy: dict | None = None, *, path: str = "") -> None:
        self.path = path
        self.columns = {str(k): [str(t) for t in v] for k, v in columns.items()}
        fz = dict(fuzzy or {})
        self.max_distance = int(fz.get("max_edit_distance", 1))
        self.min_term_len = int(fz.get("min_term_len", 2))
        self._index = {}
        for col, terms in self.columns.items():
            for term in terms:
                self._index.setdefault(_fold(term), set()).add(col)

    @classmethod
    def load(cls, path: str | Path | None = None, *, root: str | Path | None = None) -> "AliasTable":
        rel = str(path or DEFAULT_ALIASES_PATH)
        base = Path(root) if root is not None else PROJECT_ROOT
        target = Path(rel) if Path(rel).is_absolute() else base / rel
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DecisionError(
                "FILE_NOT_FOUND",
                f"别名表不存在: {rel}",
                stage="D1",
                readable_hint="中文列别名表缺失，中文列名匹配无法做人工别名优先",
            )
        except json.JSONDecodeError as exc:
            raise DecisionError("FILE_PARSE_ERROR", f"别名表不是合法 JSON: {exc}", stage="D1")
        if not isinstance(raw, dict) or not isinstance(raw.get("columns"), dict):
            raise DecisionError("SCHEMA_UNREADABLE", f"别名表结构不对（缺 columns 对象）: {rel}", stage="D1")
        return cls(raw["columns"], raw.get("fuzzy"), path=rel)

    # --- 校验 ------------------------------------------------------------- #

    def check(self) -> list:
        """→ 问题清单（空 = 通过）。同一词映射到两列会让中文匹配歧义，必须报（契约 V2 同一精神）。"""
        problems = []
        seen = {}
        for col, terms in sorted(self.columns.items()):
            if not terms:
                problems.append(f"列 {col!r} 没有别名")
            for term in terms:
                key = _fold(term)
                if key in seen and seen[key] != col:
                    problems.append(f"别名「{term}」同时指向 {seen[key]!r} 与 {col!r}（会歧义）")
                seen[key] = col
                if not term.strip():
                    problems.append(f"列 {col!r} 有空别名")
        if self.max_distance < 0:
            problems.append("fuzzy.max_edit_distance 不能为负")
        if self.min_term_len < 1:
            problems.append("fuzzy.min_term_len 至少为 1")
        return problems

    # --- 匹配 ------------------------------------------------------------- #

    def lookup(self, term: str) -> AliasHit:
        """只看别名表（不结合候选列）：`manual_alias` 命中或 `none`。"""
        cols = self._index.get(_fold(term))
        if cols and len(cols) == 1:
            return AliasHit(term, next(iter(cols)), "manual_alias")
        if cols:
            return AliasHit(term, None, "none")  # 一词对多列 = 歧义，不猜
        return AliasHit(term, None, "none")

    def match(self, term: str, candidates) -> AliasHit:
        """在候选列里匹配一个中文词。优先级：manual_alias > exact > fuzzy > none。"""
        cands = [str(c) for c in candidates]
        if not cands:
            return AliasHit(term, None, "none")
        key = _fold(term)

        hit = self.lookup(term)
        if hit.column is not None and hit.column in cands:
            return hit
        if hit.column is not None:
            return AliasHit(term, None, "none")  # 别名指向的列不在候选里：不跨表猜

        folded = {_fold(c): c for c in cands}
        if key in folded:
            return AliasHit(term, folded[key], "exact")

        if len(key) < self.min_term_len:
            return AliasHit(term, None, "none")
        best = []
        for cand in cands:
            dist = levenshtein(key, _fold(cand))
            if dist <= self.max_distance:
                best.append((dist, cand))
        if not best:
            return AliasHit(term, None, "none")
        best.sort()
        if len(best) > 1 and best[0][0] == best[1][0]:
            return AliasHit(term, None, "none")  # 并列就是歧义，不许挑一个凑数
        return AliasHit(term, best[0][1], "fuzzy", distance=best[0][0])


def _fold(text: str) -> str:
    """别名比较用的折叠：NFC + 全角转半角 + 去首尾/中间空白 + 小写。"""
    s = unicodedata.normalize("NFC", str(text))
    s = "".join(_fw_to_hw(ch) for ch in s)
    return re.sub(r"\s+", "", s).lower()


# --------------------------------------------------------------------------- #
# caveats（§2.9 元素形状 + §6.3 排序）
# --------------------------------------------------------------------------- #


def caveat(code: str, message: str, *, fields=None, trace_ids=None) -> dict:
    """§2.9 元素形状：`{code, message, affects:{fields, trace_ids}}`；message 不许为空。"""
    if code not in CAVEAT_CODES and code not in EXTRA_CAVEAT_CODES:
        raise ValueError(f"caveat.code 不在契约 §2.9 封闭集里: {code!r}")
    if not (message or "").strip():
        raise ValueError(f"caveat.message 不许为空（§2.9，code={code}）")
    return {
        "code": code,
        "message": message,
        "affects": {"fields": list(fields or []), "trace_ids": list(trace_ids or [])},
    }


def sort_caveats(items) -> list:
    """§6.3：按 `code` 字节序，同 code 按 `message` 字节序。"""
    return sorted(items, key=lambda c: (c["code"].encode("utf-8"), c["message"].encode("utf-8")))


def merge_caveats(*groups) -> list:
    out = []
    for group in groups:
        for item in group or []:
            if item not in out:
                out.append(item)
    return sort_caveats(out)


# --------------------------------------------------------------------------- #
# 决策输入（§5.1 明写的四个输入形状，照抄，不多加字段）
# --------------------------------------------------------------------------- #


def build_d1_input(question: str, schema, metric_dict, now: str, history=None) -> dict:
    return {
        "question": question,
        "now": now,
        "schema": schema,
        "metric_dict": [
            {k: m.get(k) for k in ("id", "name", "definition", "unit", "version")} for m in metric_dict
        ],
        "history": list(history or []),
    }


def build_d2_input(question: str, d1_output: dict, schema) -> dict:
    return {"question": question, "d1": d1_output, "schema": schema}


def build_d3_input(question: str, d1_output: dict, d2_output: dict, schema) -> dict:
    return {"question": question, "d1": d1_output, "d2": d2_output, "schema": schema}


def build_d4_input(question: str, d1_output: dict, d2_output: dict, table: dict, traces, options=None) -> dict:
    """§5.1 D4 输入。`rows_preview` 只放前 `rows_preview_limit` 行——整表不进 prompt。"""
    opts = normalize_options(options)
    rows = list((table or {}).get("rows") or [])
    return {
        "question": question,
        "d1": d1_output,
        "d2": d2_output,
        "table": {
            "columns": list((table or {}).get("columns") or []),
            "rows_preview": rows[: opts["rows_preview_limit"]],
        },
        "traces": list(traces or []),
    }


def normalize_options(options=None) -> dict:
    """§2.12/§5.3：`options` 只有三个键，值必须是整数；缺的按默认值，多的直接报。"""
    raw = dict(options or {})
    extra = set(raw) - set(OPTION_KEYS)
    if extra:
        raise ValueError(f"options 里有多余的键: {sorted(extra)}（§2.12 只定义 {OPTION_KEYS}）")
    out = {}
    for key in OPTION_KEYS:
        value = raw.get(key, DEFAULT_OPTIONS[key])
        if not _is_int(value) or value < 0:
            raise ValueError(f"options.{key} 必须是非负整数，拿到 {value!r}")
        out[key] = int(value)
    return out


# --------------------------------------------------------------------------- #
# 输出校验（§5.1 硬规则）
# --------------------------------------------------------------------------- #


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return _is_int(value) or isinstance(value, (float, Decimal))


def _obj(value, decision: str, what: str) -> None:
    if not isinstance(value, dict):
        raise SchemaViolation(decision, f"{what} 必须是对象，拿到 {type(value).__name__}")


# 每个子对象的「允许键」与「必填键」：值形状见下面的 validator
_ALLOWED = {
    # §5.1 D1 v1.1：四个新字段（listing / time_column / time_grain_grouping / comparison）全部必填
    "D1": (
        "metric_ids",
        "listing",
        "time_range",
        "time_column",
        "time_grain_grouping",
        "comparison",
        "dimensions",
        "filters",
        "ambiguity",
        "confidence",
    ),
    "D1.time_range": ("start", "end", "grain"),
    "D1.comparison": ("kind", "left", "right", "label_left", "label_right"),
    "D1.comparison.side": ("start", "end"),
    "D1.filter": ("column", "op", "value"),
    "D1.ambiguity": ("slot", "why", "candidate_ids"),
    "D2": ("steps", "stop_reason"),
    "D2.step": ("step_id", "intent", "title", "needs", "sql_hint", "depends_on", "fallback_of"),
    "D2.step.needs": ("metric_ids", "dimensions"),
    "D3": ("tables", "columns", "joins", "pruned", "prune_strategy", "token_estimate"),
    "D3.table": ("name", "reason"),
    # §5.1 D3 v1.1：派生列多三个键（period 用 `from`/`grain`，三种派生列都用 `derived`）
    "D3.column": ("table", "column", "reason", "derived", "from", "grain"),
    "D3.join": ("left", "right", "on", "type"),
    "D4": ("narrative", "no_claim_reason"),
    "D4.narrative": ("text_template", "evidence", "claim_kind"),
}

_REQUIRED = {
    "D1": _ALLOWED["D1"],
    "D1.time_range": _ALLOWED["D1.time_range"],
    "D1.comparison": ("kind", "left", "right"),
    "D1.comparison.side": _ALLOWED["D1.comparison.side"],
    "D1.filter": _ALLOWED["D1.filter"],
    "D1.ambiguity": _ALLOWED["D1.ambiguity"],
    "D2": _ALLOWED["D2"],
    "D2.step": ("step_id", "intent", "title", "needs"),
    "D2.step.needs": _ALLOWED["D2.step.needs"],
    "D3": _ALLOWED["D3"],
    "D3.table": _ALLOWED["D3.table"],
    "D3.column": ("table", "column", "reason"),
    "D3.join": _ALLOWED["D3.join"],
    "D4": _ALLOWED["D4"],
    "D4.narrative": _ALLOWED["D4.narrative"],
}


def _keys(value: dict, what: str, decision: str) -> None:
    """§5.2：注入输出「必须通过 5.1 的输出校验，失败即 INJECT_SCHEMA_INVALID，不做尽量修正」。

    所以这里按**封闭形状**判：多一个字段也报，并在报错里把允许的键列出来（好改）。
    """
    allowed = _ALLOWED[what]
    extra = [k for k in value if k not in allowed]
    if extra:
        raise SchemaViolation(decision, f"{what} 多了契约里没有的字段 {sorted(extra)}（允许：{list(allowed)}）")
    missing = [k for k in _REQUIRED[what] if k not in value]
    if missing:
        raise SchemaViolation(decision, f"{what} 缺必填字段 {missing}")


def _str_list(value, decision: str, what: str, *, non_empty: bool = False) -> list:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise SchemaViolation(decision, f"{what} 必须是字符串数组")
    if non_empty and not value:
        raise SchemaViolation(decision, f"{what} 不许为空数组")
    return value


def validate_d1(out, *, question: str | None = None) -> dict:
    """§5.1 D1 输出校验。`confidence` 只记录，不参与任何判定。"""
    _obj(out, "D1", "输出")
    if "needs_clarify" in out:
        # §5.1：没有独立的 needs_clarify 字段（两个字段一旦不一致，校验器不知道该信谁）
        raise SchemaViolation("D1", "不许出现 needs_clarify：ambiguity != [] ⟺ clarify（§5.1）")
    _keys(out, "D1", "D1")
    _str_list(out["metric_ids"], "D1", "metric_ids")
    _str_list(out["dimensions"], "D1", "dimensions")

    tr = out["time_range"]
    if tr is not None:
        _obj(tr, "D1", "time_range")
        _keys(tr, "D1.time_range", "D1")
        for key in ("start", "end"):
            if not isinstance(tr[key], str) or not tr[key].strip():
                raise SchemaViolation("D1", f"time_range.{key} 必须是非空字符串（或整个 time_range 为 null）")
        # v1.1：`time_grain_grouping` 非 null 时 `time_range.grain` 允许为 null（§5.1 D1）
        if tr["grain"] is not None and (not isinstance(tr["grain"], str) or not tr["grain"].strip()):
            raise SchemaViolation("D1", "time_range.grain 必须是非空字符串或 null")

    # ---- v1.1 四个新字段：全部必填，语义按 §5.1 D1 ----
    if not isinstance(out["listing"], bool):
        raise SchemaViolation("D1", "listing 必须是布尔（不许用 0/\"\" 冒充，§5.1 v1.1）")
    time_column = out["time_column"]
    if time_column is not None and (not isinstance(time_column, str) or not time_column.strip()):
        raise SchemaViolation("D1", "time_column 必须是非空字符串或 null")
    grain_grouping = out["time_grain_grouping"]
    if grain_grouping is not None and grain_grouping not in TIME_GRAINS:
        raise SchemaViolation("D1", f"time_grain_grouping={grain_grouping!r} 不在 {TIME_GRAINS}")

    comparison = out["comparison"]
    if comparison is not None:
        _obj(comparison, "D1", "comparison")
        _keys(comparison, "D1.comparison", "D1")
        if comparison["kind"] not in COMPARISON_KINDS:
            raise SchemaViolation("D1", f"comparison.kind={comparison['kind']!r} 不在 {COMPARISON_KINDS}")
        for side in ("left", "right"):
            _obj(comparison[side], "D1", f"comparison.{side}")
            _keys(comparison[side], "D1.comparison.side", "D1")
            for key in ("start", "end"):
                value = comparison[side][key]
                if not isinstance(value, str) or not value.strip():
                    raise SchemaViolation("D1", f"comparison.{side}.{key} 必须是非空字符串")
        for key in ("label_left", "label_right"):
            if key in comparison and (not isinstance(comparison[key], str) or not comparison[key].strip()):
                raise SchemaViolation("D1", f"comparison.{key} 必须是非空字符串")
        # §5.1：`time_range` 必须等于 `left`（它是主区间，变化率的除数也取自这一期）
        if (
            tr is None
            or tr.get("start") != comparison["left"]["start"]
            or tr.get("end") != comparison["left"]["end"]
        ):
            raise SchemaViolation("D1", "comparison 非 null 时 time_range 必须等于 comparison.left（§5.1）")

    if grain_grouping is not None and tr is not None and tr["grain"] not in (grain_grouping, None):
        raise SchemaViolation(
            "D1",
            f"time_grain_grouping={grain_grouping!r} 与 time_range.grain={tr['grain']!r} 不一致（§5.1 要求相等或 null）",
        )

    if out["listing"]:
        # §5.1：listing=true 是 `metric_ids=[]` 唯一合法出口，此时也不带分组维度
        if out["metric_ids"]:
            raise SchemaViolation("D1", "listing=true 时 metric_ids 必须是 []（纯明细列举不带指标口径）")
        if out["dimensions"]:
            raise SchemaViolation("D1", "listing=true 时 dimensions 必须是 []（明细行不分维度）")

    if not isinstance(out["filters"], list):
        raise SchemaViolation("D1", "filters 必须是数组")
    for i, flt in enumerate(out["filters"]):
        _obj(flt, "D1", f"filters[{i}]")
        _keys(flt, "D1.filter", "D1")
        op = flt["op"]
        if op not in FILTER_OPS:
            raise SchemaViolation("D1", f"filters[{i}].op={op!r} 不在 {FILTER_OPS}")
        # §4.3 T4 v1.1：空值判断的 value 必须显式 null；其余 op 的 value 不许是 null
        if op in NULL_OPS:
            if flt["value"] is not None:
                raise SchemaViolation("D1", f"filters[{i}].op={op!r} 时 value 必须显式为 null（§4.3 T4）")
        elif flt["value"] is None:
            raise SchemaViolation(
                "D1", f"filters[{i}].op={op!r} 的 value 不许为 null（要判空请用 is_null / is_not_null）"
            )

    if not isinstance(out["ambiguity"], list):
        raise SchemaViolation("D1", "ambiguity 必须是数组")
    for i, amb in enumerate(out["ambiguity"]):
        _obj(amb, "D1", f"ambiguity[{i}]")
        _keys(amb, "D1.ambiguity", "D1")
        if amb["slot"] not in AMBIGUITY_SLOTS:
            raise SchemaViolation("D1", f"ambiguity[{i}].slot={amb['slot']!r} 不在 {AMBIGUITY_SLOTS}")
        if not isinstance(amb["why"], str) or not amb["why"].strip():
            raise SchemaViolation("D1", f"ambiguity[{i}].why 必须是非空字符串")
        _str_list(amb["candidate_ids"], "D1", f"ambiguity[{i}].candidate_ids")

    if not _is_number(out["confidence"]):
        raise SchemaViolation("D1", "confidence 必须是数（只记录，不参与判定）")

    if (
        question is not None
        and out["time_range"] is None
        and out["comparison"] is None  # §7.1 v1.1：两期对比时时间词落在 comparison 里
        and _has_time_word(question)
        and not out["ambiguity"]
    ):
        raise SchemaViolation(
            "D1",
            f"问题里有时间词但 time_range 为 null，且 ambiguity 为空（§5.1 要求必须进 ambiguity）：{_time_words(question)}",
        )
    return out


def validate_d2(out, *, d1=None) -> dict:
    """§5.1 D2 输出校验：step_id 连续升序、depends_on 只引用更小的、不许凑数。"""
    _obj(out, "D2", "输出")
    _keys(out, "D2", "D2")
    steps = out["steps"]
    if not isinstance(steps, list) or not steps:
        raise SchemaViolation("D2", "steps 必须是非空数组（§5.1）")
    if out["stop_reason"] is not None and not isinstance(out["stop_reason"], str):
        raise SchemaViolation("D2", "stop_reason 必须是字符串或 null")

    ids = []
    for i, step in enumerate(steps):
        _obj(step, "D2", f"steps[{i}]")
        _keys(step, "D2.step", "D2")
        if not _is_int(step["step_id"]):
            raise SchemaViolation("D2", f"steps[{i}].step_id 必须是整数")
        if step["intent"] not in INTENTS:
            raise SchemaViolation("D2", f"steps[{i}].intent={step['intent']!r} 不在 {INTENTS}")
        title = step["title"]
        if not isinstance(title, str) or not title.strip():
            raise SchemaViolation("D2", f"steps[{i}].title 必须是非空字符串")
        if len(title) > PLAN_TITLE_MAX:
            raise SchemaViolation("D2", f"steps[{i}].title 超过 {PLAN_TITLE_MAX} 字：{title!r}")
        needs = step["needs"]
        _obj(needs, "D2", f"steps[{i}].needs")
        _keys(needs, "D2.step.needs", "D2")
        _str_list(needs["metric_ids"], "D2", f"steps[{i}].needs.metric_ids")
        _str_list(needs["dimensions"], "D2", f"steps[{i}].needs.dimensions")
        deps = step.get("depends_on", [])
        if not isinstance(deps, list) or any(not _is_int(d) for d in deps):
            raise SchemaViolation("D2", f"steps[{i}].depends_on 必须是整数数组")
        for dep in deps:
            if dep not in ids:
                raise SchemaViolation("D2", f"steps[{i}].depends_on 引用了不存在或不是更小的 step_id: {dep}")
        fb = step.get("fallback_of")
        if fb is not None and (not _is_int(fb) or fb not in ids):
            raise SchemaViolation("D2", f"steps[{i}].fallback_of 必须指向更小的 step_id 或 null")
        ids.append(step["step_id"])

    if ids != list(range(1, len(ids) + 1)):
        raise SchemaViolation("D2", f"step_id 必须从 1 连续升序，拿到 {ids}")

    for prev, cur in zip(steps, steps[1:]):
        if prev["intent"] == cur["intent"] and prev["needs"] == cur["needs"]:
            # §5.1：两个相邻同 intent 同 needs 的步骤算不合法（模型在凑数）
            raise SchemaViolation(
                "D2", f"steps {prev['step_id']} 与 {cur['step_id']} 的 intent + needs 完全相同（凑数，§5.1）"
            )

    if d1:
        # ---- §5.1 D2 v1.1 三条新硬规则 ----
        if d1.get("listing") is True:
            if len(steps) != 1:
                raise SchemaViolation("D2", f"listing=true 时计划必须恰好一步，拿到 {len(steps)} 步")
            only = steps[0]
            if only["intent"] != "detail":
                raise SchemaViolation("D2", f"listing=true 时那一步的 intent 必须是 detail，拿到 {only['intent']!r}")
            if only["needs"]["metric_ids"]:
                raise SchemaViolation("D2", "listing=true 时 needs.metric_ids 必须是 []（纯明细列举）")
        if d1.get("comparison"):
            final_intent = steps[-1]["intent"]
            if final_intent != "compare":
                raise SchemaViolation(
                    "D2", f"comparison 非 null 时取数步的 intent 必须是 compare，拿到 {final_intent!r}（§5.1 v1.1）"
                )
        grain_grouping = d1.get("time_grain_grouping")
        if grain_grouping:
            if PERIOD_COLUMN not in (steps[-1]["needs"]["dimensions"] or []):
                raise SchemaViolation(
                    "D2",
                    f"time_grain_grouping={grain_grouping!r} 时最终取数步的 needs.dimensions 必须含 {PERIOD_COLUMN!r}"
                    f"（§5.1 v1.1），拿到 {steps[-1]['needs']['dimensions']}",
                )
    return out


def validate_d3(out, *, d1=None, d2=None) -> dict:
    """§5.1 D3 输出形状校验（覆盖性/联动另有两个函数，见 `check_d3_coverage` / `check_d3_link`）。"""
    _obj(out, "D3", "输出")
    _keys(out, "D3", "D3")
    if not isinstance(out["tables"], list):
        raise SchemaViolation("D3", "tables 必须是数组")
    for i, tbl in enumerate(out["tables"]):
        _obj(tbl, "D3", f"tables[{i}]")
        _keys(tbl, "D3.table", "D3")
        if not isinstance(tbl["name"], str) or not tbl["name"].strip():
            raise SchemaViolation("D3", f"tables[{i}].name 必须是非空字符串")
        if not isinstance(tbl["reason"], str) or not tbl["reason"].strip():
            raise SchemaViolation("D3", f"tables[{i}].reason 必须是非空字符串")
    if not isinstance(out["columns"], list) or not out["columns"]:
        raise SchemaViolation("D3", "columns 必须是非空数组（这层的意义就是「选了哪些列」）")
    for i, col in enumerate(out["columns"]):
        _obj(col, "D3", f"columns[{i}]")
        _keys(col, "D3.column", "D3")
        for key in ("table", "column", "reason"):
            if not isinstance(col[key], str) or not col[key].strip():
                raise SchemaViolation("D3", f"columns[{i}].{key} 必须是非空字符串")
        # §2.4 v1.1：派生列是封闭的三种（period / <metric>__delta / <metric>__pct）
        derived = col.get("derived", False)
        if not isinstance(derived, bool):
            raise SchemaViolation("D3", f"columns[{i}].derived 必须是布尔")
        if derived:
            name = col["column"]
            if name == PERIOD_COLUMN:
                if not isinstance(col.get("from"), str) or not col["from"].strip():
                    raise SchemaViolation("D3", f"columns[{i}]（period）必须写 from=<原始时间列>")
                if col.get("grain") not in TIME_GRAINS:
                    raise SchemaViolation("D3", f"columns[{i}]（period）的 grain 必须是 {TIME_GRAINS} 之一")
            elif name.endswith(DELTA_SUFFIX) or name.endswith(PCT_SUFFIX):
                pass
            else:
                raise SchemaViolation(
                    "D3",
                    f"columns[{i}].column={name!r} 是派生列，但派生列只有三种（period / <指标>__delta / <指标>__pct）",
                )
        else:
            extra = [key for key in ("from", "grain") if key in col]
            if extra:
                raise SchemaViolation("D3", f"columns[{i}] 不是派生列，不该有 {extra}（§2.4）")
    if not isinstance(out["joins"], list):
        raise SchemaViolation("D3", "joins 必须是数组")
    for i, join in enumerate(out["joins"]):
        _obj(join, "D3", f"joins[{i}]")
        _keys(join, "D3.join", "D3")
        if join["type"] not in JOIN_TYPES:
            raise SchemaViolation("D3", f"joins[{i}].type={join['type']!r} 只许 {JOIN_TYPES}")
        if not isinstance(join["on"], list) or not join["on"]:
            raise SchemaViolation("D3", f"joins[{i}].on 必须是非空数组")
        for pair in join["on"]:
            if not isinstance(pair, list) or len(pair) != 2:
                raise SchemaViolation("D3", f"joins[{i}].on 的每项必须是 [左列, 右列]")
    if not isinstance(out["pruned"], bool):
        raise SchemaViolation("D3", "pruned 必须是布尔（不许用 '' / 0 冒充）")
    if out["prune_strategy"] not in PRUNE_STRATEGIES:
        raise SchemaViolation("D3", f"prune_strategy={out['prune_strategy']!r} 不在 {PRUNE_STRATEGIES}")
    if out["pruned"] is False and out["prune_strategy"] != "all":
        # §5.1：pruned=false ⟹ prune_strategy="all"
        raise SchemaViolation("D3", "pruned=false 时 prune_strategy 必须是 \"all\"")
    if not _is_int(out["token_estimate"]) or out["token_estimate"] < 0:
        raise SchemaViolation("D3", "token_estimate 必须是非负整数（只是估计值，不许当预算依据）")
    return out


def validate_d4(out, *, traces=None) -> dict:
    """§5.1 D4 输出校验：证据闭合、占位符外不许出现裸阿拉伯数字、compare 至少两个证据。"""
    _obj(out, "D4", "输出")
    _keys(out, "D4", "D4")
    items = out["narrative"]
    if not isinstance(items, list):
        raise SchemaViolation("D4", "narrative 必须是数组")
    if out["no_claim_reason"] is not None and not isinstance(out["no_claim_reason"], str):
        raise SchemaViolation("D4", "no_claim_reason 必须是字符串或 null")

    known = None
    if traces is not None:
        known = {t["trace_id"] for t in traces if isinstance(t, dict) and isinstance(t.get("trace_id"), str)}

    for i, item in enumerate(items):
        _obj(item, "D4", f"narrative[{i}]")
        _keys(item, "D4.narrative", "D4")
        text = item["text_template"]
        if not isinstance(text, str) or not text.strip():
            raise SchemaViolation("D4", f"narrative[{i}].text_template 必须是非空字符串")
        evidence = _str_list(item["evidence"], "D4", f"narrative[{i}].evidence", non_empty=True)
        if item["claim_kind"] not in CLAIM_KINDS:
            raise SchemaViolation("D4", f"narrative[{i}].claim_kind={item['claim_kind']!r} 不在 {CLAIM_KINDS}")
        refs = _PLACEHOLDER_RE.findall(text)
        unknown_fmt = re.findall(r"\{\{[^}]*\}\}", text)
        for raw in unknown_fmt:
            if not _PLACEHOLDER_RE.fullmatch(raw):
                raise SchemaViolation("D4", f"narrative[{i}] 的占位符写法不合法：{raw!r}（应为 {{{{trace:t-0001}}}}）")
        missing = [r for r in refs if r not in evidence]
        if missing:
            raise SchemaViolation("D4", f"narrative[{i}] 里的 {missing} 没写进 evidence（§5.1）")
        if known is not None:
            ghost = [e for e in evidence if e not in known]
            if ghost:
                raise SchemaViolation("D4", f"narrative[{i}].evidence 里的 {ghost} 在输入 traces 里不存在")
        bare = _DIGIT_RE.search(_PLACEHOLDER_RE.sub("", text))
        if bare:
            raise SchemaViolation(
                "D4", f"narrative[{i}].text_template 里有未被 {{{{trace:...}}}} 包裹的阿拉伯数字：{text!r}"
            )
        if item["claim_kind"] == "compare" and len(evidence) < 2:
            raise SchemaViolation("D4", f"narrative[{i}] 是 compare，evidence 必须 ≥2 条（「比谁多」要两个数）")

    if not items:
        reason = out["no_claim_reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaViolation("D4", "narrative 为空时 no_claim_reason 必须非空（§5.1）")
    return out


def validate_output(decision: str, out, **ctx) -> dict:
    """按决策点分派校验。`ctx` 里带上输入侧上下文（question / traces / d1 / d2）以便跨字段校验。"""
    decision = decision.upper()
    if decision == "D1":
        return validate_d1(out, question=ctx.get("question"))
    if decision == "D2":
        return validate_d2(out, d1=ctx.get("d1"))
    if decision == "D3":
        return validate_d3(out, d1=ctx.get("d1"), d2=ctx.get("d2"))
    if decision == "D4":
        return validate_d4(out, traces=ctx.get("traces"))
    raise ValueError(f"未知决策点: {decision!r}")


# --------------------------------------------------------------------------- #
# 覆盖性 / 联动检查（§5.1 D3 硬规则、§7.2 四处联动）
# --------------------------------------------------------------------------- #


def schema_columns(schema) -> list:
    """schema 快照里出现过的全部列名（快照没有表名，见 `check_d3_coverage` 的说明）。"""
    names = []
    for entry in schema or []:
        for sheet in entry.get("sheets") or []:
            for col in sheet.get("columns") or []:
                name = col.get("name")
                if isinstance(name, str) and name not in names:
                    names.append(name)
    return names


def definition_identifiers(expr: str) -> list:
    """从指标定义里挑出可能的列名（去掉字符串字面量与 SQL 关键字/函数）。不做 SQL 解析。"""
    text = re.sub(r"'(?:[^']|'')*'", " ", str(expr or ""))
    text = re.sub(r'"(?:[^"]|"")*"', " ", text)
    return [tok for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text) if tok.lower() not in SQL_WORDS]


def check_d3_coverage(d3_out: dict, d2_out: dict, schema, metric_dict=None) -> None:
    """§5.1：D3 选出的列必须覆盖 D2 的所有维度与所有指标定义引用到的列，少一列报 `COLUMN_NOT_FOUND`。

    注意：契约 §5.1 的 schema 快照只到「file → sheet → columns」，**没有表名**，所以这里能校的是
    「列名存在 + 维度被覆盖」；表名与表列的对应留给 #4 生成 SQL 时校（不在这里另造命名约定）。
    """
    available = set(schema_columns(schema))
    chosen = {c["column"] for c in d3_out.get("columns") or []}
    # §5.1 D3 v1.1：派生列（period / <指标>__delta / <指标>__pct）不受「必须在 schema 里存在」约束
    derived = {c["column"] for c in d3_out.get("columns") or [] if c.get("derived") is True}
    if available:
        ghost = sorted(col for col in chosen - derived if col not in available)
        if ghost:
            raise DecisionError(
                "COLUMN_NOT_FOUND",
                f"D3 选了 schema 里没有的列: {ghost}",
                stage="D3",
                readable_hint=f"这份数据里没有 {ghost}；可选的列有 {sorted(available)}",
            )
    need = []
    for step in (d2_out or {}).get("steps") or []:
        for dim in (step.get("needs") or {}).get("dimensions") or []:
            if dim not in need:
                need.append(dim)
    by_id = {m.get("id"): m for m in (metric_dict or [])}
    for step in (d2_out or {}).get("steps") or []:
        for mid in (step.get("needs") or {}).get("metric_ids") or []:
            metric = by_id.get(mid)
            if metric is None:
                continue  # 不在字典里由 D1 的 METRIC_NOT_IN_DICT + 反问处理，不在这里猜
            for ident in definition_identifiers(metric.get("definition")):
                if ident not in need:
                    need.append(ident)
    missing = [n for n in need if n not in chosen]
    if missing:
        raise DecisionError(
            "COLUMN_NOT_FOUND",
            f"D3 选列没覆盖 D2 需要的列: {missing}（已选 {sorted(chosen)}）",
            stage="D3",
            readable_hint=f"这些列是算下去必须的，但没被选进来: {missing}",
        )


def check_d3_link(d3_out: dict, *, source: str, caveats, plan_summary: str | None = None) -> None:
    """§5.1 / §7.2：`pruned=false` ⟹ `prune_strategy="all"` ⟹ caveats 含 NOT_PRUNED ⟹ source=fallback。

    四处必须同时成立；这里只判，不改——改（补齐 caveat / 改 source）由 `DecisionRunner` 做。
    """
    if d3_out.get("pruned") is not False:
        return
    codes = [c.get("code") for c in caveats or []]
    problems = []
    if d3_out.get("prune_strategy") != "all":
        problems.append('prune_strategy 不是 "all"')
    if "NOT_PRUNED" not in codes:
        problems.append("caveats 缺 NOT_PRUNED")
    if source != "fallback":
        problems.append(f'meta.model.D3.source 是 {source!r}，应为 "fallback"')
    message = "；".join(
        c.get("message", "") for c in caveats or [] if c.get("code") == "NOT_PRUNED"
    )
    if "未精简" not in (message + (plan_summary or "")):
        problems.append("NOT_PRUNED 的 message（或 plan_summary）里没有「未精简」三个字")
    if problems:
        raise DecisionError(
            "INTERNAL",
            f"D3 兜底四处联动不成立：{'；'.join(problems)}（§7.2）",
            stage="D3",
            readable_hint="D3 退化为全列时必须显式标注「未精简」，不能悄悄变宽松",
        )


# --------------------------------------------------------------------------- #
# 注入（§5.2）
# --------------------------------------------------------------------------- #

_INJECT_KEYS = ("decision", "contract_version", "input_digest", "output")


def load_inject(
    path: str | Path,
    decision: str,
    input_obj=None,
    *,
    root: str | Path | None = None,
    allow_unchecked_digest: bool = False,
) -> tuple:
    """读注入文件 → `(output, 相对路径, digest_checked)`。

    §5.2 的每条硬规则都在这里落地：
    - `decision` 与参数里的 dN 不一致 → `INJECT_SCHEMA_INVALID`
    - `contract_version` 不是当前版本 → `INJECT_SCHEMA_INVALID`（不跨版本复用）
    - `input_digest` 有值但不等于当前输入 → `INJECT_STALE`（不许「差不多就用」）
    - `input_digest` 为空/不写 → 允许，但 `digest_checked=False`（调用方要记
      `INJECT_DIGEST_UNCHECKED`）
    - `output` 不在这里校验：形状校验要按决策点带上下文，交给 `validate_output`
    """
    decision = decision.upper()
    if decision not in DECISIONS:
        raise ValueError(f"未知决策点: {decision!r}")
    base = Path(root) if root is not None else PROJECT_ROOT
    target = Path(path)
    rel = _rel_posix(target, base)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DecisionError(
            "INJECT_SCHEMA_INVALID",
            f"注入文件不存在: {rel}",
            stage=decision,
            readable_hint="注入路径写错了，或文件还没生成",
        )
    except json.JSONDecodeError as exc:
        raise DecisionError("INJECT_SCHEMA_INVALID", f"注入文件不是合法 JSON: {rel} / {exc}", stage=decision)

    if not isinstance(raw, dict):
        raise DecisionError("INJECT_SCHEMA_INVALID", f"注入文件顶层必须是对象: {rel}", stage=decision)
    extra = set(raw) - set(_INJECT_KEYS)
    if extra:
        raise DecisionError(
            "INJECT_SCHEMA_INVALID",
            f"注入文件多了字段 {sorted(extra)}（§5.2 只定义 {list(_INJECT_KEYS)}）: {rel}",
            stage=decision,
        )
    if raw.get("decision") != decision:
        raise DecisionError(
            "INJECT_SCHEMA_INVALID",
            f"注入文件里的 decision={raw.get('decision')!r} 与参数 {decision} 不一致: {rel}",
            stage=decision,
        )
    if raw.get("contract_version") != CONTRACT_VERSION:
        raise DecisionError(
            "INJECT_SCHEMA_INVALID",
            f"注入文件 contract_version={raw.get('contract_version')!r}，当前契约是 {CONTRACT_VERSION}: {rel}",
            stage=decision,
        )
    if "output" not in raw:
        raise DecisionError("INJECT_SCHEMA_INVALID", f"注入文件缺 output: {rel}", stage=decision)

    digest = raw.get("input_digest")
    checked = False
    if digest is not None and str(digest).strip() != "":
        if not isinstance(digest, str) or len(digest) != 64:
            raise DecisionError(
                "INJECT_SCHEMA_INVALID",
                f"input_digest 必须是 64 位十六进制字符串（或留空）: {rel}",
                stage=decision,
            )
        if input_obj is None:
            if not allow_unchecked_digest:
                raise DecisionError(
                    "INJECT_SCHEMA_INVALID",
                    f"注入文件写了 input_digest，但没有提供本次输入，无法校验: {rel}",
                    stage=decision,
                    readable_hint="要么带上本次输入重新算 digest，要么把 input_digest 清空（自己承担输入会变的后果）",
                )
        elif digest != input_digest(input_obj):
            raise DecisionError(
                "INJECT_STALE",
                f"注入文件的 input_digest 与本次输入不符: {rel}",
                stage=decision,
                readable_hint="这份注入是按另一次输入写的，结果会答非所问；要么重写注入，要么换掉这次输入",
            )
        else:
            checked = True
    return raw["output"], rel, checked


def _rel_posix(path: Path, base: Path) -> str:
    """§5.2：`inject_path` 记**相对路径**（不记绝对路径，否则换机器缓存键就变）。"""
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def parse_decide_args(items) -> dict:
    """解析 `--decide d1=inject:inject/d1.json`（可只给子集）→ `{"D1": "inject/d1.json"}`。

    只认 `inject:` 一种来源；其它写法（含 `d1=model`）一律当参数错误（退出码 2），
    因为契约 §5.2 只定义了这一种注入写法，别的约定不许我现造。
    """
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--decide 参数格式应为 dN=inject:PATH，拿到 {item!r}")
        left, right = item.split("=", 1)
        name = left.strip().upper()
        if name not in DECISIONS:
            raise ValueError(f"--decide 的决策点只能是 {DECISIONS}，拿到 {left!r}")
        if not right.startswith("inject:"):
            raise ValueError(f"--decide 只支持 inject:PATH（§5.2），拿到 {right!r}")
        path = right[len("inject:") :].strip()
        if not path:
            raise ValueError(f"--decide {name} 的注入路径为空")
        if name in out:
            raise ValueError(f"--decide {name} 给了两次")
        out[name] = path
    return out


# --------------------------------------------------------------------------- #
# 缓存键（§5.3）
# --------------------------------------------------------------------------- #


def cache_key(
    *,
    question: str,
    files,
    sheets,
    metric_dict_sha256: str,
    decisions: dict,
    options=None,
    contract_version: str = CONTRACT_VERSION,
) -> str:
    """§5.3 `cache_key`。不含：绝对路径、用户名、日志级别、时间戳、elapsed_ms、模型名、命中状态。"""
    if set(decisions) != set(DECISIONS):
        raise ValueError(f"decisions 必须正好是 {DECISIONS}，拿到 {sorted(decisions)}")
    decision_payload = {}
    for name in DECISIONS:
        entry = decisions[name]
        source = entry.get("source")
        if source not in SOURCES:
            raise ValueError(f"decisions.{name}.source 只能是 {SOURCES}，拿到 {source!r}")
        sha = entry.get("output_sha256")
        if not isinstance(sha, str) or not sha:
            raise ValueError(f"decisions.{name}.output_sha256 必须是非空字符串")
        decision_payload[name] = {"source": source, "output_sha256": sha}

    file_items = [{"path": str(f["path"]), "sha256": str(f["sha256"])} for f in files or []]
    file_items.sort(key=lambda item: item["path"].encode("utf-8"))
    sheet_names = sorted({str(s) for s in sheets or []}, key=lambda s: s.encode("utf-8"))
    payload = {
        "contract_version": contract_version,
        "question": normalize_question(question),
        "files": file_items,
        "sheets": sheet_names,
        "metric_dict_sha256": metric_dict_sha256,
        "decisions": decision_payload,
        "options": normalize_options(options),
    }
    return sha256_canonical(payload)


def answer_id(key: str) -> str:
    """§2.1：`answer_id` = `sha256(cache_key)` 前 16 位十六进制。"""
    if not re.fullmatch(r"[0-9a-f]{64}", key or ""):
        raise ValueError(f"cache_key 必须是 64 位小写十六进制，拿到 {key!r}")
    return sha256_text(key)[:16]


# --------------------------------------------------------------------------- #
# 决策运行器（模型 / 注入 / 兜底）
# --------------------------------------------------------------------------- #


@dataclass
class DecisionResult:
    """一个决策点的运行结果：输出 + 来源 + 哈希 + caveats（+ 注入路径）。"""

    decision: str
    output: dict
    source: str  # model / inject / fallback
    model: str | None
    injected: bool
    latency_ms: int
    output_sha256: str
    inject_path: str | None = None
    caveats: list = field(default_factory=list)

    def meta_entry(self) -> dict:
        """§2.12 `meta.model.Dn`。`inject_path` 是 §5.2 要求的附加字段（只对注入来源出现）。"""
        entry = {
            "source": self.source,
            "model": self.model,
            "injected": self.injected,
            "latency_ms": int(self.latency_ms),
            "output_sha256": self.output_sha256,
        }
        if self.inject_path is not None:
            entry["inject_path"] = self.inject_path
        return entry

    def key_entry(self) -> dict:
        """§5.3：`decisions` 里每条只留 `{source, output_sha256}`。"""
        return {"source": self.source, "output_sha256": self.output_sha256}


class DecisionRunner:
    """跑 D1–D4：先看注入，再看模型，最后按契约兜底（D1 没有兜底，缺就报）。

    `model` 是 `callable(decision, input_obj) -> dict`；P0 离线时给 `None`，
    只靠注入模板（`ask/inject/d4.json` 这类）演示可复现路径。
    """

    def __init__(self, *, injects=None, model=None, model_name: str | None = None, options=None,
                 root: str | Path | None = None) -> None:
        if model is not None and not (model_name or "").strip():
            raise ValueError("给了 model 就必须给 model_name：meta.model.Dn.model 不许用一个编的名字")
        self.injects = {str(k).upper(): str(v) for k, v in (injects or {}).items()}
        unknown = set(self.injects) - set(DECISIONS)
        if unknown:
            raise ValueError(f"注入点只能是 {DECISIONS}，拿到 {sorted(unknown)}")
        self.model = model
        self.model_name = model_name
        self.options = normalize_options(options)
        self.root = Path(root) if root is not None else PROJECT_ROOT
        self.caveats = []
        self.results = {}

    # --- 单个决策点 -------------------------------------------------------- #

    def run(self, decision: str, input_obj, **ctx) -> DecisionResult:
        decision = decision.upper()
        if decision not in DECISIONS:
            raise ValueError(f"未知决策点: {decision!r}")
        result = self._run_inject(decision, input_obj, ctx) or self._run_model(decision, input_obj, ctx)
        result.caveats = merge_caveats(result.caveats, ctx.get("caveats"))
        if decision == "D3":
            result = self._enforce_d3_link(result)
        self.results[decision] = result
        return result

    def _run_inject(self, decision: str, input_obj, ctx) -> DecisionResult | None:
        if decision not in self.injects:
            return None
        output, rel, checked = load_inject(self.injects[decision], decision, input_obj, root=self.root)
        _validate_or_raise_inject(decision, output, ctx)
        caveats = []
        if not checked:
            # §5.2：input_digest 为空 = 自己承担输入会变的后果，但必须留痕
            caveats.append(
                caveat(
                    "INJECT_DIGEST_UNCHECKED",
                    f"{decision} 的注入文件 {rel} 没写 input_digest：本次输出没有和输入对过账",
                    fields=[f"meta.model.{decision}"],
                )
            )
        return DecisionResult(
            decision=decision,
            output=output,
            source="inject",
            model=None,
            injected=True,
            latency_ms=0,
            output_sha256=sha256_digest(output),
            inject_path=rel,
            caveats=caveats,
        )

    def _run_model(self, decision: str, input_obj, ctx) -> DecisionResult:
        started = time.perf_counter()
        output = None
        failure = None
        if self.model is not None:
            try:
                output = self.model(decision, input_obj)
            except Exception as exc:  # 模型侧任何异常都按「不可用」处理，但要说清原因
                failure = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - started) * 1000)

        if output is not None:
            try:
                validate_output(decision, output, **ctx)
            except SchemaViolation as exc:
                if decision == "D1":
                    # 契约没有给 D1 定义兜底（§5.1 只有 D2/D3/D4 有），这里不许自己造一个
                    raise DecisionError(
                        "INTERNAL",
                        f"D1 输出不合法，且契约未定义 D1 兜底：{exc.message}",
                        stage="D1",
                        readable_hint="问题理解需要重跑模型或改用 --decide d1=inject:…，不能拿半个理解继续算",
                    )
                return self._fallback(decision, reason=str(exc), **ctx)
            return DecisionResult(
                decision=decision,
                output=output,
                source="model",
                model=self.model_name,
                injected=False,
                latency_ms=latency_ms,
                output_sha256=sha256_digest(output),
            )
        reason = "模型不可用（没有配置 model 回调）" if self.model is None else f"模型不可用：{failure}"
        return self._fallback(decision, reason=reason, **ctx)

    def _fallback(self, decision: str, *, reason: str, **ctx) -> DecisionResult:
        """§5.1 的兜底：D2 最小计划、D3 全列未精简、D4 只给数不说话；D1 没有兜底。"""
        if decision == "D1":
            raise DecisionError(
                "INTERNAL",
                f"D1 没有兜底（§5.1），当前不可用：{reason}",
                stage="D1",
                readable_hint="问题理解这一步不能猜：请配模型，或用 --decide d1=inject:… 固定一份输出",
            )
        if decision == "D2":
            output = fallback_plan(ctx.get("d1"))
            return DecisionResult(
                decision, output, "fallback", None, False, 0, sha256_digest(output),
                caveats=[
                    caveat(
                        "PLAN_MINIMAL",
                        f"退化为最小计划（单步直接取数）：{reason}",
                        fields=["plan", "plan_summary"],
                    )
                ],
            )
        if decision == "D3":
            output = fallback_d3(
                ctx.get("schema"),
                reason=reason,
                d1=ctx.get("d1"),
                imported=ctx.get("tables"),
                pool=ctx.get("pool"),
                metric_dict=ctx.get("metric_dict"),
                question=ctx.get("question") or "",
            )
            return DecisionResult(
                decision, output, "fallback", None, False, 0, sha256_digest(output),
                caveats=[
                    caveat(
                        "NOT_PRUNED",
                        f"选列未精简（D3 兜底为全列），可能答非所问：{reason}",
                        fields=["columns"],
                    )
                ],
            )
        if decision == "D4":
            output = fallback_d4(reason=reason)
            return DecisionResult(
                decision, output, "fallback", None, False, 0, sha256_digest(output),
                caveats=[
                    caveat(
                        "NO_INTERPRETATION",
                        f"只给数、不说话：{reason}",
                        fields=["narrative"],
                    )
                ],
            )
        raise ValueError(f"未知决策点: {decision!r}")

    def _enforce_d3_link(self, result: DecisionResult) -> DecisionResult:
        """§5.1/§7.2：模型给的 D3 若 `pruned=false`，那就已经是「兜底」，四处必须一起改。"""
        if result.output.get("pruned") is False:
            result.source = "fallback"
            result.model = None
            if not any(c["code"] == "NOT_PRUNED" for c in result.caveats):
                result.caveats = merge_caveats(
                    result.caveats,
                    [
                        caveat(
                            "NOT_PRUNED",
                            "选列未精简（D3 输出 pruned=false），可能答非所问",
                            fields=["columns"],
                        )
                    ],
                )
        check_d3_link(result.output, source=result.source, caveats=result.caveats)
        return result

    # --- 汇总 ------------------------------------------------------------- #

    def meta_model(self) -> dict:
        """§2.12 `meta.model`：四个决策点各一条（跑过哪些就有哪些）。"""
        return {name: self.results[name].meta_entry() for name in DECISIONS if name in self.results}

    def decisions_key_part(self) -> dict:
        return {name: self.results[name].key_entry() for name in DECISIONS if name in self.results}

    def all_caveats(self) -> list:
        return merge_caveats(*[r.caveats for r in self.results.values()])


def _validate_or_raise_inject(decision: str, output, ctx) -> None:
    try:
        validate_output(decision, output, **ctx)
    except SchemaViolation as exc:
        # §5.2：output 校验失败即 INJECT_SCHEMA_INVALID，不做「尽量修正」
        raise DecisionError(
            "INJECT_SCHEMA_INVALID",
            str(exc),
            stage=decision,
            readable_hint="注入文件里的 output 不合契约 §5.1，改注入文件，不要指望程序替你兜",
        )


def fallback_plan(d1=None) -> dict:
    """§5.1 D2 兜底 = 最小计划（单步、intent=detail、title=「按问题直接取数」）。

    契约只写死了这三样（`intent`/`title`/单步），`needs` 未规定；这里按标题的字面意思
    「按问题直接取数」把它落到 D1 已经确定的口径上：`metric_ids`/`dimensions` 原样抄 D1。
    不抄的话，兜底计划会丢掉「按 gmv 分组求和」这个已经理解好的口径，问题问聚合却答明细——
    那才是真的答非所问。这里的 `intent` 仍按契约留 `detail`：契约 §5.1 D2 的示例里，
    「按区域汇总成交额」这一步本身就是 `intent="detail"` + `needs.metric_ids=["gmv"]`。
    """
    d1 = d1 or {}
    dimensions = list(d1.get("dimensions") or [])
    if d1.get("time_grain_grouping") and PERIOD_COLUMN not in dimensions:
        # §5.1 D2 v1.1：按时间粒度分组时最终步的 needs.dimensions 必须含派生列 "period"
        dimensions.append(PERIOD_COLUMN)
    # §5.1 D2 v1.1：`d1.comparison != null` ⟹ 取数步的 intent 必须是 compare。
    # 兜底计划也守这条：留 `detail` 的话，这份兜底本身就过不了 §5.1 的校验。
    intent = "compare" if d1.get("comparison") else "detail"
    return {
        "steps": [
            {
                "step_id": 1,
                "intent": intent,
                "title": "按问题直接取数",
                "needs": {
                    "metric_ids": list(d1.get("metric_ids") or []),
                    "dimensions": dimensions,
                },
                "sql_hint": None,
                "depends_on": [],
                "fallback_of": None,
            }
        ],
        "stop_reason": "退化为最小计划：模型不可用或输出不合法",
    }


def fallback_d3(
    schema=None,
    *,
    reason: str = "模型不可用或输出不合法",
    d1=None,
    imported=None,
    pool=None,
    metric_dict=None,
    question: str = "",
) -> dict:
    """§5.1 D3 兜底 = 全列（`pruned=false` + `prune_strategy="all"`）。

    v1.1：`d1.time_grain_grouping` 非 null 时，兜底也要按 §5.1 D3 的硬规则补上派生列 `period`
    （它不在 schema 里，但 `from` 指向的时间列必须在）。

    #12：给了 `imported`（本次导入的 Table 对象）时，顺带把跨表列解析的结果写进 `joins[]`
    ——契约把「选表 + JOIN」的责任放在 D3，兜底也不能只留一个空数组。推导规则与
    `ask.sqlgen` 生成 SQL 时用的是同一套（`ask.joins.plan_source`），不会两处各说一套。
    """
    joins: list = []
    tables = []
    columns = []
    for entry in schema or []:
        for sheet in entry.get("sheets") or []:
            table = sheet.get("name") or Path(str(entry.get("file", ""))).stem
            if not any(t["name"] == table for t in tables):
                tables.append({"name": table, "reason": f"兜底：未精简，整表纳入（{reason}）"})
            for col in sheet.get("columns") or []:
                columns.append(
                    {"table": table, "column": col.get("name"), "reason": "兜底：未精简，全列纳入"}
                )
    d1 = d1 or {}
    grain = d1.get("time_grain_grouping")
    if grain:
        time_column = d1.get("time_column") or _single_date_column(schema)
        holder = _table_of_column(schema, time_column)
        if time_column and holder:
            columns.append(
                {
                    "table": holder,
                    "column": PERIOD_COLUMN,
                    "derived": True,
                    "from": time_column,
                    "grain": grain,
                    "reason": f"D1 要求按 {grain} 分组（派生列，源列 {time_column}）",
                }
            )
    if d1.get("comparison"):
        # §2.4 v1.1：两期对比要多两列派生列（差值 / 变化率），兜底也得把它们纳入
        holder = _table_of_column(schema, d1.get("time_column")) or (tables[0]["name"] if tables else None)
        if holder:
            for mid in d1.get("metric_ids") or []:
                for name, why in ((f"{mid}{DELTA_SUFFIX}", "两期差值"), (f"{mid}{PCT_SUFFIX}", "两期变化率")):
                    columns.append(
                        {
                            "table": holder,
                            "column": name,
                            "derived": True,
                            "reason": f"D1 要求两期对比（{why}派生列）",
                        }
                    )
    if imported and d1.get("metric_ids"):
        try:  # 局部 import：ask.joins 依赖本模块的 DecisionError，模块级互相 import 会成环
            from .joins import plan_source

            by_id = {m.get("id"): m for m in (metric_dict or [])}
            metric_cols: list = []
            for mid in d1.get("metric_ids") or []:
                metric = by_id.get(mid)
                if metric is None:
                    continue
                for column in definition_identifiers(metric.get("definition") or ""):
                    if column not in metric_cols:
                        metric_cols.append(column)
            dims = [d for d in (d1.get("dimensions") or []) if d != PERIOD_COLUMN]
            source = plan_source(
                imported,
                dimensions=dims,
                metric_columns=metric_cols,
                metric_tables=[
                    table
                    for mid in d1.get("metric_ids") or []
                    for table in ((by_id.get(mid) or {}).get("requires_tables") or [])
                ],
                prefer=[entry["name"] for entry in tables],
                question=question,
                pool=pool,
            )
            joins = source.as_joins()
            for name in source.pool_used:
                if not any(entry["name"] == name for entry in tables):
                    # 桥接表（如 q039 的 product_dim）：两张表没有同名列，借它一跳连过去
                    tables.append(
                        {"name": name, "reason": f"桥接表：主表与目标维表没有同名列，经 {name} 等值连过去"}
                    )
        except DecisionError:
            joins = []
    return {
        "tables": tables,
        "columns": columns,
        "joins": joins,
        "pruned": False,
        "prune_strategy": "all",
        "token_estimate": 0,
    }


def _single_date_column(schema) -> str | None:
    """schema 里唯一一个 date 列；没有或不止一个 → None（不猜）。"""
    dates: list = []
    for entry in schema or []:
        for sheet in entry.get("sheets") or []:
            for col in sheet.get("columns") or []:
                name = col.get("name")
                if col.get("type") == "date" and name and name not in dates:
                    dates.append(name)
    return dates[0] if len(dates) == 1 else None


def _table_of_column(schema, column) -> str | None:
    """`column` 属于哪张表（取 schema 里第一张含它的表）；找不到 → None。"""
    if not column:
        return None
    for entry in schema or []:
        for sheet in entry.get("sheets") or []:
            for col in sheet.get("columns") or []:
                if col.get("name") == column:
                    return sheet.get("name") or Path(str(entry.get("file", ""))).stem
    return None


def fallback_d4(*, reason: str = "模型不可用") -> dict:
    """§5.1 D4 兜底 = 只给数，不说话（`narrative=[]` + 非空 no_claim_reason）。"""
    return {"narrative": [], "no_claim_reason": f"没有可用证据链，按契约不给结论：{reason}"}


# --------------------------------------------------------------------------- #
# D1 路由：ok / clarify / error（§7.1）
# --------------------------------------------------------------------------- #


def _has_time_word(question: str | None) -> bool:
    return bool(question) and bool(_TIME_WORD_RE.search(question))


def _time_words(question: str | None) -> list:
    return _TIME_WORD_RE.findall(question or "")


def _years(question: str | None) -> list:
    return sorted({int(y) for y in _YEAR_RE.findall(question or "")})


def time_column_ambiguous(d1_output: dict, question: str, schema, metric_dict=None) -> list:
    """§7.1 第 4 条（v1.1）：`time_column` 为 null、schema 里有**两个以上**候选时间列、问题又无线索。

    v1.1 已删掉「数据集不唯一」这条触发条件（多表/多文件归 D3 的 `joins[]`），
    所以这里只看「时间落在哪一列」，不再看「用哪份数据」。

    本实现补的两条边界（契约没写、但不多这一层就会把能答的题反问掉）：
    1. **没有时间约束就不问**：`time_range` 与 `comparison` 都为 null 时，时间落在哪一列不影响结果
       （q032/q040 没有时间词，却被问「用 sale_date 还是 register_date」，属于凭空反问）。
    2. **候选列只看指标所在的事实表**：指标定义引用到的列落在哪张表，时间过滤就只可能作用在
       那张表上。`register_date` 在客户维表里，而成交额/订单数只来自 `sales`——这不是歧义。
    """
    if d1_output.get("time_column"):
        return []
    if d1_output.get("time_range") is None and d1_output.get("comparison") is None:
        return []
    dates: list = []
    for entry in schema or []:
        for sheet in entry.get("sheets") or []:
            for col in sheet.get("columns") or []:
                name = col.get("name")
                if col.get("type") == "date" and name and name not in dates:
                    dates.append(name)
    if len(dates) < 2:
        return []
    named = [name for name in dates if name in (question or "")]
    if named:
        return []
    allowed = _metric_sheets(d1_output, schema, metric_dict)
    if allowed:
        scoped = [name for name in dates if _column_sheets(schema, name) & allowed]
        if len(scoped) < 2:
            return []
        dates = scoped
    return dates


def _metric_sheets(d1_output: dict, schema, metric_dict) -> set:
    """指标定义引用到的列落在哪些表（sheet 名 / CSV 主名）。"""
    if not metric_dict:
        return set()
    by_id = {m.get("id"): m for m in metric_dict}
    idents: set = set()
    for mid in d1_output.get("metric_ids") or []:
        metric = by_id.get(mid)
        if metric is not None:
            idents |= set(definition_identifiers(metric.get("definition") or ""))
    if not idents:
        return set()
    sheets: set = set()
    for entry in schema or []:
        for sheet in entry.get("sheets") or []:
            names = {col.get("name") for col in sheet.get("columns") or []}
            if idents & names:
                sheets.add(sheet.get("name") or Path(str(entry.get("file", ""))).stem)
    return sheets


def _column_sheets(schema, column: str) -> set:
    out: set = set()
    for entry in schema or []:
        for sheet in entry.get("sheets") or []:
            if any(col.get("name") == column for col in sheet.get("columns") or []):
                out.add(sheet.get("name") or Path(str(entry.get("file", ""))).stem)
    return out


@dataclass
class D1Route:
    status: str  # ok / clarify / error
    clarify: dict | None
    caveats: list
    unknown_metric_ids: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "clarify": self.clarify,
            "caveats": self.caveats,
            "unknown_metric_ids": self.unknown_metric_ids,
        }


def route_d1(
    d1_output: dict,
    *,
    question: str,
    metric_dict=None,
    schema=None,
    truncate_slots: bool = False,
) -> D1Route:
    """§7.1 的触发条件落地：`ambiguity != []` ⟺ `status="clarify"`。

    - `metric_ids` 为空 / 有不在字典里的 id → `METRIC_NOT_IN_DICT` + 反问（不猜）
    - 有时间词但 `time_range` 为 null → 进 ambiguity（`validate_d1` 已要求，这里补槽位）
    - 多文件且问题没点名 → `dataset` 槽位
    - 槽位 > 3 → `QUESTION_TOO_BROAD`（§7.1：问题本身太大，不硬问）
    """
    ids = set(m.get("id") for m in (metric_dict or [])) if metric_dict is not None else None
    unknown = [m for m in d1_output["metric_ids"] if ids is not None and m not in ids]

    caveats = []
    slots = []  # [(slot, why, candidates)]

    if unknown:
        caveats.append(
            caveat(
                "METRIC_NOT_IN_DICT",
                f"指标 {unknown} 不在字典里：不猜口径，改为反问",
                fields=["metrics_used", "clarify"],
            )
        )
        slots.append(("metric", f"「{'、'.join(unknown)}」在字典里查不到，不能替它选一个口径", unknown))

    if not d1_output["metric_ids"] and d1_output.get("listing") is not True:
        # §7.1 v1.1：listing=true 是 metric_ids=[] 唯一合法的出口，不算信息不足
        slots.append(("metric", "问题里没有可执行的指标口径", []))

    for amb in d1_output["ambiguity"]:
        slot = amb["slot"]
        if not any(s[0] == slot for s in slots):
            slots.append((slot, amb["why"], list(amb.get("candidate_ids") or [])))

    if d1_output["time_range"] is None and d1_output.get("comparison") is None and _has_time_word(question):
        if not any(s[0] == "time_range" for s in slots):
            slots.append(("time_range", f"问题里出现了时间词（{_time_words(question)}），但没定下时间范围", []))

    date_columns = time_column_ambiguous(d1_output, question, schema, metric_dict)
    if date_columns and not any(s[0] == "time_column" for s in slots):
        slots.append(
            ("time_column", "时间范围要落在哪一列不明确：这几列都能当业务日期，算出来的数不一样", date_columns)
        )

    if not slots:
        return D1Route("ok", None, sort_caveats(caveats), unknown)

    if len(slots) > MAX_CLARIFY_SLOTS and not truncate_slots:
        raise DecisionError(
            "QUESTION_TOO_BROAD",
            f"一次要问 {len(slots)} 个槽位（{ [s[0] for s in slots] }），超过 §7.1 的上限 {MAX_CLARIFY_SLOTS}：问题本身太大",
            stage="D1",
            readable_hint="这个问题要拆：先定口径和时间范围，再问具体的分布/对比",
        )
    if len(slots) > MAX_CLARIFY_SLOTS:
        caveats.append(
            caveat(
                "CLARIFY_TRUNCATED_SLOTS",
                f"反问槽位有 {len(slots)} 个，只问了前 {MAX_CLARIFY_SLOTS} 个",
                fields=["clarify"],
            )
        )
        slots = slots[:MAX_CLARIFY_SLOTS]

    clarify = build_clarify(slots, question=question, metric_dict=metric_dict, schema=schema)
    return D1Route("clarify", clarify, sort_caveats(caveats), unknown)


_SLOT_ASK = {
    "metric": "按哪个口径算",
    "time_range": "算哪段时间",
    "time_column": "时间落在哪一列",
    "grain": "按什么粒度",
    "dimension": "按哪个维度分组",
    "filter": "要不要加过滤条件",
}


def _candidates_for(slot: str, cands, *, question: str, metric_dict, schema) -> list:
    """§2.10：候选必须带 `{id, name, definition}`；`id=null` 表示「候选不是字典条目」。"""
    by_id = {m.get("id"): m for m in (metric_dict or [])}
    out = []
    if slot == "metric":
        for cid in cands or []:
            metric = by_id.get(cid)
            if metric is not None:
                out.append(
                    {
                        "id": metric.get("id"),
                        "name": metric.get("name"),
                        "definition": metric.get("definition"),
                    }
                )
            elif cid:
                out.append({"id": None, "name": str(cid), "definition": ""})
    elif slot == "time_column":
        for name in cands or []:
            out.append({"id": None, "name": str(name), "definition": f"用 {name} 当业务日期列"})
    elif slot == "time_range":
        years = _years(question)
        if len(years) == 1:
            year = years[0]
            out.append(
                {
                    "id": None,
                    "name": f"{year} 年（1/1–12/31）",
                    "definition": f"把问题里的「{year} 年」按自然年解释",
                }
            )
    else:
        for cid in cands or []:
            metric = by_id.get(cid)
            if metric is not None:
                out.append({"id": metric.get("id"), "name": metric.get("name"), "definition": metric.get("definition")})
    return out


def build_clarify(slots, *, question: str, metric_dict=None, schema=None) -> dict:
    """§2.10 反问体。每个槽位一条 missing，`asked_at_step` 固定 "D1"（P0 只有 D1 会反问）。"""
    missing = []
    prompts = []
    for slot, why, cands in slots:
        candidates = _candidates_for(slot, cands, question=question, metric_dict=metric_dict, schema=schema)
        missing.append({"slot": slot, "why_needed": why, "candidates": candidates})
        names = [c["name"] for c in candidates if c.get("name")]
        if len(names) >= 2:
            prompts.append(f"{_SLOT_ASK[slot]}：" + " 还是 ".join(names))
        elif names:
            prompts.append(f"{_SLOT_ASK[slot]}：{names[0]}（还有别的选法就说）")
        else:
            prompts.append(_SLOT_ASK[slot])
    head = prompts[0] if len(prompts) == 1 else "需要确认：" + "；".join(prompts)
    return {"question": head + "？", "missing": missing, "asked_at_step": "D1"}


# --------------------------------------------------------------------------- #
# 串起来跑（供 #4 的 `ask/query.py` 调用）
# --------------------------------------------------------------------------- #


@dataclass
class DecisionBundle:
    """四个决策点跑到哪就是哪：D4 要等执行完有 traces 才能跑，这里允许留空。"""

    question: str
    status: str
    route: D1Route
    runner: "DecisionRunner"
    files: list = field(default_factory=list)
    sheets: list = field(default_factory=list)
    metric_dict_sha256: str = ""

    @property
    def outputs(self) -> dict:
        return {name: r.output for name, r in self.runner.results.items()}

    def caveats(self) -> list:
        return merge_caveats(self.route.caveats, self.runner.all_caveats())

    def cache_key(self, options=None) -> str:
        part = self.runner.decisions_key_part()
        if set(part) != set(DECISIONS):
            raise DecisionError(
                "INTERNAL",
                f"缓存键需要 D1–D4 四个决策点都在（当前只有 {sorted(part)}）",
                stage="export",
                readable_hint="D4 要等 SQL 执行出 traces 才能跑；没有 D4 就没有 cache_key",
            )
        return cache_key(
            question=self.question,
            files=self.files,
            sheets=self.sheets,
            metric_dict_sha256=self.metric_dict_sha256,
            decisions=part,
            options=options,
        )

    def answer_id(self, options=None) -> str:
        return answer_id(self.cache_key(options))

    def meta_model(self) -> dict:
        return self.runner.meta_model()


def decide_all(
    runner: DecisionRunner,
    *,
    question: str,
    schema,
    metric_dict,
    now: str,
    history=None,
    files=None,
    sheets=None,
    metric_dict_sha256: str = "",
    traces=None,
    table=None,
    tables=None,
    pool=None,
) -> DecisionBundle:
    """D1 →（路由）→ D2 → D3 →[执行在 #4]→ D4。状态不是 ok 就停在反问/错误上。"""
    d1 = runner.run("D1", build_d1_input(question, schema, metric_dict, now, history), question=question)
    route = route_d1(d1.output, question=question, metric_dict=metric_dict, schema=schema)
    bundle = DecisionBundle(
        question=question,
        status=route.status,
        route=route,
        runner=runner,
        files=list(files or []),
        sheets=list(sheets or []),
        metric_dict_sha256=metric_dict_sha256,
    )
    if route.status != "ok":
        return bundle

    d2 = runner.run("D2", build_d2_input(question, d1.output, schema), d1=d1.output)
    d3 = runner.run(
        "D3",
        build_d3_input(question, d1.output, d2.output, schema),
        d1=d1.output,
        d2=d2.output,
        schema=schema,
        tables=tables,
        pool=pool,
        metric_dict=metric_dict,
        question=question,
    )
    check_d3_coverage(d3.output, d2.output, schema, metric_dict)

    if traces is not None or table is not None:
        runner.run(
            "D4",
            build_d4_input(question, d1.output, d2.output, table or {}, traces, runner.options),
            d1=d1.output,
            d2=d2.output,
            traces=traces,
        )
    return bundle


# --------------------------------------------------------------------------- #
# `--verify-schemas`：注入文件与别名表的形状自检
# --------------------------------------------------------------------------- #


def _inject_dir(path: str | Path | None = None) -> Path:
    if path is None:
        return PROJECT_ROOT / DEFAULT_INJECT_DIR
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def verify_schemas(inject_dir: str | Path | None = None, aliases_path: str | Path | None = None) -> int:
    """校验 `ask/aliases.json` 与 `ask/inject/*.json` 的形状，全过才返回 0。

    注入文件按 §5.2 判：`decision` 与文件名一致、`contract_version` 对得上、
    `input_digest` 是 64 位十六进制或留空、`output` 过 §5.1 的形状校验。
    """
    ok = True
    print("ask.decide --verify-schemas")

    # 1) 别名表
    try:
        table = AliasTable.load(aliases_path)
        problems = table.check()
        if problems:
            ok = False
            for problem in problems:
                print(f"  [FAIL] {table.path}: {problem}")
        else:
            print(f"  [PASS] {table.path}: {len(table.columns)} 列 / "
                  f"{sum(len(v) for v in table.columns.values())} 条别名，无歧义")
    except DecisionError as exc:
        ok = False
        print(f"  [FAIL] 别名表: [{exc.code}] {exc.message}")

    # 2) 注入文件
    directory = _inject_dir(inject_dir)
    found = 0
    for decision in DECISIONS:
        path = directory / f"{decision.lower()}.json"
        if not path.exists():
            print(f"  [SKIP] {_rel_posix(path, PROJECT_ROOT)} 不存在")
            continue
        found += 1
        rel = _rel_posix(path, PROJECT_ROOT)
        try:
            output, rel, checked = load_inject(
                path, decision, None, root=PROJECT_ROOT, allow_unchecked_digest=True
            )
            validate_output(decision, output)
            digest_note = "input_digest 已声明（本次未提供输入，未对账）" if not checked else "input_digest 已对账"
            print(f"  [PASS] {rel}: decision={decision} output_sha256={sha256_digest(output)[:16]} · {digest_note}")
        except DecisionError as exc:
            ok = False
            print(f"  [FAIL] {rel}: [{exc.code}] {exc.message}")
        except SchemaViolation as exc:
            ok = False
            print(f"  [FAIL] {rel}: {exc.message}")

    if found == 0:
        ok = False
        print(f"  [FAIL] {_rel_posix(directory, PROJECT_ROOT)} 下没有任何注入文件"
              "（至少要有一份 d1–d4，`--decide inject:` 才有东西可吃）")

    print(f"结果: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


def _selftest() -> int:
    """把「形状校验 / 兜底 / 注入 / 缓存键 / 别名匹配」这些规则钉成可复跑的断言。"""
    import tempfile

    results: list = []

    def check(name: str, fn) -> None:
        try:
            results.append((True, name, fn() or ""))
        except AssertionError as exc:
            results.append((False, name, str(exc) or "断言不成立"))
        except Exception as exc:  # pragma: no cover
            results.append((False, name, f"{type(exc).__name__}: {exc}"))

    metric_dict = [
        {
            "id": "gmv",
            "name": "成交额",
            "definition": "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
            "unit": "CNY",
            "version": 3,
        }
    ]
    schema = [
        {
            "file": "data/sales.xlsx",
            "sha256": "1" * 64,
            "sheets": [
                {
                    "name": "Sheet1",
                    "header_row": 1,
                    "header_row_guessed": False,
                    "columns": [
                        {"name": "region", "type": "text", "null_ratio": 0.0, "unique_count": 7},
                        {"name": "status", "type": "text", "null_ratio": 0.0, "unique_count": 2},
                        {"name": "amount", "type": "decimal", "null_ratio": 0.0, "unique_count": 20},
                    ],
                }
            ],
        }
    ]
    d1_ok = {
        "metric_ids": ["gmv"],
        "listing": False,
        "time_range": {"start": "2025-01-01", "end": "2025-12-31", "grain": "year"},
        "time_column": None,
        "time_grain_grouping": None,
        "comparison": None,
        "dimensions": ["region"],
        "filters": [],
        "ambiguity": [],
        "confidence": 0.86,
    }

    # ---- 1. 提问归一化：只做四件事 ----
    def _normalize():
        assert normalize_question("  2025 年  各区域的成交额 ") == "2025 年 各区域的成交额"
        assert normalize_question("ＡＢＣ　１２３") == "ABC 123", normalize_question("ＡＢＣ　１２３")
        assert normalize_question("销售额  VS  GMV") == "销售额 VS GMV", "大小写/标点不许改"
        assert normalize_question(normalize_question(" 甲  乙 ")) == normalize_question(" 甲  乙 "), "归一化要幂等"
        return "NFC/去首尾空白/全角转半角/压空白，大小写与标点不动"

    check("§5.3 提问归一化只做四件事", _normalize)

    # ---- 2. cache_key / answer_id ----
    def _cache_key():
        decisions = {
            "D1": {"source": "inject", "output_sha256": "a" * 64},
            "D2": {"source": "model", "output_sha256": "b" * 64},
            "D3": {"source": "fallback", "output_sha256": "c" * 64},
            "D4": {"source": "inject", "output_sha256": "d" * 64},
        }
        files = [{"path": "data/b.csv", "sha256": "2" * 64}, {"path": "data/a.xlsx", "sha256": "1" * 64}]
        kwargs = dict(
            question=" 2025 年各区域的成交额是多少 ",
            files=files,
            sheets=["Sheet1"],
            metric_dict_sha256="7" * 64,
            decisions=decisions,
        )
        key = cache_key(**kwargs)
        assert re.fullmatch(r"[0-9a-f]{64}", key)
        assert key == cache_key(**{**kwargs, "question": "2025 年各区域的成交额是多少"}), "归一化前后必须是同一个键"
        assert key == cache_key(**{**kwargs, "files": list(reversed(files))}), "files 顺序不许影响键"
        tilted = {**decisions, "D3": {"source": "fallback", "output_sha256": "e" * 64}}
        assert cache_key(**{**kwargs, "decisions": tilted}) != key, "换一份注入答案就必须换 id（§5.3）"
        assert cache_key(**{**kwargs, "options": {"row_cap": 100}}) != key, "options 参与缓存键"
        try:
            cache_key(**{**kwargs, "options": {"row_cap": 5000, "verbose": True}})
        except ValueError as exc:
            assert "verbose" in str(exc)
        else:
            raise AssertionError("options 里多出来的键必须报（§2.12 只定义三个）")
        assert len(answer_id(key)) == 16 and answer_id(key) == answer_id(key)
        return f"key={key[:16]}… answer_id={answer_id(key)}"

    check("§5.3 缓存键: 归一化/文件序/注入集变化都要影响 id", _cache_key)

    # ---- 3. 缓存键里不许出现浮点 ----
    def _no_float():
        try:
            canonical_json({"a": 1.5})
        except ValueError as exc:
            assert "浮点" in str(exc)
        else:
            raise AssertionError("浮点必须被拒")
        assert canonical_json({"b": 1, "a": None}) == '{"a":null,"b":1}', "键要按码点升序、整数不带 .0"
        assert digest_json({"r": 0.5}) == '{"r":0.5}', "digest 允许有限浮点（schema 里的 null_ratio）"
        try:
            digest_json({"x": float("nan")})
        except ValueError:
            pass
        else:
            raise AssertionError("NaN 必须炸，不许算出一个稳定但没意义的哈希")
        return "缓存键拒浮点；digest 允许有限浮点、拒绝 NaN"

    check("§5.3/§6.2 浮点: 缓存键拒浮点，NaN 直接炸", _no_float)

    # ---- 4. D1 校验 + 路由（ok / clarify）----
    def _d1():
        validate_d1(d1_ok, question="2025 年各区域的成交额是多少")
        route = route_d1(d1_ok, question="2025 年各区域的成交额是多少", metric_dict=metric_dict, schema=schema)
        assert route.status == "ok" and route.clarify is None, route.as_dict()

        ambiguous = {
            **d1_ok,
            "ambiguity": [{"slot": "metric", "why": "字典里两个口径都叫销售额", "candidate_ids": ["gmv", "gmv_paid"]}],
        }
        route2 = route_d1(ambiguous, question="销售额", metric_dict=metric_dict + [
            {"id": "gmv_paid", "name": "已付款成交额", "definition": "SUM(amount)", "unit": "CNY", "version": 1}
        ], schema=schema)
        assert route2.status == "clarify", route2.as_dict()
        assert route2.clarify["asked_at_step"] == "D1"
        assert route2.clarify["missing"][0]["candidates"][1]["definition"] == "SUM(amount)", "候选必须带定义"

        unknown = {**d1_ok, "metric_ids": ["gmv_nope"]}
        route3 = route_d1(unknown, question="毛利率", metric_dict=metric_dict, schema=schema)
        assert route3.status == "clarify" and route3.unknown_metric_ids == ["gmv_nope"]
        assert "METRIC_NOT_IN_DICT" in [c["code"] for c in route3.caveats], route3.caveats

        try:
            validate_d1({**d1_ok, "time_range": None}, question="2025 年各区域的成交额是多少")
        except SchemaViolation as exc:
            assert "时间词" in exc.message
        else:
            raise AssertionError("有时间词但 time_range=null 且 ambiguity 空 → 必须判不合法")
        return "ok / clarify（带候选定义）/ METRIC_NOT_IN_DICT / 时间词规则 各一条"

    check("§7.1 D1 路由: ambiguity≠[] ⟺ clarify，指标不在字典不猜", _d1)

    # ---- 5. 槽位 > 3 → QUESTION_TOO_BROAD ----
    def _too_broad():
        messy = {
            **d1_ok,
            "metric_ids": [],
            "time_range": None,
            "ambiguity": [
                {"slot": "metric", "why": "a", "candidate_ids": []},
                {"slot": "grain", "why": "b", "candidate_ids": []},
                {"slot": "dimension", "why": "c", "candidate_ids": []},
                {"slot": "filter", "why": "d", "candidate_ids": []},
            ],
        }
        try:
            route_d1(messy, question="卖得怎么样", metric_dict=metric_dict, schema=schema)
        except DecisionError as exc:
            assert exc.code == "QUESTION_TOO_BROAD" and exc.stage == "D1"
        else:
            raise AssertionError("超过 3 个槽位必须报 QUESTION_TOO_BROAD（§7.1）")
        fine = {**messy, "ambiguity": messy["ambiguity"][:1], "metric_ids": ["gmv"]}
        route = route_d1(fine, question="卖得怎么样", metric_dict=metric_dict, schema=schema)
        assert route.status == "clarify"
        return "4 个槽位 → QUESTION_TOO_BROAD；1 个槽位 → clarify"

    check("§7.1 反问: 超过 3 个槽位报 QUESTION_TOO_BROAD，不硬问", _too_broad)

    # ---- 6. D2 最小计划兜底 + PLAN_MINIMAL ----
    def _d2_fallback():
        runner = DecisionRunner()
        result = runner.run("D2", {"question": "q", "d1": d1_ok, "schema": schema}, d1=d1_ok)
        assert result.source == "fallback" and result.injected is False
        assert result.output["steps"][0]["title"] == "按问题直接取数"
        assert result.output["steps"][0]["intent"] == "detail"
        # 兜底只「不拆步」，不许把 D1 已经定好的口径也丢掉（否则问聚合答明细）
        assert result.output["steps"][0]["needs"] == {
            "metric_ids": d1_ok["metric_ids"],
            "dimensions": d1_ok["dimensions"],
        }, result.output["steps"][0]["needs"]
        assert [c["code"] for c in result.caveats] == ["PLAN_MINIMAL"], result.caveats
        validate_d2(result.output)
        return f"source={result.source} caveat=PLAN_MINIMAL steps={len(result.output['steps'])}"

    check("§5.1 D2 兜底: 最小计划 + PLAN_MINIMAL", _d2_fallback)

    # ---- 7. D2 形状：step_id 连续 / 相邻同 intent 同 needs 判凑数 ----
    def _d2_shape():
        good = {
            "steps": [
                {"step_id": 1, "intent": "distribution", "title": "先看各区域行数分布",
                 "needs": {"metric_ids": [], "dimensions": ["region"]}, "sql_hint": None},
                {"step_id": 2, "intent": "detail", "title": "按区域汇总成交额",
                 "needs": {"metric_ids": ["gmv"], "dimensions": ["region"]}, "sql_hint": None},
            ],
            "stop_reason": None,
        }
        validate_d2(good)
        dup = {
            "steps": [
                {**good["steps"][1], "step_id": 1},
                {**good["steps"][1], "step_id": 2},
            ],
            "stop_reason": None,
        }
        try:
            validate_d2(dup)
        except SchemaViolation as exc:
            assert "凑数" in exc.message, exc.message
        else:
            raise AssertionError("相邻同 intent 同 needs 必须判不合法（§5.1）")
        gap = {"steps": [{**good["steps"][0], "step_id": 2}], "stop_reason": None}
        try:
            validate_d2(gap)
        except SchemaViolation:
            pass
        else:
            raise AssertionError("step_id 必须从 1 连续升序")
        return "合法计划通过；凑数 / step_id 断号 各拦住一条"

    check("§5.1 D2 形状: step_id 连续升序、相邻同 intent+needs 判凑数", _d2_shape)

    # ---- 8. D3 兜底：pruned=false + NOT_PRUNED（含「未精简」）+ source=fallback ----
    def _d3_fallback():
        runner = DecisionRunner()
        result = runner.run(
            "D3",
            {"question": "q", "d1": d1_ok, "d2": {"steps": []}, "schema": schema},
            d1=d1_ok,
            d2={"steps": []},
            schema=schema,
        )
        assert result.output["pruned"] is False and result.output["prune_strategy"] == "all"
        assert result.source == "fallback"
        codes = [c["code"] for c in result.caveats]
        assert codes == ["NOT_PRUNED"], codes
        assert "未精简" in result.caveats[0]["message"], result.caveats[0]["message"]
        check_d3_link(result.output, source=result.source, caveats=result.caveats)
        try:
            check_d3_link(result.output, source="model", caveats=result.caveats)
        except DecisionError as exc:
            assert "四处联动" in exc.message
        else:
            raise AssertionError("pruned=false 却 source=model 必须报（§7.2）")
        return f"pruned=false / NOT_PRUNED / source=fallback / 已选 {len(result.output['columns'])} 列"

    check("§7.2 D3 兜底四处联动: pruned=false + all + NOT_PRUNED「未精简」+ fallback", _d3_fallback)

    # ---- 9. D3 覆盖检查：维度/指标定义引用到的列不许漏 ----
    def _d3_coverage():
        d2 = {
            "steps": [
                {"step_id": 1, "intent": "detail", "title": "按区域汇总成交额",
                 "needs": {"metric_ids": ["gmv"], "dimensions": ["region"]}}
            ],
            "stop_reason": None,
        }
        good = {
            "tables": [{"name": "Sheet1", "reason": "唯一含 region 与 amount 的表"}],
            "columns": [
                {"table": "Sheet1", "column": "region", "reason": "分组维度"},
                {"table": "Sheet1", "column": "amount", "reason": "指标定义依赖"},
                {"table": "Sheet1", "column": "status", "reason": "指标定义依赖"},
            ],
            "joins": [],
            "pruned": True,
            "prune_strategy": "minimal",
            "token_estimate": 0,
        }
        check_d3_coverage(good, d2, schema, metric_dict)
        thin = {**good, "columns": good["columns"][:2]}
        try:
            check_d3_coverage(thin, d2, schema, metric_dict)
        except DecisionError as exc:
            assert exc.code == "COLUMN_NOT_FOUND" and exc.stage == "D3", exc
        else:
            raise AssertionError("少选 status 必须报 COLUMN_NOT_FOUND")
        return "三列覆盖 gmv 定义（status/amount）+ 维度 region；少一列即 COLUMN_NOT_FOUND"

    check("§5.1 D3 覆盖: 维度与指标定义引用到的列都不许漏", _d3_coverage)

    # ---- 10. D4 兜底：narrative=[] + NO_INTERPRETATION ----
    def _d4_fallback():
        runner = DecisionRunner()
        result = runner.run("D4", {"question": "q"}, traces=[])
        assert result.output["narrative"] == [] and result.output["no_claim_reason"]
        assert [c["code"] for c in result.caveats] == ["NO_INTERPRETATION"], result.caveats
        assert result.source == "fallback"
        return "source=fallback narrative=[] caveat=NO_INTERPRETATION"

    check("§5.1 D4 兜底: 只给数不说话 + NO_INTERPRETATION", _d4_fallback)

    # ---- 11. D4 校验：占位符闭合 / 裸数字 / compare 两个证据 ----
    def _d4_rules():
        traces = [{"trace_id": "t-0001", "value": 1, "metric_id": "gmv", "unit": "CNY", "digits": 2},
                  {"trace_id": "t-0002", "value": 2, "metric_id": "gmv", "unit": "CNY", "digits": 2}]
        ok = {"narrative": [{"text_template": "华东区最高，为 {{trace:t-0001}} 元",
                             "evidence": ["t-0001"], "claim_kind": "fact"}], "no_claim_reason": None}
        validate_d4(ok, traces=traces)
        cases = [
            ({"narrative": [{"text_template": "华东区最高 1234567 元", "evidence": ["t-0001"], "claim_kind": "fact"}],
              "no_claim_reason": None}, "阿拉伯数字"),
            ({"narrative": [{"text_template": "华东最高，为 {{trace:t-0009}} 元", "evidence": ["t-0009"], "claim_kind": "fact"}],
              "no_claim_reason": None}, "不存在"),
            ({"narrative": [{"text_template": "{{trace:t-0001}}", "evidence": [], "claim_kind": "fact"}],
              "no_claim_reason": None}, "evidence"),
            ({"narrative": [], "no_claim_reason": None}, "no_claim_reason"),
            ({"narrative": [{"text_template": "只有 {{trace:t-0001}}",
                             "evidence": ["t-0001"], "claim_kind": "compare"}], "no_claim_reason": None}, "compare"),
        ]
        for bad, keyword in cases:
            try:
                validate_d4(bad, traces=traces)
            except SchemaViolation as exc:
                assert keyword in exc.message, f"{keyword} → {exc.message}"
            else:
                raise AssertionError(f"应被拦下：{keyword}")
        return f"合法句通过；裸数字/幽灵 trace/空 evidence/空 narrative 无理由/compare 单证据 共 {len(cases)} 条被拦"

    check("§5.1 D4 校验: 占位符必须闭合、不许裸阿拉伯数字、compare 要两个证据", _d4_rules)

    # ---- 12. 注入：stale / 未写 digest / 形状错 ----
    def _inject():
        with tempfile.TemporaryDirectory(prefix="tally-decide-") as tmp:
            root = Path(tmp)
            (root / "inject").mkdir()
            d1_in = build_d1_input("2025 年各区域的成交额是多少", schema, metric_dict, "2026-10-26")
            payload = {"decision": "D1", "contract_version": CONTRACT_VERSION,
                       "input_digest": input_digest(d1_in), "output": d1_ok}
            good = root / "inject" / "d1.json"
            good.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            runner = DecisionRunner(injects={"D1": str(good)}, root=root)
            result = runner.run("D1", d1_in, question="2025 年各区域的成交额是多少")
            assert result.source == "inject" and result.injected is True and result.latency_ms == 0
            assert result.inject_path == "inject/d1.json", result.inject_path
            meta = result.meta_entry()
            assert meta["injected"] is True and meta["source"] == "inject" and meta["model"] is None
            assert meta["inject_path"] == "inject/d1.json" and result.caveats == []

            stale = dict(payload, input_digest="0" * 64)
            good.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
            try:
                DecisionRunner(injects={"D1": str(good)}, root=root).run("D1", d1_in)
            except DecisionError as exc:
                assert exc.code == "INJECT_STALE" and exc.stage == "D1"
            else:
                raise AssertionError("input_digest 不符必须报 INJECT_STALE")

            unchecked = dict(payload, input_digest="")
            good.write_text(json.dumps(unchecked, ensure_ascii=False), encoding="utf-8")
            result2 = DecisionRunner(injects={"D1": str(good)}, root=root).run("D1", d1_in, question="销售")
            assert result2.injected is True
            assert [c["code"] for c in result2.caveats] == ["INJECT_DIGEST_UNCHECKED"], result2.caveats

            wrong_decision = dict(payload, decision="D2")
            good.write_text(json.dumps(wrong_decision, ensure_ascii=False), encoding="utf-8")
            try:
                DecisionRunner(injects={"D1": str(good)}, root=root).run("D1", d1_in)
            except DecisionError as exc:
                assert exc.code == "INJECT_SCHEMA_INVALID"
            else:
                raise AssertionError("decision 不一致必须报 INJECT_SCHEMA_INVALID")

            broken = dict(payload, output={**d1_ok, "needs_clarify": True})
            good.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            try:
                DecisionRunner(injects={"D1": str(good)}, root=root).run("D1", d1_in)
            except DecisionError as exc:
                assert exc.code == "INJECT_SCHEMA_INVALID", exc.code
                assert "needs_clarify" in exc.message
            else:
                raise AssertionError("输出不合 §5.1 必须报 INJECT_SCHEMA_INVALID，不许修正")
            return "命中注入 / INJECT_STALE / INJECT_DIGEST_UNCHECKED / decision 不符 / output 不合法 各一条"
        return ""

    check("§5.2 注入: digest 对账、stale 报错、留空要记 caveat、output 不许修正", _inject)

    # ---- 13. --decide 参数解析 ----
    def _parse():
        got = parse_decide_args(["d1=inject:ask/inject/d1.json", "D4=inject:ask/inject/d4.json"])
        assert got == {"D1": "ask/inject/d1.json", "D4": "ask/inject/d4.json"}, got
        for bad in (["d1=model"], ["d9=inject:x.json"], ["d1"], ["d1=inject:"]):
            try:
                parse_decide_args(bad)
            except ValueError:
                continue
            raise AssertionError(f"应判参数错误: {bad}")
        return "d1/d4 解析正确；model / 未知决策点 / 缺等号 / 空路径 都拒"

    check("§5.2 --decide 参数: 只认 dN=inject:PATH，可只给子集", _parse)

    # ---- 14. 别名表：人工别名 > 精确 > 编辑距离（模糊必须记 caveat）----
    def _alias():
        table = AliasTable.load()
        assert table.check() == [], table.check()
        hit = table.match("金额", ["region", "amount", "status"])
        assert (hit.column, hit.how) == ("amount", "manual_alias"), hit
        assert hit.caveat() is None, "人工别名不许记 COLUMN_ALIAS_FUZZY"
        exact = table.match("Region", ["region", "amount"])
        assert (exact.column, exact.how) == ("region", "exact"), exact
        fuzzy = table.match("amoun", ["amount", "region"])
        assert (fuzzy.column, fuzzy.how) == ("amount", "fuzzy") and fuzzy.distance == 1, fuzzy
        cav = fuzzy.caveat()
        assert cav["code"] == "COLUMN_ALIAS_FUZZY" and cav["message"], cav
        assert table.match("区域", ["amount"]).column is None, "别名指向的列不在候选里 → 不许跨表猜"
        assert table.match("a", ["amount"]).column is None, "词太短不走编辑距离"
        assert table.match("金额", ["金额1", "金额2"]).how == "none", "并列歧义不许挑一个"
        return "manual_alias/exact/fuzzy(记 COLUMN_ALIAS_FUZZY)/none 四条 + 表本身无歧义"

    check("中文列别名: 优先级 + 模糊命中必须记 COLUMN_ALIAS_FUZZY", _alias)

    # ---- 15. D1 无兜底：没有模型也没注入就报 INTERNAL ----
    def _bundle_no_model():
        runner = DecisionRunner()
        try:
            decide_all(runner, question="2025 年各区域的成交额是多少", schema=schema,
                       metric_dict=metric_dict, now="2026-10-26")
        except DecisionError as exc:
            assert exc.code == "INTERNAL" and exc.stage == "D1", exc
            return "D1 无模型且未注入 → INTERNAL（契约没给 D1 兜底，不自己造）"
        raise AssertionError("D1 没有兜底，必须报错而不是编一个理解")

    check("§5.1 D1 无兜底: 没有模型也没注入就报 INTERNAL，不许猜", _bundle_no_model)

    def _meta_model_key():
        runner = DecisionRunner()
        plan = fallback_plan()
        d1_in = build_d1_input("2025 年各区域的成交额是多少", schema, metric_dict, "2026-10-26")
        with tempfile.TemporaryDirectory(prefix="tally-decide-") as tmp:
            root = Path(tmp)
            (root / "inject").mkdir()
            (root / "inject" / "d1.json").write_text(
                json.dumps({"decision": "D1", "contract_version": CONTRACT_VERSION,
                            "input_digest": input_digest(d1_in), "output": d1_ok}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "inject" / "d2.json").write_text(
                json.dumps({"decision": "D2", "contract_version": CONTRACT_VERSION, "input_digest": "",
                            "output": {"steps": plan["steps"], "stop_reason": None}}, ensure_ascii=False),
                encoding="utf-8",
            )
            runner = DecisionRunner(injects={"D1": str(root / "inject" / "d1.json"),
                                            "D2": str(root / "inject" / "d2.json")}, root=root)
            bundle = decide_all(
                runner,
                question="2025 年各区域的成交额是多少",
                schema=schema,
                metric_dict=metric_dict,
                now="2026-10-26",
                files=[{"path": "data/a.xlsx", "sha256": "1" * 64}],
                sheets=["Sheet1"],
                metric_dict_sha256="7" * 64,
            )
            assert bundle.status == "ok", bundle.route.as_dict()
            meta = bundle.meta_model()
            assert set(meta) == {"D1", "D2", "D3"}, meta.keys()
            assert meta["D1"]["injected"] is True and meta["D1"]["inject_path"].endswith("d1.json")
            assert meta["D3"]["source"] == "fallback", meta["D3"]
            codes = [c["code"] for c in bundle.caveats()]
            assert "PLAN_MINIMAL" not in codes, "D2 吃了注入就不该报 PLAN_MINIMAL"
            assert "INJECT_DIGEST_UNCHECKED" in codes, codes
            assert "NOT_PRUNED" in codes, codes
            try:
                bundle.cache_key()
            except DecisionError as exc:
                assert exc.code == "INTERNAL" and "D1–D4" in exc.message
            else:
                raise AssertionError("D4 没跑就不许算 cache_key（§5.3 要四个决策点）")
            return f"meta.model={sorted(meta)}；caveats={codes}；缺 D4 拒算 cache_key"

    check("§2.12 meta.model + §5.3 缺 D4 拒算缓存键", _meta_model_key)

    ok = sum(1 for r in results if r[0])
    print("ask.decide 自检")
    for passed, name, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    print(f"结果: {ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

USAGE = """ask.decide · 决策四段 D1–D4 + --decide inject 注入 + 缓存键 + 中文别名表

  python -m ask.decide --verify-schemas [--inject-dir DIR] [--aliases FILE]
        校验 ask/aliases.json 与 ask/inject/*.json 的形状（契约 §5.2 / §5.1）
  python -m ask.decide --selftest
        自检：归一化 / 缓存键 / 四段校验 / 兜底 / 注入 / 别名匹配
  python -m ask.decide --show-aliases
        打印别名表统计（列数、别名数、模糊阈值）
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
    rest = argv[1:]

    if cmd == "--selftest":
        return _selftest()
    if cmd == "--show-aliases":
        table = AliasTable.load()
        problems = table.check()
        print(
            json.dumps(
                {
                    "path": table.path,
                    "columns": len(table.columns),
                    "aliases": sum(len(v) for v in table.columns.values()),
                    "fuzzy": {"max_edit_distance": table.max_distance, "min_term_len": table.min_term_len},
                    "problems": problems,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not problems else 1
    if cmd == "--verify-schemas":
        inject_dir = None
        aliases = None
        i = 0
        while i < len(rest):
            if rest[i] == "--inject-dir" and i + 1 < len(rest):
                inject_dir = rest[i + 1]
                i += 2
            elif rest[i] == "--aliases" and i + 1 < len(rest):
                aliases = rest[i + 1]
                i += 2
            else:
                print(f"未知参数: {rest[i]}", file=sys.stderr)
                print(USAGE, file=sys.stderr)
                return 2
        return verify_schemas(inject_dir, aliases)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
