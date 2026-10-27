"""ask.metric_dict · 指标字典加载与校验（契约 §3）

契约出处（`contract/answer.md`）：
- §3.1 `metric` 字段表 + 单位白名单（封闭集）
- §3.2 校验规则 V1–V12（「任何一条不过就报错，不许静默」）
- §2.3 `metrics_used[]` 快照形状
- §8.1 V3 要求定义本身也过只读静态检查

**实现现状（V4 / V5 / V8 的边界，不许含糊）**
- V4（定义里的列必须在 schema 快照里）**不在加载时判**：加载时还没有 schema，硬判只能猜。
  落地位置是 `check_metric_use()`，由 `ask.sqlgen` 在真正要用这条指标时调用（列不存在 →
  `COLUMN_NOT_FOUND`，带「最接近的列名」提示，不自动替换）。
- V5（定义实际引用的表集合 == `requires_tables`）**只判形状**（非空字符串数组）。理由：契约
  §3.1 的 `requires_tables` 是字典世界的表名（本仓库是 `sales`），而 §5.1 的 schema 快照里
  **没有表名**，物理表名来自 sheet 名——两者之间没有任何契约规定的映射。没有映射就不许猜，
  所以这里只判形状，把「集合相等」留作契约缺口上报（见 `DEFERRED_RULES`）。
- V8（同 id 同 version 的 definition 哈希一致）需要历史快照；本模块不读 `dict/` 下的其它文件，
  留 `history=` 形参，给未来调用方传入。
- V12（任一失败 → 整份字典加载失败，一次列全）在这里落地：`load_metric_dict` 收集**所有**
  问题后一次性抛 `DICT_LOAD_FAILED`，不 fail-fast、不跳过坏条目。

零依赖：只用标准库。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .decide import PROJECT_ROOT, CONTRACT_VERSION, DecisionError, sha256_text
from .static_check import StaticReject, check_sql, identifiers, tokenize

# 契约版本只留一处真源（`ask.decide.CONTRACT_VERSION`）：字典的 `contract_version` 要跟它比对，
# 各模块各写一个字面量迟早会飘。
DEFAULT_METRIC_DICT = "dict/metrics.json"

# §3.1 单位白名单（封闭集）
UNITS = frozenset({"CNY", "%", "人", "次", "个", "天", "ratio", "none"})

# §3.1 default_aggregation 封闭集
AGG_OPS = ("sum", "avg", "count", "count_distinct", "min", "max")

# §3.1 window 枚举
WINDOW_GRAINS = ("month", "quarter", "year")
WINDOW_COMPARES = ("yoy", "mom")

METRIC_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
NAME_MAX = 20

# 契约没写清楚、本实现只判形状或推迟的规则（报告里要照抄这三条）
DEFERRED_RULES = {
    "V4": "定义引用的列是否存在于 schema —— 推迟到 `check_metric_use()`（真正用这条指标时）",
    "V5": "定义引用的表集合 == requires_tables —— 只判形状；契约没有「字典表名 → 物理表名」的映射",
    "V8": "同 id 同 version 的 definition 哈希一致 —— 需要历史快照，本模块不读 dict/ 下其它文件",
}

_SQL_LIMIT_WORDS = ("limit",)


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Metric:
    """一条字典条目（§3.1 的全字段，一个都不省）。"""

    id: str
    name: str
    definition: str
    unit: str
    owner: str
    version: int
    requires_tables: tuple
    aliases: tuple
    time_column: str | None
    window: dict | None
    default_aggregation: str
    deprecated: bool

    @property
    def definition_sha256(self) -> str:
        return sha256_text(self.definition)

    def as_snapshot(self) -> dict:
        """§2.3 `metrics_used[]` 元素：**拷贝**定义，不是引用（字典改了旧答案仍可自证）。"""
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "definition": self.definition,
            "unit": self.unit,
            "owner": self.owner,
            "definition_sha256": self.definition_sha256,
        }

    def as_d1_dict(self) -> dict:
        """§5.1 D1 输入里的 `metric_dict[]` 形状：只有五个键。"""
        return {
            "id": self.id,
            "name": self.name,
            "definition": self.definition,
            "unit": self.unit,
            "version": self.version,
        }


class MetricDict:
    """一份已校验的字典。`metrics` 顺序 = 文件里的顺序（§3：按 id 字节序，加载时已校验）。"""

    def __init__(self, *, metrics, path: str, sha256: str) -> None:
        self.metrics = list(metrics)
        self.path = path
        self.sha256 = sha256
        self._by_id = {m.id: m for m in self.metrics}

    # --- 查询 ------------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self.metrics)

    def __iter__(self):
        return iter(self.metrics)

    def ids(self) -> list:
        return [m.id for m in self.metrics]

    def get(self, metric_id: str) -> Metric | None:
        return self._by_id.get(metric_id)

    def require(self, metric_id: str) -> Metric:
        metric = self.get(metric_id)
        if metric is None:
            raise DecisionError(
                "METRIC_NOT_IN_DICT",
                f"指标 id {metric_id!r} 不在字典里（字典有 {self.ids()}）",
                stage="D1",
                readable_hint="这个口径字典里没有；不猜，改为反问或补字典",
            )
        return metric

    def names(self) -> list:
        return [m.name for m in self.metrics]

    def d1_view(self) -> list:
        """§5.1 D1 输入的 `metric_dict` 数组。"""
        return [m.as_d1_dict() for m in self.metrics]

    def snapshots(self, ids) -> list:
        """§2.3 快照数组，按 `id` 字节序（§6.3.7）。"""
        out = []
        for mid in sorted({str(i) for i in ids or []}, key=lambda s: s.encode("utf-8")):
            metric = self.get(mid)
            if metric is not None:
                out.append(metric.as_snapshot())
        return out


# --------------------------------------------------------------------------- #
# 外层聚合函数（V11 用）
# --------------------------------------------------------------------------- #


def outer_aggregation(definition: str) -> str | None:
    """从定义里取**外层**聚合函数 → `AGG_OPS` 之一（认不出就是 None）。"""
    toks = [t for t in tokenize(definition) if t[0] in ("word", "punct")]
    words = [t for t in toks if t[0] == "word"]
    if not words:
        return None
    head = words[0][1].lower()
    if head == "count":
        if any(t[0] == "word" and t[1].lower() == "distinct" for t in toks):
            return "count_distinct"
        return "count"
    return {"sum": "sum", "total": "sum", "avg": "avg", "min": "min", "max": "max"}.get(head)


# --------------------------------------------------------------------------- #
# 加载 + 校验（V1–V3、V5–V7、V9–V12）
# --------------------------------------------------------------------------- #


def _as_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _check_metric(raw, index: int, seen_ids: dict, seen_names: dict, history=None) -> tuple:
    """→ `(Metric | None, [问题描述])`。每条问题的前缀写清是哪一条规则（便于一次改完）。"""
    problems: list = []

    def bad(rule: str, code: str, message: str) -> None:
        problems.append(f"[{rule}/{code}] metrics[{index}]: {message}")

    if not isinstance(raw, dict):
        bad("形状", "METRIC_INVALID", f"条目必须是对象，拿到 {type(raw).__name__}")
        return None, problems

    required = (
        "id", "name", "definition", "unit", "owner", "version",
        "requires_tables", "aliases", "time_column", "default_aggregation", "deprecated",
    )
    missing = [k for k in required if k not in raw]
    if missing:
        bad("形状", "METRIC_INVALID", f"缺必填字段 {missing}")
        return None, problems
    if "window" not in raw:
        bad("V10", "METRIC_INVALID", "缺 window 字段（无窗口类指标必须显式写 null）")

    mid = raw["id"]
    # ---- V1 id 形状 + 唯一 ----
    if not isinstance(mid, str) or not METRIC_ID_RE.match(mid):
        bad("V1", "METRIC_INVALID", f"id={mid!r} 不符合 ^[a-z][a-z0-9_]{{1,31}}$")
    elif mid in seen_ids:
        bad("V1", "METRIC_INVALID", f"id={mid!r} 重复（第 {seen_ids[mid]} 条已经用过）")
    else:
        seen_ids[mid] = index

    # ---- V2 name 非空、≤20、同一 name 只许一个 id ----
    name = raw["name"]
    if not isinstance(name, str) or not name.strip():
        bad("V2", "METRIC_INVALID", "name 不许为空")
    else:
        if len(name) > NAME_MAX:
            bad("V2", "METRIC_INVALID", f"name={name!r} 超过 {NAME_MAX} 字")
        if name in seen_names and seen_names[name] != mid:
            bad("V2", "METRIC_INVALID", f"「{name}」同时属于 id={seen_names[name]!r} 与 {mid!r}（中文匹配会歧义）")
        else:
            seen_names[name] = mid

    # ---- V3 definition 非空 + 过 §8.1 只读静态检查 ----
    definition = raw["definition"]
    if not isinstance(definition, str) or not definition.strip():
        bad("V3", "METRIC_INVALID", "definition 不许为空")
    else:
        try:
            check_sql(definition, tables=None, require_select=False, where=f"metrics[{index}].definition")
        except StaticReject as exc:
            bad("V3", "SQL_STATIC_REJECT", f"definition 没过静态检查：{exc.message}")

    # ---- V5 requires_tables 形状（集合相等见 DEFERRED_RULES）----
    if not _as_str_list(raw["requires_tables"]) or not raw["requires_tables"]:
        bad("V5", "METRIC_INVALID", "requires_tables 必须是非空字符串数组")

    # ---- V6 单位白名单 ----
    unit = raw["unit"]
    if unit not in UNITS:
        bad("V6", "METRIC_INVALID", f"unit={unit!r} 不在白名单 {sorted(UNITS)}")

    # ---- V7 owner 非空 ----
    owner = raw["owner"]
    if not isinstance(owner, str) or not owner.strip():
        bad("V7", "METRIC_INVALID", "owner 不许为空")

    # ---- V8、V9 版本与 LIMIT/ORDER BY ----
    version = raw["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        bad("V8", "METRIC_INVALID", f"version={version!r} 必须是 ≥1 的整数")
    elif history is not None:
        prev = (history.get(mid) or {}).get(str(version))
        if prev is not None and prev != sha256_text(str(definition)):
            bad("V8", "METRIC_INVALID", f"同 id={mid} 同 version={version} 的 definition 哈希变了（最危险的漂移）")

    low_words = [t[1].lower() for t in tokenize(str(definition or "")) if t[0] == "word"]
    if any(w in _SQL_LIMIT_WORDS for w in low_words):
        bad("V9", "METRIC_INVALID", "definition 里不许出现 LIMIT（截断属于查询层）")
    for i, w in enumerate(low_words[:-1]):
        if w == "order" and low_words[i + 1] == "by":
            bad("V9", "METRIC_INVALID", "definition 里不许出现 ORDER BY（指标只描述一个数）")

    # ---- V10 window ⟺ time_column ----
    window = raw.get("window")
    time_column = raw["time_column"]
    if time_column is not None and (not isinstance(time_column, str) or not time_column.strip()):
        bad("V10", "METRIC_INVALID", f"time_column={time_column!r} 必须是非空字符串或 null")
    if (window is None) != (time_column is None):
        bad("V10", "METRIC_INVALID", "window 非空 ⟺ time_column 非空（同比/环比比哪一期不许猜）")
    if window is not None:
        if not isinstance(window, dict) or set(window) != {"grain", "compare"}:
            bad("V10", "METRIC_INVALID", f"window 必须是 {{grain, compare}}，拿到 {window!r}")
        else:
            if window["grain"] not in WINDOW_GRAINS:
                bad("V10", "METRIC_INVALID", f"window.grain={window['grain']!r} 不在 {WINDOW_GRAINS}")
            if window["compare"] not in WINDOW_COMPARES:
                bad("V10", "METRIC_INVALID", f"window.compare={window['compare']!r} 不在 {WINDOW_COMPARES}")

    # ---- V11 default_aggregation 与定义外层聚合函数一致 ----
    agg = raw["default_aggregation"]
    if agg not in AGG_OPS:
        bad("V11", "METRIC_INVALID", f"default_aggregation={agg!r} 不在 {AGG_OPS}")
    else:
        actual = outer_aggregation(str(definition or ""))
        if actual is not None and actual != agg:
            bad("V11", "METRIC_INVALID", f"default_aggregation={agg!r} 与定义的外层聚合 {actual!r} 不一致")

    # ---- 形状（别名 / deprecated）----
    if not _as_str_list(raw["aliases"]):
        bad("形状", "METRIC_INVALID", "aliases 必须是字符串数组（可空）")
    if not isinstance(raw["deprecated"], bool):
        bad("形状", "METRIC_INVALID", "deprecated 必须是布尔")

    if problems:
        return None, problems
    metric = Metric(
        id=mid,
        name=name,
        definition=definition,
        unit=unit,
        owner=owner,
        version=version,
        requires_tables=tuple(raw["requires_tables"]),
        aliases=tuple(raw["aliases"]),
        time_column=time_column,
        window=window,
        default_aggregation=agg,
        deprecated=bool(raw["deprecated"]),
    )
    return metric, []


def load_metric_dict(path: str | Path | None = None, *, root: str | Path | None = None, history=None) -> MetricDict:
    """读并校验一份字典。**任何一条不合规 → 整份加载失败**（V12），不跳过坏条目。"""
    rel = str(path or DEFAULT_METRIC_DICT)
    base = Path(root) if root is not None else PROJECT_ROOT
    target = Path(rel)
    if not target.is_absolute():
        target = base / rel
    try:
        raw_bytes = target.read_bytes()
    except FileNotFoundError:
        raise DecisionError(
            "FILE_NOT_FOUND",
            f"指标字典不存在: {rel}",
            stage="ingest",
            readable_hint="字典路径写错了；`--metric-dict` 默认 dict/metrics.json",
        )
    except OSError as exc:
        raise DecisionError("FILE_PARSE_ERROR", f"指标字典读不了: {rel} / {exc}", stage="ingest")

    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecisionError("FILE_PARSE_ERROR", f"指标字典不是合法 UTF-8 JSON: {rel} / {exc}", stage="ingest")

    problems: list = []
    if not isinstance(raw, dict):
        problems.append("[形状/DICT_LOAD_FAILED] 顶层必须是对象")
        raw = {}
    if raw.get("contract_version") != CONTRACT_VERSION:
        problems.append(
            f"[形状/DICT_LOAD_FAILED] contract_version={raw.get('contract_version')!r}，当前契约 {CONTRACT_VERSION}"
        )
    items = raw.get("metrics")
    if not isinstance(items, list) or not items:
        problems.append("[形状/DICT_LOAD_FAILED] metrics 必须是非空数组")
        items = []

    ids = [item.get("id") for item in items if isinstance(item, dict)]
    if ids == sorted(ids, key=lambda s: str(s).encode("utf-8")) if ids else False:
        pass
    elif ids and ids != sorted(ids, key=lambda s: str(s).encode("utf-8")):
        problems.append("[形状/DICT_LOAD_FAILED] metrics 必须按 id 字节序排列（否则字典整体哈希不稳）")

    metrics: list = []
    seen_ids: dict = {}
    seen_names: dict = {}
    for index, item in enumerate(items):
        metric, item_problems = _check_metric(item, index, seen_ids, seen_names, history)
        problems.extend(item_problems)
        if metric is not None:
            metrics.append(metric)

    if problems:
        raise DecisionError(
            "DICT_LOAD_FAILED",
            f"指标字典 {rel} 未通过校验（{len(problems)} 条）：\n  " + "\n  ".join(problems),
            stage="ingest",
            readable_hint="字典里任何一条口径不合规都不许算数（§3.2 V12）：先改字典，再问数",
        )

    return MetricDict(metrics=metrics, path=rel, sha256=sha256_text(raw_bytes.decode("utf-8")))


# --------------------------------------------------------------------------- #
# V4：真正用这条指标时才校验列是否存在于当前表
# --------------------------------------------------------------------------- #


def metric_columns(metric: Metric) -> list:
    """定义里引用到的列名（去重、保持出现顺序）。"""
    return [i for i in identifiers(metric.definition) if i != "0"]


def check_metric_use(metric: Metric, table_columns) -> list:
    """V4：定义引用的每个列名都必须在当前表的列里。→ 通过过的列名列表；不过就抛 `COLUMN_NOT_FOUND`。

    带「最接近的列名」提示，但**不自动替换**（§3.2 V4）。
    """
    import difflib

    available = [str(c) for c in table_columns]
    used = metric_columns(metric)
    missing = [c for c in used if c not in available]
    if missing:
        hints = []
        for col in missing:
            close = difflib.get_close_matches(col, available, n=1, cutoff=0.5)
            hints.append(f"{col}（最接近的是 {close[0]}）" if close else f"{col}（没有相近的列）")
        raise DecisionError(
            "COLUMN_NOT_FOUND",
            f"指标 {metric.id} 的定义引用的列 {missing} 不在当前表里（表里有 {available}）",
            stage="D3",
            readable_hint="定义里的列在这份数据里找不到：" + "、".join(hints),
        )
    return used


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

USAGE = """ask.metric_dict · 指标字典加载与校验（契约 §3）

  python -m ask.metric_dict [PATH]        校验并打印条目摘要（默认 dict/metrics.json）
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
    try:
        mdict = load_metric_dict(argv[0])
    except DecisionError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        if exc.readable_hint:
            print(f"  {exc.readable_hint}", file=sys.stderr)
        return 1
    print(f"{mdict.path}  sha256={mdict.sha256[:16]}…  {len(mdict)} 条")
    for metric in mdict:
        print(
            f"  {metric.id:<18} v{metric.version}  {metric.name:<8} {metric.unit:<4} "
            f"{metric.default_aggregation:<14} {metric.definition}"
        )
    for rule, note in DEFERRED_RULES.items():
        print(f"  推迟: {rule} — {note}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
