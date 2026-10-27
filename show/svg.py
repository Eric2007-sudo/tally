"""show/svg.py —— 纯 SVG 图表绘制（柱 / 折线 / 散点），零依赖、零随机、零脚本。

本模块只做一件事：把「已经排好序的点」画成像素。**不计算任何统计量**，
唯一的算术是「数据值 → 像素坐标」的线性映射（像素坐标不是数据数字，
所以这里的浮点运算不违反 contract/answer.md 第 6.2 节对数据数字的规定）。

调用约定：`values` 是 Decimal（或 int）原样取自 answer.json 的 `rows`；
`labels` 是**调用方已经按 columns[].digits / unit 格式化好的显示串**
（数字格式化的唯一真源在 show/render.py，本模块不重复实现，避免两处不一致）。

输出约束（contract 第 8.4 节）：不含 `<script>`、不含 `on*` 事件属性、
不含 `javascript:` URL —— 本模块只产出 `rect` / `line` / `path` / `circle` /
`text` 与纯色属性，所有文本一律经 html.escape。
"""

from __future__ import annotations

import html
import math
from decimal import Decimal

# 画布尺寸（viewBox 坐标系；页面上按宽度缩放，不放大也不裁切）
WIDTH = 760
HEIGHT = 380
PAD_L = 88
PAD_R = 32
PAD_T = 64
PAD_B = 76

# 固定配色，按序列下标取，不用随机色
PALETTE = ("#2f6fed", "#e0592b", "#2a9d5c", "#7c3aed", "#b45309")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def esc(text: object) -> str:
    """SVG/HTML 文本转义（含引号）。"""
    return html.escape("" if text is None else str(text), quote=True)


def _f(value: float) -> str:
    """像素坐标输出：定点两位小数，永不出现科学计数法，不出现 -0.00。"""
    out = f"{value:.2f}"
    if out.startswith("-") and float(out) == 0.0:
        out = out[1:]
    return out


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clip(text: object, limit: int = 12) -> str:
    """轴标签过长时截断（显示层的取舍，不改数据）。"""
    out = "" if text is None else str(text)
    if limit <= 0 or len(out) <= limit:
        return out
    return out[: limit - 1] + "…"


def fmt_tick(value: float) -> str:
    """刻度文本：整数不带小数点，其余两位小数，千分位分隔。"""
    if abs(value) < 1e-9:
        return "0"
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.2f}"


def nice_step(span: float, target: int = 5) -> float:
    if span <= 0:
        return 1.0
    rough = span / max(1, target)
    magnitude = 10.0 ** math.floor(math.log10(rough))
    for factor in (1.0, 2.0, 2.5, 5.0, 10.0):
        if rough <= factor * magnitude + 1e-12:
            return factor * magnitude
    return 10.0 * magnitude


def axis_values(values) -> tuple[float, float, list[float]]:
    """给出 (下界, 上界, 刻度列表)。有数据时把 0 包进范围，柱状图才有基线。"""
    nums = [v for v in (_num(x) for x in values) if v is not None]
    if not nums:
        return 0.0, 1.0, [0.0, 0.5, 1.0]
    lo = min(min(nums), 0.0)
    hi = max(max(nums), 0.0)
    if hi - lo <= 1e-12:
        hi = lo + 1.0
    step = nice_step(hi - lo, 5)
    lo = math.floor(lo / step + 1e-12) * step
    hi = math.ceil(hi / step - 1e-12) * step
    ticks: list[float] = []
    tick = lo
    guard = 0
    while tick <= hi + step / 2 and guard < 64:
        ticks.append(round(tick, 10))
        tick += step
        guard += 1
    return lo, hi, ticks


# --------------------------------------------------------------------------
# 图元
# --------------------------------------------------------------------------


def _text(
    x: float,
    y: float,
    content: object,
    *,
    size: int = 12,
    anchor: str = "middle",
    fill: str = "#334155",
    weight: str | None = None,
    rotate: float | None = None,
) -> str:
    attrs = (
        f'x="{_f(x)}" y="{_f(y)}" font-size="{size}" '
        f'text-anchor="{anchor}" fill="{fill}"'
    )
    if weight:
        attrs += f' font-weight="{weight}"'
    if rotate is not None:
        attrs += f' transform="rotate({_f(rotate)} {_f(x)} {_f(y)})"'
    return f"<text {attrs}>{esc(content)}</text>"


