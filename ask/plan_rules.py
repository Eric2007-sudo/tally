"""ask.plan_rules · 没有模型时，把「问题文本 + D1 口径」翻译成 D2 计划说不出来的那几件事

**为什么需要这个模块**

契约 §5.1 的 D2 输出只有 `needs{metric_ids,dimensions}` / `sql_hint` / `intent`：
它能表达「按哪些维度、用哪些指标汇总」，但表达不了四件事——

① 排名（从高到低 / 取前 N 条）：`plan[]` 里没有字段承载排序方向和截断条数；
② 时间粒度分组（每月 / 每天 / 按季度）：`D1.time_range.grain` 有这个信息，契约没写它要进 `GROUP BY`；
③ 窗口语义（环比同比）：指标字典里 `window{grain,compare}` 已经声明了口径，D2 计划没地方放；
④ 异常语义（明显低于当月平均 / 3 倍标准差 / 重复订单号）：需要 `LAG` / `AVG() OVER` / `HAVING`。

线上（配了模型回调）这些由 D2 判断；离线时 D2 走 §5.1 的兜底（最小计划），
于是 ask/ 自己按**写死的、可解释的规则**把这几件事补齐。本模块只读两样东西：
问题文本（事实，不是猜）和 D1 已经定下的口径（`metric_ids` / `dimensions` / `time_range`），
**绝不新增指标、不新增维度、不改 D1 的时间范围**。

**规则表（全部写死在这里；改了就是改口径，报告里要报）**

| 线索 | 得到的 SQL |
|---|---|
| 指标字典 `window{grain,compare}`（如 `gmv_mom`） | 按 grain 分组后用 `LAG(...)` 算环比/同比 |
| 「每月/每个月/各月/按月/逐月」且 D1 没给维度 | 多一个 `strftime('%Y-%m', 时间列)` 桶列并进 `GROUP BY` |
| 「每天/每日/按天/逐日」 | `strftime('%Y-%m-%d', 时间列)` |
| 「每季度/各季度/按季度/逐季」 | `strftime('%Y') ‖ '-Q' ‖ 季度序` |
| 「最高/最大/最多/从高到低/排名/前 N/TOP N」 | `ORDER BY 指标 DESC` |
| 「最低/最少/最小/从低到高/排名最末」 | `ORDER BY 指标 ASC` |
| 「前 3 / 最高的 10 个 / 20 笔」（数字后面不是 `%`） | `LIMIT n` |
| 「X 相比 Y」「同比上一期」且 D1 区间正好两个桶 | 两个 CTE 各算一次，再相减/相除（`WITH`） |
| 「第二季度相比第一季度」（季度写在问题里） | 按 D1 的年份取两个季度区间，各算一次再比 |
| 区间只含一个期 + 「环比/上月/同比」 | 本期 vs 上一期（`2025-06` 比 `2025-05`；同比错一年） |
| 字典 `window.compare` + D1 `time_range.grain == "year"` + 问题没点月份 | 按**年**分桶再 `LAG(1)`（D1 说的是年，按月分桶等于换了个问题） |
| 指标字典 `unit == "%"`（本身就是率） | 变化量给**百分点差**（再除以基期就成了「率的增长率」） |
| 「变化率/增长率/增幅/涨幅」 | 相对变化，单位 `%`；否则给绝对增量（沿用指标单位） |
| 「明显低于当月平均」 | 本期值 < 所属月均值 × 0.5（`AVG() OVER (PARTITION BY 月)`） |
| 「偏离整体均值 3 倍标准差以上」 | 方差判据：`AVG(x*x)-AVG(x)^2`（SQLite 没有 stddev） |
| 「异常偏高」 | 值 > 同组均值 × 1.5 |
| 「突然大幅下降」 | 值 < 自身均值 × 0.5 |
| 「突然放大」 | 值 > 自身均值 × 2 |
| 「异常波动」 | 与均值偏离超过 30% |
| 「偏离…超过 30%」 | `ABS(值 − 均值) > ABS(均值) × 30%` |
| 「重复的订单号」 | `GROUP BY … HAVING COUNT(*) > 1` |
| 「单笔订单金额」 | 单笔金额 = `SUM(amount) OVER (PARTITION BY order_id)`（不多穿一层 GROUP BY） |

阈值（0.5 / 1.5 / 2 / 30% / 3σ）是**本实现选的口径**，契约没规定 → 报告里必须报。

**边界**：这些只影响「怎么取数」，不新增 `metrics_used`、不改 `columns` 的口径来源；
规则没命中就返回空 `Shape`，SQL 与第 4 步完全一致（不会因为本模块就多出 DESC/LIMIT）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .metric_dict import Metric, MetricDict

GRAINS = ("month", "day", "quarter", "year")

# 显式粒度词 → grain
_GRAIN_WORDS = (
    ("month", ("每个月", "每月", "各月", "按月", "逐月", "月度")),
    ("day", ("每天", "每日", "按天", "各天", "逐日", "日度")),
    ("quarter", ("每季度", "每季", "各季度", "按季度", "逐季")),
)
_ALL_GRAIN_WORDS = tuple(word for _grain, words in _GRAIN_WORDS for word in words)

# 排名方向
_DESC_WORDS = ("最高", "最大", "最多", "从高到低", "降序", "排名前", "排名", "前几")
_ASC_WORDS = ("最低", "最少", "最小", "从低到高", "升序", "最末", "最差")

# 数字后面跟这些才算「取前 N 条」
_COUNT_UNITS = "个|笔|名|位|条|家|项|类|种|件|张|单"

_RE_PREV_N = re.compile(r"前\s*(\d+)(\s*%)?")
_RE_NUM_UNIT = re.compile(rf"(\d+)\s*(?:{_COUNT_UNITS})")
_RE_TOP = re.compile(r"top\s*(\d+)", re.IGNORECASE)
_RE_YEAR_MONTH = re.compile(r"(\d{1,2})\s*月")
_RE_QUARTER = re.compile(r"第\s*([一二三四1-4])\s*季度")
_QUARTER_NO = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}

# 问题里指「更细的时间粒度」的词：D1 说是「年」，问题却在说月，就得按月分桶
_FINE_GRAIN_WORDS = _ALL_GRAIN_WORDS + ("上月", "上一月", "本月", "当月", "这个月", "同期")


@dataclass
class Shape:
    """一步 SQL 的「D2 说不出来、但语义必需」的部分。全空 = 与最小计划一致。"""

    grain: str | None = None  # 时间桶：month / day / quarter / year
    order_desc: bool | None = None  # None = 不覆盖默认（全序升序）
    limit: int | None = None
    window_compare: str | None = None  # "mom" / "yoy"（来自指标字典 window.compare）
    compare: dict | None = None  # 两期对比：{prev_start, prev_end, cur_start, cur_end, mode}
    outlier: dict | None = None  # 异常判据，见 `derive_outlier`
    duplicate: bool = False
    notes: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (
            self.grain
            or self.order_desc is not None
            or self.limit is not None
            or self.window_compare
            or self.compare
            or self.outlier
            or self.duplicate
        )

    def summary(self) -> str:
        bits = []
        if self.grain:
            bits.append(f"按{self.grain}分组")
        if self.window_compare:
            bits.append("环比" if self.window_compare == "mom" else "同比")
        if self.compare:
            bits.append("两期对比")
        if self.outlier:
            bits.append(f"异常判据 {self.outlier['kind']}/{self.outlier['compare']}")
        if self.duplicate:
            bits.append("重复值检查")
        if self.order_desc is not None:
            bits.append("降序" if self.order_desc else "升序")
        if self.limit is not None:
            bits.append(f"取前 {self.limit} 条")
        return "、".join(bits)


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #


def _has_any(text: str, words) -> bool:
    return any(word in text for word in words)


def _named_month(question: str) -> int | None:
    """问题里点名的月份（`12 月` → 12）；没点名返回 None。"""
    found = None
    for match in _RE_YEAR_MONTH.finditer(question):
        found = int(match.group(1))
    if found is not None and 1 <= found <= 12:
        return found
    return None


def _spans_more_than_one(d1: dict, grain: str) -> bool:
    """D1 的时间范围是否跨了不止一个桶（跨了才值得加桶列）。"""
    time_range = d1.get("time_range") or {}
    start, end = time_range.get("start"), time_range.get("end")
    if not (start and end):
        return False
    return len(_buckets(start, end, grain)) > 1


def _shift_year(iso: str, years: int) -> str:
    """`2025-03-31` → `2024-03-31`（2/29 落到 2/28，宁可保守也不越界）。"""
    try:
        day = date.fromisoformat(iso)
    except ValueError:
        return iso
    try:
        return day.replace(year=day.year + years).isoformat()
    except ValueError:  # 2/29 → 2/28
        return day.replace(year=day.year + years, day=28).isoformat()


def _buckets(start: str, end: str, grain: str) -> list:
    """把 `[start, end]` 切成整桶 `[(桶首, 桶尾)]`；切不整齐就返回空（不猜）。"""
    try:
        lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return []
    if lo > hi:
        return []
    out: list = []
    if grain == "month":
        cursor = lo.replace(day=1)
        while cursor <= hi:
            nxt = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            out.append((cursor.isoformat(), (nxt - timedelta(days=1)).isoformat()))
            cursor = nxt
    elif grain == "quarter":
        cursor = lo.replace(month=((lo.month - 1) // 3) * 3 + 1, day=1)
        while cursor <= hi:
            month = cursor.month + 3
            year = cursor.year + (month - 1) // 12
            nxt = date(year, (month - 1) % 12 + 1, 1)
            out.append((cursor.isoformat(), (nxt - timedelta(days=1)).isoformat()))
            cursor = nxt
    elif grain == "day":
        cursor = lo
        while cursor <= hi:
            out.append((cursor.isoformat(), cursor.isoformat()))
            cursor += timedelta(days=1)
    elif grain == "year":
        cursor = lo.replace(month=1, day=1)
        while cursor <= hi:
            nxt = date(cursor.year + 1, 1, 1)
            out.append((cursor.isoformat(), (nxt - timedelta(days=1)).isoformat()))
            cursor = nxt
    return out


def _prev_period(start: str, end: str, grain: str) -> tuple | None:
    """上一期（同粒度、同长度）：`2025-06` → `2025-05`；`Q2` → `Q1`；`2025` → `2024`。"""
    if grain == "year":
        return _shift_year(start, -1), _shift_year(end, -1)
    prev_start, prev_end = None, None
    for _ in range(1):
        try:
            lo = date.fromisoformat(start)
        except ValueError:
            return None
        if grain == "month":
            first = lo.replace(day=1) - timedelta(days=1)
            prev_start = first.replace(day=1).isoformat()
            prev_end = first.isoformat()
        elif grain == "quarter":
            month = lo.month - 3
            year = lo.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            prev_start = date(year, month, 1).isoformat()
            nxt = date(year + (month // 12), month % 12 + 1, 1)
            prev_end = (nxt - timedelta(days=1)).isoformat()
        elif grain == "day":
            prev_start = prev_end = (lo - timedelta(days=1)).isoformat()
    return prev_start, prev_end


def _quarter_range(year: str, quarter: int) -> tuple:
    """`('2025', 4)` → `('2025-10-01', '2025-12-31')`。"""
    first = (quarter - 1) * 3 + 1
    start = date(int(year), first, 1)
    month_after = first + 3
    nxt = date(int(year) + (1 if month_after > 12 else 0), (month_after - 1) % 12 + 1, 1)
    return start.isoformat(), (nxt - timedelta(days=1)).isoformat()



def derive_grain(question: str, metrics: list, dimensions: list) -> str | None:
    """时间粒度：① 指标自带 `window.grain` 优先；② 否则看问题里的显式粒度词。

    D1 已经给了维度时不再凭「每个月」加桶（那是两步问题，§2.1 只有一份结果表）——
    例外是窗口/异常判据自己需要桶，由 `derive` 单独补。
    """
    for metric in metrics:
        window = getattr(metric, "window", None) or {}
        if window.get("grain") in GRAINS:
            return window["grain"]
    if dimensions:
        return None
    for grain, words in _GRAIN_WORDS:
        if _has_any(question, words):
            return grain
    return None


def derive_order(question: str) -> bool | None:
    """排名方向 → `True` 降序 / `False` 升序 / `None` 不覆盖。"""
    desc = _has_any(question, _DESC_WORDS)
    asc = _has_any(question, _ASC_WORDS)
    if desc and not asc:
        return True
    if asc and not desc:
        return False
    return None


def derive_limit(question: str) -> int | None:
    """截断条数：`前 3` / `最高的 10 个` / `TOP 5`。数字后面是 `%` 的一律不当条数（那是比例）。"""
    for match in _RE_PREV_N.finditer(question):
        if match.group(2):  # 「前 10%」是比例，不是条数
            continue
        value = int(match.group(1))
        if value > 0:
            return value
    for match in _RE_NUM_UNIT.finditer(question):
        value = int(match.group(1))
        if 0 < value <= 1000:
            return value
    match = _RE_TOP.search(question)
    if match:
        return int(match.group(1))
    return None


def derive_outlier(question: str, metric_ids: list) -> dict | None:
    """异常判据 → `{kind, compare, partition, …}`（阈值是本实现的口径）。"""
    if "重复" in question:
        return {"kind": "duplicate", "compare": "none", "partition": "none"}
    if "标准差" in question:
        return {
            "kind": "variance",
            "compare": "abs",
            "sigma": 3.0,
            "partition": "none",
            "row_level": "order_amount" in metric_ids,
        }
    if "明显低于" in question and "平均" in question:
        return {"kind": "mean", "compare": "below", "factor": 0.5, "partition": "month"}
    if "自身" in question and "平均" in question:
        return {"kind": "mean", "compare": "above", "factor": 5.0, "partition": "dim",
                "row_level": "order_amount" in metric_ids}
    if "偏离" in question and "%" in question:
        return {"kind": "mean", "compare": "abs", "tolerance": 0.3, "partition": "dim"}
    if "偏离" in question:
        return {"kind": "mean", "compare": "abs", "tolerance": 0.5, "partition": "dim"}
    if "突然" in question and ("下降" in question or "下滑" in question or "下跌" in question):
        return {"kind": "mean", "compare": "below", "factor": 0.5, "partition": "dim"}
    if "突然" in question and ("放大" in question or "上升" in question or "上涨" in question):
        return {"kind": "mean", "compare": "above", "factor": 1.5, "partition": "dim"}
    if "异常" in question:
        return {"kind": "mean", "compare": "abs", "tolerance": 0.3, "partition": "none"}
    return None


def derive_compare(
    question: str,
    d1: dict,
    window_compare: str | None,
    dimensions: list,
    unit: str | None = None,
) -> dict | None:
    """两期对比 → `{prev_start, prev_end, cur_start, cur_end, mode}`；表达不了返回 None。

    `mode` 三选一（都是「变化了多少」的自然读法，本实现的口径）：
    - `rate`：**相对**变化 `(本期-上期)/上期 × 100`，单位 %；
    - `pp`：指标本身就是率（字典 `unit == "%"`）→ **百分点差** `本期-上期`，不除以基期；
    - `diff`：绝对增量 `本期-上期`，沿用指标自身单位。
    """
    if dimensions:
        return None
    if _has_any(question, _ALL_GRAIN_WORDS):  # 「每个月」是逐期窗口，不是两期对比
        return None
    time_range = d1.get("time_range") or {}
    start, end = time_range.get("start"), time_range.get("end")
    if not (start and end):
        return None
    grain = time_range.get("grain")
    grain = grain if grain in GRAINS else "month"

    def mode() -> str:
        if unit == "%":
            return "pp"
        if _has_any(question, ("变化率", "增长率", "增幅", "涨幅", "率是")):
            return "rate"
        return "diff"

    if window_compare == "yoy" and grain == "year":
        return {
            "prev_start": _shift_year(start, -1),
            "prev_end": _shift_year(end, -1),
            "cur_start": start,
            "cur_end": end,
            "mode": mode(),
            "why": "年度同比：本期区间 vs 去年同期",
        }
    # 「第四季度相比第三季度」：季度是写在问题里的，D1 的区间往往覆盖整年，不能靠切桶猜
    quarters = _RE_QUARTER.findall(question)
    if len(quarters) >= 2 and _has_any(question, ("相比", "对比", "比上", "与")):
        year = start[:4]
        cur_start, cur_end = _quarter_range(year, _QUARTER_NO[quarters[0]])
        prev_start, prev_end = _quarter_range(year, _QUARTER_NO[quarters[1]])
        return {
            "prev_start": prev_start,
            "prev_end": prev_end,
            "cur_start": cur_start,
            "cur_end": cur_end,
            "mode": mode(),
            "why": "问题点名了两个季度，各算一次再比",
        }
    if not _has_any(question, ("相比", "对比", "比上", "环比", "同比")):
        return None
    buckets = _buckets(start, end, grain)
    if len(buckets) == 2:
        (prev_start, prev_end), (cur_start, cur_end) = buckets
        return {
            "prev_start": prev_start,
            "prev_end": prev_end,
            "cur_start": cur_start,
            "cur_end": cur_end,
            "mode": mode(),
            "why": "问题点名了两个相邻时间段，各算一次再比",
        }
    if len(buckets) == 1:
        # 区间只覆盖一个期，问题又说「环比/同比」→ 上一期在区间之外（取数时单独算一次）
        prev = _prev_period(start, end, "year" if window_compare == "yoy" else grain)
        if prev is None:
            return None
        return {
            "prev_start": prev[0],
            "prev_end": prev[1],
            "cur_start": start,
            "cur_end": end,
            "mode": mode(),
            "why": "区间只含一个期，基期按问题里的环比/同比往前取一期",
        }
    return None


def derive(question: str, d1: dict, metrics: MetricDict, metric_ids: list, dimensions: list) -> Shape:
    """问题 + D1 → `Shape`。任何一条规则没命中就留空，绝不「猜一个」。"""
    shape = Shape()
    objs: list[Metric] = []
    for mid in metric_ids:
        try:
            objs.append(metrics.require(mid))
        except Exception:  # noqa: BLE001 - 不在字典里的话上游已经反问过，这里不该发生
            continue

    for metric in objs:
        window = getattr(metric, "window", None) or {}
        if window.get("compare") in ("mom", "yoy"):
            shape.window_compare = window["compare"]
            break

    outlier = derive_outlier(question, metric_ids)
    if outlier is not None:
        if outlier["kind"] == "duplicate":
            shape.duplicate = True
        else:
            shape.outlier = outlier

    shape.compare = derive_compare(
        question, d1, shape.window_compare, dimensions, objs[0].unit if objs else None
    )
    shape.grain = derive_grain(question, objs, dimensions)

    if shape.compare is None and shape.window_compare and shape.grain is None:
        shape.grain = "month"  # 环比/同比的最小单位就是月（字典 window.grain 已给，兜一层）
    if shape.compare is None and shape.window_compare and not _has_any(question, _FINE_GRAIN_WORDS):
        # 指标只声明「月度粒度」，但 D1 说的是「2025 年」、问题也没点月份 → 按年比才贴合问题
        if (d1.get("time_range") or {}).get("grain") == "year":
            shape.grain = "year"
    if shape.outlier is not None and shape.grain is None:
        time_grain = (d1.get("time_range") or {}).get("grain")
        named_month = _named_month(question)
        if shape.outlier["partition"] == "month" or _has_any(question, ("月度", "当月", "每月")):
            shape.grain = "month"  # 「当月/月度均值」得先有月桶
        elif named_month is not None:
            shape.grain = "month"  # 「12 月」这种点名月份也要一个桶
        elif not dimensions and time_grain in GRAINS:
            shape.grain = time_grain  # 没给维度时，桶就是 D1 自己声明的粒度
    if shape.grain is not None and not shape.window_compare and not _spans_more_than_one(d1, shape.grain):
        # D1 的范围本来就在一个桶里（例如就是 12 月）→ 再分桶没有任何信息量
        shape.grain = None
    if shape.outlier is not None and shape.grain == "month":
        month = _named_month(question)
        time_range = d1.get("time_range") or {}
        if month is not None and _spans_more_than_one(d1, "month"):
            # 区间跨多个月、问题又点名了某个月 → 只看那个月（区间本来就只含它时不必再过滤）
            year = (time_range.get("start") or "")[:4] or str(date.today().year)
            shape.outlier["month_filter"] = f"{year}-{month:02d}"

    shape.order_desc = derive_order(question)
    shape.limit = derive_limit(question)
    if shape.limit is not None and shape.order_desc is None:
        shape.limit = None  # 只说了条数没说方向 → 不截断，免得按错的顺序砍掉答案
    if shape.order_desc is not None and (shape.outlier or shape.window_compare or shape.compare):
        shape.order_desc = None  # 这几类题的行序由判据决定，不按大小排名
        shape.limit = None
    if shape.order_desc is not None and not dimensions and shape.grain is None:
        shape.order_desc = None  # 单行结果谈不上排名
        shape.limit = None
    if shape.duplicate:
        shape.order_desc = None
        shape.limit = None

    shape.notes.append(shape.summary())
    return shape
