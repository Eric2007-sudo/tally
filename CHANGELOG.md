# Changelog

格式按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

---

## [0.2.0] — 2026-10-27

### Added

- **多表 JOIN**（`ask/joins.py`）。D3 建「列 → 表」索引，D1 要的维度不在主表时按同名列推出等值键，
  生成 `joins[]`；sqlgen 按 `joins[]` 拼 `JOIN`，列名带表名限定。
  「哪些 X 没有 Y」这类反向条件走 `LEFT JOIN … WHERE 右表键 IS NULL`；一对多用 `JOIN_ROW_MULTIPLIED` 拒掉。
  效果：q031–q040 十道 JOIN 题 **0/10 → 10/10**。

### Fixed

- **时间过滤整段丢失**（`ask/sqlgen.py`）。`build_where` 的 `include_time` 写成
  `bool(source.anti and source.anti.get("time_in_outer"))`，对所有**非反连接**查询都算出 `False`。
  实测 54 道带 `time_range` 的题里，47 道生成的 SQL 一个日期字面量都没有；
  而判分器只看结构（SUM / GROUP BY / JOIN），不检查时间过滤，所以一直是绿的。
  改为 `_time_in_where()`：非反连接一律进 WHERE。修完 47 → 0。
- **自检脚本永远 `exit 0`**（`ask/_selfcheck/run_all.py`）。11 题不过，门禁照样绿。
  改为有失败即 `exit 1` —— 一个不会失败的自检等于没有自检。

### Docs

- README 重写为「实现了什么 / 如何使用 / 实测结果 / 参考资料」，并补上实测数字
  （此前只有验收标准，读者看不出 JOIN 是 0/10）。

---

## [0.1.0] — 2026-10-26

首个可用版本。

- 文件接入（`.xlsx` 用标准库 zip + XML 手解 / `.csv`）、schema 推断、中文列名匹配
- 指标字典 + V1–V12 校验器
- 决策四段（D1 问题理解 / D2 分析计划 / D3 选表选列 / D4 解读），全部可注入
- 问题 → 只读 SQL，静态检查拦截一切写操作
- 追溯链：SQL / 命中行 / 计算步骤 / 原始单元格
- 纯 SVG 图表、本地只读工作台、导出 `answer.json` / `report.md` / SVG
- 60 题离线题集与判据
