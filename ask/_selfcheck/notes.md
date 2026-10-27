# 第 4 步返工 · 自测记录与「等契约补」

本轮只改 `ask/`。契约 `contract/answer.md` 是唯一事实来源，凡是契约没写、由本实现选定的口径，
都记在这里（用户要求：契约表达不了的记「等契约补」，不自己发明）。

自测脚本 `ask/_selfcheck/run_all.py`：只读 `eval/`，产物落 `ask/_selfcheck/out/`，
按 `eval/metrics.py` 的 `judge_question` / `metric_hit_tiers` / `trace_completeness` 判分。

**结果：60 题 45 题 PASS（非 JOIN 50 题里 45 题 PASS = 90%）**；剩下的 15 题在下面第 5、6 条说明原因。

---

## 1. 契约缺口：等契约补

| # | 缺口 | 本实现怎么绕 | 想要契约补什么 |
|---|---|---|---|
| 1 | `filter.op` 枚举里没有 `IS NULL` / `IS NOT NULL`（§5.1 D1 `filters[].op` 只有 `= != > >= < <= in like`） | 用 `op="="` + `value=null` 表示「为空」，SQL 生成 `"amount" IS NULL`；`op` 字段仍照实写 `"="` | 加 `is_null` / `is_not_null` 两个 op |
| 2 | `time_range.grain`（`day/month/quarter/year`）在契约里只是「时间范围的粒度」，没规定它要进 `GROUP BY` | 「按时间粒度分组」由 `ask/plan_rules.py` 自己按问题粒度词 + `time_range.grain` 决定要不要加 `strftime()` 桶列 | §5.1 明确 `grain` 是否参与分组，或让 D2 输出 `group_by_grain` |
| 3 | `plan[]` 只有 `needs{metric_ids,dimensions}` / `sql_hint` / `intent`，没有「排序方向」「截断条数」「窗口」「异常判据」 | 全部落在 `ask/plan_rules.py` 的规则表（见该模块 docstring）；规则没命中就返回空 `Shape`，SQL 与最小计划完全一致 | D2 加 `order_by` / `limit` / `window` / `anomaly` 字段 |
| 4 | 两期对比（「第四季度相比第三季度」「6 月环比上月」）`time_range` 是**一个**区间，表达不了「比谁」 | 生成两个 CTE（本期一次、基期一次）再相减/相除，SQL 带 `WITH` | §5.1 加 `compare_to` / 允许两个 `time_range` |
| 5 | 窗口指标（字典 `window{grain,compare}`）的**基期必然在 D1 区间之外**（2025 年环比要 2024-12 的数） | 只把**取数**范围起点往前挪一期（mom 挪 1 月、yoy 挪 12 月），输出行、`time_range`、列仍只落在 D1 声明区间内；挪出来的那一期没有基期，被 `基期 IS NOT NULL AND 基期 <> 0` 挡掉 | 契约明确「窗口指标允许读取 `time_range` 之外的一期」 |
| 6 | `trace.row_ids` 与「计数为 0」冲突：§4.3 T3 要求 `row_ids` 非空，但 0 条命中行没有任何行标识 | `ask/execute.py::check_traces` 只放行 `row_ids_total == 0` 这一种（q058「是否存在金额为负的订单」= 0 条）；`eval/metrics.py` 的 T3 仍判这条 trace 不合契约 | T3 补「聚合结果为空集时 `row_ids` 可为空，但 `row_ids_total` 必须是 0」 |
| 7 | 输出里的**空值数字格**要不要发 trace：§4.1 的 `N` 定义是「`rows` 里 int/decimal 单元格数」，没排除 `null`；§2.6 又要求 `value` 与单元格严格相等 | 空值**不发** trace（`null` 谈不上“相等”），并保证生成的 SQL 不产出空值数字格（窗口题过滤掉算不出的行） | §4.1 明确「`N` = 非空单元格数」 |
| 8 | 多表连接：§5.1 D1 的 `dimensions` 只有列名，没有「哪张表的列」，跨表维度无法表达（q031–q040 共 10 题，`expect.join == true`） | 不碰。D3 见两张表都有能对上语义的列时提问「用哪份数据」（`status="clarify"`） | D1/D2 加 `table` 限定或 `joins` |
| 9 | 契约没写「已经没有任何数据能算出一行时怎么回」 | 照 §7.4 不重查、不放宽条件，返回 0 行 + `caveats: RESULT_EMPTY` | 契约已足够，仅记录行为 |