def _wrap(title: str, parts: list[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="100%" role="img" aria-label="{esc(title)}" '
        f'style="max-width:100%;height:auto;display:block">'
        f"<title>{esc(title)}</title>"
        f'<rect x="0" y="0" width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>'
        + "".join(parts)
        + "</svg>"
    )


def _plot_box() -> dict:
    return {"l": PAD_L, "r": WIDTH - PAD_R, "t": PAD_T, "b": HEIGHT - PAD_B}


def _ymap(value: float, lo: float, hi: float, plot: dict) -> float:
    if hi - lo <= 1e-12:
        return plot["b"]
    return plot["b"] - (value - lo) / (hi - lo) * (plot["b"] - plot["t"])


def _grid(plot: dict, lo: float, hi: float, ticks: list[float], y_label: str) -> list[str]:
    parts = [
        f'<line x1="{_f(plot["l"])}" y1="{_f(plot["b"])}" x2="{_f(plot["r"])}" '
        f'y2="{_f(plot["b"])}" stroke="#94a3b8" stroke-width="1"/>'
    ]
    for tick in ticks:
        y = _ymap(tick, lo, hi, plot)
        parts.append(
            f'<line x1="{_f(plot["l"])}" y1="{_f(y)}" x2="{_f(plot["r"])}" '
            f'y2="{_f(y)}" stroke="#e2e8f0" stroke-width="1"/>'
        )
        parts.append(_text(plot["l"] - 12, y + 4, fmt_tick(tick), size=11, anchor="end", fill="#64748b"))
    if lo < 0 < hi:
        y0 = _ymap(0.0, lo, hi, plot)
        parts.append(
            f'<line x1="{_f(plot["l"])}" y1="{_f(y0)}" x2="{_f(plot["r"])}" '
            f'y2="{_f(y0)}" stroke="#94a3b8" stroke-width="1" stroke-dasharray="4 3"/>'
        )
    if y_label:
        parts.append(
            _text(18, (plot["t"] + plot["b"]) / 2, y_label, size=12, anchor="middle", rotate=-90)
        )
    return parts


def _x_axis(plot: dict, categories: list[str], band: float, x_label: str) -> list[str]:
    parts: list[str] = []
    rotate = len(categories) > 8
    for index, category in enumerate(categories):
        cx = plot["l"] + band * (index + 0.5)
        if rotate:
            parts.append(
                _text(cx, plot["b"] + 16, clip(category, 10), size=11, anchor="end",
                      fill="#475569", rotate=-30)
            )
        else:
            parts.append(_text(cx, plot["b"] + 20, clip(category, 12), size=11, fill="#475569"))
    if x_label:
        parts.append(_text((plot["l"] + plot["r"]) / 2, HEIGHT - 18, x_label, size=12))
    return parts


def _legend(series: list[dict], plot: dict) -> list[str]:
    """多序列才画图例；单序列时 y 轴标题已经说明它是什么，不重复。"""
    if len(series) < 2:
        return []
    parts: list[str] = []
    x = plot["l"]
    y = 34
    for index, item in enumerate(series):
        color = PALETTE[index % len(PALETTE)]
        parts.append(
            f'<rect x="{_f(x)}" y="{_f(y - 9)}" width="10" height="10" rx="2" '
            f'fill="{color}" fill-opacity="0.88"/>'
        )
        label = item.get("label") or ""
        parts.append(_text(x + 16, y, label, size=12, anchor="start"))
        x += 16 + max(24.0, 13.0 * len(str(label))) + 20
    return parts


def _series_color(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


def _pick(item: dict, key: str, index: int):
    seq = item.get(key) or []
    return seq[index] if index < len(seq) else None


# --------------------------------------------------------------------------
# 三种图
# --------------------------------------------------------------------------


def bar_chart(*, title: str, categories: list[str], series: list[dict],
              x_label: str = "", y_label: str = "") -> str:
    """柱状图。`categories` 是 x 轴标签，`series[*].values` 与之等长对齐。"""
    plot = _plot_box()
    lo, hi, ticks = axis_values([v for item in series for v in (item.get("values") or [])])
    parts = _legend(series, plot)
    parts += _grid(plot, lo, hi, ticks, y_label)

    count = max(1, len(categories))
    band = (plot["r"] - plot["l"]) / count
    group = band * 0.66
    bars = max(1, len(series))
    bar_w = group / bars
    baseline = _ymap(0.0, lo, hi, plot)
    show_labels = count * bars <= 24

    for index in range(count):
        cx = plot["l"] + band * (index + 0.5)
        for si, item in enumerate(series):
            value = _num(_pick(item, "values", index))
            if value is None:
                continue
            y = _ymap(value, lo, hi, plot)
            top = min(y, baseline)
            height = max(abs(baseline - y), 1.0)
            x = cx - group / 2 + bar_w * si + bar_w * 0.08
            width = bar_w * 0.84
            parts.append(
                f'<rect x="{_f(x)}" y="{_f(top)}" width="{_f(width)}" height="{_f(height)}" '
                f'rx="2" fill="{_series_color(si)}" fill-opacity="0.88"/>'
            )
            if show_labels:
                label = _pick(item, "labels", index)
                if label is not None:
                    ly = top - 6 if value >= 0 else top + height + 14
                    parts.append(_text(x + width / 2, ly, label, size=11, fill="#475569"))

    parts += _x_axis(plot, categories, band, x_label)
    return _wrap(title, parts)


def line_chart(*, title: str, categories: list[str], series: list[dict],
               x_label: str = "", y_label: str = "") -> str:
    """折线图。缺失点（null / 无 trace）不连线，只跳过 —— 不许插值。"""
    plot = _plot_box()
    lo, hi, ticks = axis_values([v for item in series for v in (item.get("values") or [])])
    parts = _legend(series, plot)
    parts += _grid(plot, lo, hi, ticks, y_label)

    count = max(1, len(categories))
    band = (plot["r"] - plot["l"]) / count

    for si, item in enumerate(series):
        color = _series_color(si)
        run: list[tuple[float, float]] = []
        runs: list[list[tuple[float, float]]] = []
        for index in range(count):
            value = _num(_pick(item, "values", index))
            if value is None:
                if run:
                    runs.append(run)
                    run = []
                continue
            run.append((plot["l"] + band * (index + 0.5), _ymap(value, lo, hi, plot)))
        if run:
            runs.append(run)

        for segment in runs:
            if len(segment) < 2:
                continue
            path = "M " + " L ".join(f"{_f(px)} {_f(py)}" for px, py in segment)
            parts.append(
                f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" '
                f'stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for index in range(count):
            value = _num(_pick(item, "values", index))
            if value is None:
                continue
            px = plot["l"] + band * (index + 0.5)
            py = _ymap(value, lo, hi, plot)
            parts.append(
                f'<circle cx="{_f(px)}" cy="{_f(py)}" r="4" fill="{color}">'
                f"<title>{esc(categories[index] if index < len(categories) else '')} · "
                f"{esc(_pick(item, 'labels', index) or '')}</title></circle>"
            )

    parts += _x_axis(plot, categories, band, x_label)
    return _wrap(title, parts)


def scatter_chart(*, title: str, points: list[dict],
                  x_label: str = "", y_label: str = "") -> str:
    """散点图。`points[*]` 需含 `x` / `y`（数值）与 `x_text` / `y_text`（显示串）。"""
    plot = _plot_box()
    usable = []
    for point in points:
        px_value = _num(point.get("x"))
        py_value = _num(point.get("y"))
        if px_value is None or py_value is None:
            continue
        usable.append((point, px_value, py_value))
    x_lo, x_hi, x_ticks = axis_values([item[1] for item in usable])
    y_lo, y_hi, y_ticks = axis_values([item[2] for item in usable])
    parts = _grid(plot, y_lo, y_hi, y_ticks, y_label)

    for tick in x_ticks:
        px = plot["l"] + (tick - x_lo) / max(x_hi - x_lo, 1e-12) * (plot["r"] - plot["l"])
        parts.append(
            f'<line x1="{_f(px)}" y1="{_f(plot["t"])}" x2="{_f(px)}" '
            f'y2="{_f(plot["b"])}" stroke="#f1f5f9" stroke-width="1"/>'
        )
        parts.append(_text(px, plot["b"] + 20, fmt_tick(tick), size=11, fill="#475569"))

    for point, px_value, py_value in usable:
        px = plot["l"] + (px_value - x_lo) / max(x_hi - x_lo, 1e-12) * (plot["r"] - plot["l"])
        py = _ymap(py_value, y_lo, y_hi, plot)
        parts.append(
            f'<circle cx="{_f(px)}" cy="{_f(py)}" r="5" fill="{_series_color(0)}" '
            f'fill-opacity="0.75" stroke="#ffffff" stroke-width="1">'
            f"<title>{esc(point.get('label') or '')}</title></circle>"
        )

    if x_label:
        parts.append(_text((plot["l"] + plot["r"]) / 2, HEIGHT - 18, x_label, size=12))
    return _wrap(title, parts)
