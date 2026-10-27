"""ask/ · 读文件 → schema 推断 → 决策四段 → SQL → 执行 → 追溯链 → answer.json

本包只依赖 Python 标准库（README「零依赖」）：
- `ask.ingest`：文件接入（`.xlsx` 用 zip + XML 解析，不落盘；`.csv`）→ 原始网格
- `ask.schema`：表头行推断 + 列类型推断（int/decimal/text/date/bool/null）→ schema 快照
- `ask.decide`：决策四段 D1–D4 + `--decide inject:` 注入 + 缓存键 + 中文列别名表
  （`ask/aliases.json`）
- `ask.metric_dict`：指标字典 §3 校验（V1–V12）→ 快照 / 定义列
- `ask.joins`：跨表列解析 + 等值 JOIN 路径（D3 `joins[]` / 反向条件 / 桥接表）
- `ask.sqlgen`：D2 步骤 + D3 选列 → 一条条只读 SELECT + 输出列元数据
- `ask.static_check`：§8.1 只读静态检查（S1–S6、单条 SELECT、表名/列名白名单）
- `ask.execute`：内存里只读执行 + 逐步追溯（行号 → `row_ids`）+ T1–T8 自检
- `ask.query`：串起全链路并按 §6.5 序列化落盘（`python -m ask.query`）

字段名、枚举、降级语义一律以 `contract/answer.md` 为准；本包不新造约定。
"""

__all__ = ["ingest", "schema", "decide", "metric_dict", "joins", "sqlgen", "static_check", "execute", "query"]