## 2. 本实现选定的口径（报告里要报）

**异常/偏离阈值**（`ask/plan_rules.py` 里写死，契约没有数值）：

| 问题里的说法 | 判据 |
|---|---|
| 突然放大 / 突然上升 | `值 > 自身/同组均值 × 1.5` |
| 突然下降 / 大幅下降 | `值 < 均值 × 0.5` |
| 明显高于 / 偏高 | `值 > 均值 × 2` |
| 明显低于 | `值 < 均值 × 0.5` |
| 异常波动（无其它限定） | 与均值偏离超过 30% |
| 问题自己给数字（`超过 30%` / `5 倍` / `3σ`） | 用问题给的数，不套上面的默认值 |

**变化量口径**（`derive_compare`）：指标字典 `unit == "%"`（本身就是率，如退货率）→ 给**百分点差**；
问题问「变化率/增长率/增幅/涨幅」→ 给相对变化（单位 `%`）；否则给绝对增量（沿用指标单位）。

**时间桶**：字典 `window.grain` 优先；但 D1 的 `time_range.grain` 是 `year` 且问题没点月份时按**年**分桶
（`2025 年各区域的成交额同比` 是「今年 vs 去年」，不是 12 个月的同比）；D1 区间只落在一个桶里时不加桶列。

**排名/截断**：「最高/最多/从高到低」→ `ORDER BY 指标 DESC`；「前 N 个/笔/家」→ `LIMIT N`；
方向与条数都来自问题文本，`plan[]` 里无处存放（缺口 3）。

## 3. 追溯链：一个 eval 判据的 bug（只记，不改 eval/）

`eval/metrics.py` 的 T8（L363–399）在 `trace.value == null` 或单元格是 `null` 时，
走 `a is None or b is None` 分支设 `res["T8"] = False`，但 L388 的 `if equal and ...`
缩进在 `else` 之外，此时 `equal` **未绑定** → `UnboundLocalError`，整个评测脚本会崩。

所以本实现**绝不给空值格发 trace**（见缺口 7）：`cell` 为 `null` 时不出 trace，
且生成的 SQL 主动排除算不出的行（窗口题 `上一期 IS NOT NULL AND 上一期 <> 0`）。
当前 60 题自测合计 188 个非空数字格、188 条 trace，`missing_paths` 全空。

## 4. 空值筛选（q009）

`amount IS NULL` 走 `filter.op="="` + `value=null`（缺口 1）。q047「单笔订单金额最高的 20 单」
里的空值靠 `ORDER BY order_amount DESC` 把 `NULL` 排到末尾自然排除，不需要额外造条件。

## 5. 数据现实造成的 5 题空结果（SQL 是对的，这份数据回答不了）

| 题 | 问题 | 为什么是 0 行 |
|---|---|---|
| q025 | 2024 年每个月的成交额同比 | 同比要 2023 年数据，`sales.csv` 最早是 2024-01-01 |
| q053 | 哪些区域/月份的退货率明显偏高 | 字典 `refund_rate` 定义是 `SUM(refund_amount) / SUM(amount)`，SQLite 整除恒 0，没有「偏高」 |
| q056 | 单笔金额超过自身均值的 5 倍 | 实测单笔/均值最大比 1.64，没有 5 倍尖峰 |
| q057 | 订单笔数与自身月度均值偏离超 30% | 每区域每月恒 3 单，偏离 0 |
| q060 | 2025 年 12 月各区域成交额异常波动 | 12 月只有 R4 一行（且未成交），没有可比对象 |

这几题**不放宽条件重查**（§7.4），返回 0 行 + `RESULT_EMPTY`。判据里「应为非空结果」必然失败，
这是数据限制，不是 SQL 写错。

## 6. 多表连接的 10 题（q031–q040）

本轮（#12）已做完：`ask/joins.py` 负责「列 → 表 → 等值键 → 连接路径」，`ask/sqlgen.py`
按它拼 JOIN，`ask/decide.py::fallback_d3` 把同一份推导写进 D3 的 `joins[]`。

