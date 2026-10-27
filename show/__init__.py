"""show/ —— 只吃 answer.json 的渲染层（图表 + 追溯面板 + 指标字典视图的落点）。

本包不 import ask/、不读源表格、不重算任何数字：入口是 show/render.py（渲染成 HTML）、
show/export.py（导出 answer.json / report.md / SVG + 字典提案）、show/server.py（只读工作台）。
"""

__all__ = ["render", "export", "server", "svg"]