**写法**：JOIN 后各表需要的列先在子查询里投影成同名
（`(SELECT "sales"."amount" AS "amount", "region_dim"."region_name" AS "region_name", CAST("sales"."__src_rowid" AS TEXT) AS "__src_rowid" FROM "sales" INNER JOIN "region_dim" ON "sales"."region" = "region_dim"."region") AS "__join"`），
外层仍按裸列名写 SQL——这样指标定义能**逐字**出现在 `sql[]` 里（§4.3 T7 要求子串）。

**本实现自己定的口径（契约没写）**

| # | 事 | 规则 |
|---|---|---|
| 15 | 主表怎么选 | 含全部指标定义列的表里：优先字典 `requires_tables`，其次行数多的（事实表），最后按 D3 的表顺序 |
| 16 | 维度取哪张表 | 主表里也有同名列时，若另有「右表键唯一（多对一）、行数更少」的维表，取维表那一列（维表优先于反规范化列）；单表题永不因此连表 |
| 17 | 一对多 | 右表键不唯一（会放大行数）就**不连**，继续找别的同名列；全不唯一 → `COLUMN_NOT_FOUND`。运行期另有 `JOIN_ROW_MULTIPLIED` 兜底 caveat（§2.9） |
| 18 | 反向条件 | 「没有/没有任何/从未/不存在」→ `LEFT JOIN … WHERE 右表键 IS NULL`。时间列在**右侧**（q033）落进 `ON`；在**主表**（q038）留在外层 `WHERE`——放错边会把该保留的行滤掉/把不该留的行保下来 |
| 19 | 桥接表（q039） | `sales` 与 `brand_dim` 没有同名列 → 允许经同目录的 `product_dim` 一跳桥（`product_id` / `brand`，每跳右表键唯一）。桥表只有真用上才进 `meta.files` |
| 20 | 「付款/已付」 | 是非问（「有没有…」「是否存在…」）不算反向条件；「从未付款」才找带 `*status*` 列、与主表有唯一键、值里有「已/paid」的表做反连接（q038 → `payment.pay_status='已付'`） |

**仍然等契约补的（JOIN 相关）**

| # | 缺口 | 本实现怎么绕 |
|---|---|---|
| 21 | §2.6 `trace.source` 只有**一个** `file`/`sheet`，但 JOIN 后一个数字可能由多张表算出来；§4.3 T2 又要求 `source.columns` 都在那张表的表头里 | `source` 记**主表**（算法事实表），`source.columns` 只保留主表真实存在的列（表达式引用到的列一个不丢，T5 优先）。跨表维度列（如 `region_name`、`brand`）因此**不出现在** `source.columns` 里 |
| 22 | §2.5 规定 JOIN 行 `__rid` 用 `+` 连接各源 id，但 §4.3 T3 的反解规则只写了单个 `文件#sheet!行号` | 行标识（`trace.row_ids`）只取**主表**命中的源行（`agg(...)` 也按主表文件名生成），保证 T3 可机械反解 |


## 7. 回归

- `python -m ask.static_check --selftest` → 8/8；`python -m ask.execute --selftest` → 6/6；
  `python -m ask.decide --selftest` → 16/16；`python -m ask.query --selftest` → 8/8（新增三条：`grain_monthly` / `period_compare` / `pct_base_zero`）。
- `python -m ask.decide --verify-schemas` → PASS（`ask/inject/*.json` 全部 `contract_version="1.1"`）。
- 第 4 步判据 `python -m ask.query --question-file ask/fixtures/question.first.txt --decide d1=inject:ask/inject/d1.json --out ask/answer_out/smoke.json`：
  连跑两遍除 `meta.created` / `meta.elapsed_ms` 外字节一致，`answer_id` 不变，
  `status=ok`、SQL 含 `GROUP BY`、`metrics_used` 含 `gmv`。

## 8. 契约 v1.1 收口（本轮）

契约 `contract/answer.md` 升到 1.1 后，上面第 1 条的 `is_null` / `is_not_null`、第 2 条的
`time_grain_grouping`（派生列 `period` + `GROUP BY period`）、第 4 条的 `comparison`（两期一条 SQL +
`__delta` / `__pct` 派生列）、第 8 条里的跨表（`D1.listing` + `D3.joins[]`）都已有明文规则，
`ask/` 已按新字段实现（D1 的四个新字段是**真源**，`plan_rules` 的粒度/两期猜测降级为兜底）。

**仍然要等契约补的（本轮没有自己发明）**

| # | 缺口 | 本实现怎么绕 |
|---|---|---|
| 10 | §2.4 说「派生列只许三种，多一种即 `COLUMN_INVALID`」，但 §2.11 的 `error.code` 封闭集里**没有** `COLUMN_INVALID`（与 `INJECT_DIGEST_UNCHECKED` 同一个毛病） | 派生列白名单照 §2.4 校验，命中外来派生列时用 `COLUMN_NOT_FOUND` / `SchemaViolation` 的措辞拒绝，不自造错误码 |
| 11 | §5.2 要求「留空 `input_digest` 要记 `INJECT_DIGEST_UNCHECKED`」，§2.9 封闭集未收录 | 仍发这条 caveat，code 单列在 `decide.EXTRA_CAVEAT_CODES` 里等契约升版 |
| 12 | §5.1 只写「`left` 为 0 → 变化率 null + `PCT_BASE_ZERO`」，没写**哪一侧是基期**、没写 `columns[]` 里不带后缀的 `<指标>` 列放哪一期 | `left`=基期（除数，且 `time_range` 也要求等于它）、`right`=对比期；`<指标>` 列放基期值 |
| 13 | §9 的 `ask/` 迁移清单里没有「真实跨表 JOIN 的 SQL 生成」 | 本轮已补（见第 6 节）：D3 兜底写 `joins[]`，`ask/sqlgen.py` 按它拼 JOIN，不再报 `INTERNAL` |
| 14 | §4.1 的 `N` 是否排除 `null` 单元格（第 7 条）仍然没写清 | 空值格不发 trace；两期对比里基期为 0 的那格就是 `null`（`PCT_BASE_ZERO`），因此不出 trace |

## 9. 两期对比的键集并集（本轮补完）

§5.1 v1.1 要求「合并后的键集必须是两期键集的**并集**」，本轮把对比语句从 `INNER JOIN` 改成：

```
WITH "__left" AS (基期聚合), "__right" AS (对比期聚合),
     "__keys" AS (SELECT <维度> FROM "__left" UNION SELECT <维度> FROM "__right")
SELECT "__keys".<维度>, "__left".<指标> AS <指标>,
       <差值> AS <指标>__delta, <变化率> AS <指标>__pct
FROM "__keys" LEFT JOIN "__left" ON … LEFT JOIN "__right" ON …
```

- 只在其中一期出现的键照样出一行；缺失侧的 `<指标>` 写 `null`（**不补 0**），该行 `__delta` / `__pct` 也是 `null`；
- 没有维度时：`"__keys"` = `SELECT 1 FROM "__left" UNION SELECT 1 FROM "__right"`，两期都空则结果仍 0 行（`RESULT_EMPTY`）；
- 两种「写 null」分开记账（`ask/query.py::_comparison_caveats`，判据只看输出列，因为对比期原值不进 `columns[]`）：
  - `<指标>` 为 `null` ⟹ 基期没有这个键；`<指标>` 有值而 `__delta` 为 `null` ⟹ 对比期没有这个键 → `COMPARISON_KEY_MISSING`；
  - `<指标>` 与 `__delta` 都有值、只有 `__pct` 为 `null` ⟹ 基期为 0 → `PCT_BASE_ZERO`。

## 10. #12 验收记录（多表 JOIN）

- `python ask/_selfcheck/run_all.py q031 … q040` → **10/10 PASS**，exit 0；
  每题 SQL 都含 `JOIN`（q033/q038 是 `LEFT JOIN`），静态检查照常放行、写操作照常拒绝。
- `python ask/_selfcheck/run_all.py`（全 60 题）→ **59/60**；唯一失败的 q056 是第 5 节
  已记录的数据限制（没有 5 倍尖峰），非 JOIN 部分 **49/50**。
- `python ask/_selfcheck/twice.py`（q031–q040 连跑两遍）→ PASS：稳定子集逐字节一致、`answer_id` 不变。
- 模块自检：`static_check 8/8`、`execute 6/6`、`query 8/8`、`schema 10/10`、`decide --verify-schemas` PASS。

