# tally 契约 · `answer.json` / 指标字典 / 追溯链 / 决策模型注入

> 本文是**逐字段契约**，不是设计说明。凡写「必须」的地方，实现层要么做到，要么抛错；凡写 `null` 的地方，实现层**不许**编一个值填进去。
> 本文例子里的所有数字、人名、文件内容**都是示意数据，数字是编的**，不代表任何真实被测结果。
> 本文不含任何实测统计（准确率、覆盖率、耗时）。本项目目前**一条 60 题都没跑过**，所以任何「xx%」出现在别处都不是本文件给的。

---

## 1. 范围与非目标

**这层负责什么（一句话）**：定义「一次中文提问 → 一个可追溯的答案」这段路上**所有跨模块交换的数据形状**——字段名、类型、必填性、降级语义，以及四个决策点的输入输出 schema，使 `ask/` 与 `show/` 能各自独立实现而不互相猜。

**明确不做**：

| 不做 | 理由 / 归谁 |
|---|---|
| 不定义 UI 布局、颜色、交互 | `show/` 的事 |
| 不定义 schema 推断算法（列类型怎么判、唯一值怎么算） | `ask/` 内部实现，受本文第 2 节输出形状约束 |
| 不定指标字典的**存储介质**（JSON 文件还是别的） | 只定义条目格式与校验规则（第 3 节） |
| 不定义 SVG 的绘制细节（坐标、路径、字体） | `answer.json` 只给图表**描述**，不含 SVG 字符串（第 2 节 `charts`） |
| 不定义 prompt 文本 | 只定义注入协议与缓存键（第 5 节） |
| 不做写操作 / 多用户 / 云端 / 大屏 | README 已定 |
| 不产出任何 `.py` / `.js` / `.html` / `.json` 实现文件 | 本步零代码 |
| 不给出任何未核实的统计数字 | 见文首 |  |

---

## 2. `answer.json` 契约

### 2.1 顶层字段

`必填` 列取值：**必填** = 任何 `status` 下都必须存在；**条件** = 由 `status` 决定；**可空** = 字段必须存在，值可以是 `null` / `[]`。

| 字段名 | 类型 | 必填 | 含义 | 示例（示意数据，数字是编的） |
|---|---|---|---|---|
| `question` | `string` | 必填 | 用户原始提问，**逐字保存**，不做任何清洗（归一化只发生在缓存键里，见第 5 节） | `"2025 年各区域的成交额是多少"` |
| `answer_id` | `string` | 必填 | `sha256(cache_key)` 前 16 位十六进制；同一答案重复导出必须同 id | `"a3f1c8e20b9d4c17"` |
| `status` | `string` | 必填 | 三选一：`"ok"` / `"clarify"` / `"error"`。**决定用哪种体**，决定哪些字段必须为 `null` | `"ok"` |
| `plan` | `object[]` | 条件 | D2 产出的**步骤清单**；`status="ok"` 时非空，`status="clarify"` 时可为空数组。元素见 2.2 | 见 2.2 |
| `plan_summary` | `string` | 必填 | 一句话计划，供 `show/` 当标题；不许为空字符串 | `"单步：按区域聚合成交额"` |
| `metrics_used` | `object[]` | 必填 | 本次用到的指标字典条目**快照**（拷贝定义，不是引用）；没用到就是 `[]`。元素见 2.3 | 见 2.3 |
| `sql` | `string[]` | 条件 | **实际执行的** SQL，按执行顺序；`status="ok"` 时非空。每个元素**恰好一条语句**，只有 SELECT | `["SELECT region, SUM(...) FROM sales WHERE year=2025 GROUP BY region ORDER BY region ASC"]` |
| `columns` | `object[]` | 必填 | 结果表列定义，**按 SELECT 顺序**。元素见 2.4 | 见 2.4 |
| `rows` | `object[]` | 条件 | 结果数据，每行一个对象；`status="ok"` 且结果非空时非空。元素见 2.5 | 见 2.5 |
| `trace` | `object[]` | 条件 | 追溯链，**一个输出数字一条**；`status="ok"` 时与数值单元格数量相等（第 4 节） | 见 2.6 |
| `charts` | `object[]` | 必填 | 图表**描述**（类型 + 取哪几列 + 排序）；不含 SVG。可以是 `[]` | 见 2.7 |
| `narrative` | `object[]` | 必填 | D4 解读句，每句挂证据；`[]` 表示「只给数，不说话」。元素见 2.8 | 见 2.8 |
| `caveats` | `object[]` | 必填 | 必须显式声明的限制，`code` 取自固定枚举（2.9）。可以是 `[]` | 见 2.9 |
| `clarify` | `object \| null` | 必填 | 需要反问时为对象（2.10），否则**必须是 `null`** | `null` |
| `error` | `object \| null` | 必填 | `status="error"` 时为对象（2.11），否则**必须是 `null`** | `null` |
| `meta` | `object` | 必填 | 运行元信息（2.12） | 见 2.12 |

### 2.2 `plan[]`（D2 产出的步骤清单）

| 字段 | 类型 | 必填 | 含义 | 示例 |
|---|---|---|---|---|
| `step_id` | `int` | 必填 | 从 1 开始、连续、按升序排列 | `1` |
| `intent` | `string` | 必填 | 六选一：`distribution` / `trend` / `compare` / `anomaly` / `rank` / `detail` | `"detail"` |
| `title` | `string` | 必填 | 人读的一步话，≤40 字 | `"按区域汇总成交额"` |
| `needs.metric_ids` | `string[]` | 必填 | 这一步要用哪些指标 id；可空（纯明细步骤） | `["gmv"]` |
| `needs.dimensions` | `string[]` | 必填 | 分组的**原始列名**（不是字典名）。**唯一例外**：粒度分组时的 `"period"`（v1.1 派生列，见 2.4） | `["region"]` |
| `sql_index` | `int \| null` | 必填 | 指向 `sql[]` 的下标。**一个 step 最多挂一条 SQL**；要两条就拆成两步（理由：一 step 两 SQL 时，追溯链没法回答「这个数字出自哪一步」） | `0` |
| `depends_on` | `int[]` | 必填 | 依赖的 `step_id`；只许引用更小的 `step_id` | `[]` |
| `fallback_of` | `int \| null` | 必填 | 若本步是某步的兜底重试，填被重试的 `step_id`，否则 `null` | `null` |

### 2.3 `metrics_used[]`（字典快照）

| 字段 | 类型 | 必填 | 含义 | 示例 |
|---|---|---|---|---|
| `id` | `string` | 必填 | 指标 id | `"gmv"` |
| `version` | `int` | 必填 | 使用时的字典版本号 | `3` |
| `name` | `string` | 必填 | 中文名 | `"成交额"` |
| `definition` | `string` | 必填 | 可执行 SQL 表达式，**逐字拷贝**当时字典里的值 | `"SUM(CASE WHEN status='paid' THEN amount ELSE 0 END)"` |
| `unit` | `string` | 必填 | 单位，取自第 3 节白名单 | `"CNY"` |
| `owner` | `string` | 必填 | 口径负责人 | `"运营-王工"` |
| `definition_sha256` | `string` | 必填 | `definition` 的 sha256；用于回答「字典改过之后这条答案还能不能用」 | `"9f2c1e..."` |

> 快照的意义：字典改了口径，旧 `answer.json` 仍然能证明「当时是按什么算的」。所以这里**必须是拷贝**，不许只存 id。

### 2.4 `columns[]`

| 字段 | 类型 | 必填 | 含义 | 示例 |
|---|---|---|---|---|
| `name` | `string` | 必填 | 输出列名 | `"gmv"` |
| `type` | `string` | 必填 | 六选一：`int` / `decimal` / `text` / `date` / `bool` / `null` | `"decimal"` |
| `unit` | `string \| null` | 必填 | `type="decimal"` 时非空；文本/日期列必须为 `null` | `"CNY"` |
| `digits` | `int` | 必填 | 小数位数。`type="int"` 时必须为 `0`；`type="decimal"` 时必须 ≥ 0；文本列必须为 `0`。**格式化按这个数走，见第 6 节** | `2` |
| `source_metric_id` | `string \| null` | 必填 | 该列来自哪个字典指标；非字典口径必须 `null`（不许填近似的 id） | `"gmv"` |
| `source_column` | `string \| null` | 必填 | 直接映射原始列时填原始列名，否则 `null` | `null` |

> **v1.1 新增（派生列是封闭的三种）**：`columns[]` 里允许出现的派生列**只有**下面三种，多一种就是 `COLUMN_INVALID`（防止以后悄悄扩成「随便算一列」）：
>
> | 派生列 | 何时必须有 | 固定字段 |
> |---|---|---|
> | `period` | `d1.time_grain_grouping != null` | `type="text"`、`digits=0`、`unit=null`、`source_metric_id=null`、`source_column=null`、`derived=true`、`from="<原始时间列>"`、`grain="<粒度>"` |
> | `<metric_id>__delta` | `d1.comparison != null` | `type`/`digits`/`unit` 与指标一致、`source_metric_id=<metric_id>`、`source_column=null`、`derived=true` |
> | `<metric_id>__pct` | `d1.comparison != null` | `type="decimal"`、`digits=2`、`unit="%"`、`source_metric_id=<metric_id>`、`source_column=null`、`derived=true` |

### 2.5 `rows[]`

每行是一个对象：键 = `columns[].name`（全部出现，缺值写 `null`），外加保留键 `__rid`。

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `__rid` | `string` | 必填 | 行标识，全局唯一，用于 `trace[].row_ids` 指认。规则见下 |
| 其余键 | 按 `columns[].type` | 必填 | 值必须与声明类型一致；`int` 不许写成 `1.0` |

`__rid` 生成规则（确定性）：

- 明细行：`<文件basename>#<sheet>!<源行号>`，源行号 1 基且指**数据行**（表头不计）。CSV 的 `sheet` 写文件名去掉扩展名。
- 聚合行：`agg(<文件basename>#<sheet>:<group_key>)`，`group_key` 按 `plan.needs.dimensions` 顺序拼 `列名=值`，用 `;` 连接。
- JOIN 行：各源 id 用 `+` 连接，**按文件名+sheet 的字节序排序**后再连，保证同一物理行组合永远同一个 `__rid`。

### 2.6 `trace[]`（追溯链元素）

字段完整定义见第 4 节。字段总表：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `trace_id` | `string` | 必填 | `t-` + 4 位序号（`t-0001`） |
| `value_path` | `string` | 必填 | 被追溯的数字在答案里的位置，如 `rows[3].gmv` |
| `value` | `int \| decimal \| string` | 必填 | 该数字的值，必须与 `value_path` 指向处**严格相等** |
| `metric_id` | `string \| null` | 必填 | 字典指标 id；非字典口径必须 `null` |
| `source.file` | `string` | 必填 | 相对项目根的正斜杠路径 |
| `source.file_sha256` | `string` | 必填 | 源文件内容哈希 |
| `source.sheet` | `string \| null` | 必填 | 工作表名；CSV 为 `null` |
| `source.header_row` | `int \| null` | 必填 | 表头所在行号（1 基）；推断不出时必须 `null` 并在 `caveats` 记 `HEADER_ROW_GUESSED` |
| `source.columns` | `string[]` | 必填 | 用到的**原始列名**，非空 |
| `row_ids` | `string[]` | 必填 | 命中的行标识，非空 |
| `row_ids_total` | `int` | 必填 | 命中行总数（未截断时等于 `len(row_ids)`） |
| `row_ids_truncated` | `bool` | 必填 | 是否因 `trace_row_id_cap` 截断 |
| `filter` | `object` | 必填 | 过滤条件；不过滤时必须写 `{"none": true}`，**不许省略** |
| `expression` | `string` | 必填 | 对原始列的可执行 SQL 表达式（非空） |
| `aggregation` | `object[]` | 必填 | 聚合步骤，按顺序；明细指标必须写 `[{"op":"identity"}]`，空数组算不完整 |
| `sql_index` | `int` | 必填 | 指向 `sql[]` 的下标 |

### 2.7 `charts[]`

| 字段 | 类型 | 必填 | 含义 | 示例 |
|---|---|---|---|---|
| `chart_id` | `string` | 必填 | `c-` + 2 位序号 | `"c-01"` |
| `type` | `string` | 必填 | `bar` / `line` / `scatter` 三选一 | `"bar"` |
| `title` | `string` | 必填 | 图标题 | `"各区域成交额"` |
| `x` | `object` | 必填 | `{field, label, type}`，`field` 必须是 `columns[].name` 之一 | `{"field":"region","label":"区域","type":"text"}` |
| `y` | `object[]` | 必填 | 非空；`{field, label, unit, digits}` | `[{"field":"gmv","label":"成交额","unit":"CNY","digits":2}]` |
| `sort` | `object` | 必填 | `{by:"__row"|"x"|"y", desc:bool}`；`__row` = 沿用 `rows` 顺序 | `{"by":"y","desc":true}` |
| `source_trace_ids` | `string[]` | 必填 | 该图用到的所有 trace；图里每个点都必须有，否则该点不许画 | `["t-0001","t-0002"]` |
| `note` | `string \| null` | 必填 | 备注，没有就是 `null` | `null` |

> `charts` 里**不许出现任何数据值**，只许出现字段名。图上的数字一律从 `rows` 取、从 `trace` 找解释。这样「图里的数」和「表里的数」不可能不一致。

### 2.8 `narrative[]`（D4 解读）

| 字段 | 类型 | 必填 | 含义 | 示例 |
|---|---|---|---|---|
| `text_template` | `string` | 必填 | 一句话。**任何阿拉伯数字都必须写成 `{{trace:t-0007}}`**，不许写裸数字 | `"华东区最高，为 {{trace:t-0001}} 元"` |
| `evidence` | `string[]` | 必填 | 非空；`text_template` 里出现的每个 `trace_id` 都必须在这里 | `["t-0001"]` |
| `claim_kind` | `string` | 必填 | `fact` / `compare` / `limit` 三选一 | `"fact"` |

### 2.9 `caveats[]` 的 `code` 枚举（**封闭集**，新增必须升契约版本）

| code | 何时必须出现 |
|---|---|
| `NOT_PRUNED` | D3 兜底走全列（`pruned=false`） |
| `METRIC_NOT_IN_DICT` | 用了字典里没有的口径 |
| `DICT_VERSION_DRIFT` | 命中缓存，但字典版本已变 |
| `PLAN_MINIMAL` | D2 退化为最小计划 |
| `NO_INTERPRETATION` | D4 无证据，未给结论 |
| `RESULT_EMPTY` | SQL 成功但 0 行 |
| `TRUNCATED_BY_ROW_CAP` | 结果行数超 `row_cap` 被截断 |
| `TRACE_ROW_IDS_TRUNCATED` | `row_ids` 超 `trace_row_id_cap` 被截断 |
| `HEADER_ROW_GUESSED` | 表头行是猜的 |
| `JOIN_ROW_MULTIPLIED` | JOIN 后行数比任一源表大，结果可能被放大 |
| `ASSUMED_TIMEZONE` | 时间范围按本地某时区解释 |
| `COLUMN_ALIAS_FUZZY` | 中文列名靠编辑距离匹配上的，非人工别名 |
| `CLARIFY_TRUNCATED_SLOTS` | 反问槽位超 3 个，只问了前 3 个 |
| `PCT_BASE_ZERO` | v1.1 新增：两期对比里基期为 0，变化率写 `null`（不许写 0%，也不许写 ∞） |
| `COMPARISON_KEY_MISSING` | v1.1 新增：两期对比里某个键只在其中一期出现，缺失侧写 `null`（不许补 0） |

元素形状：`{"code": "...", "message": "人话一句", "affects": { "fields": [...], "trace_ids": [...] }}`。`message` 不许为空。

### 2.10 `clarify`（反问体）

| 字段 | 类型 | 必填 | 含义 | 示例（示意数据，数字是编的） |
|---|---|---|---|---|
| `question` | `string` | 必填 | 反问给用户看的那一句 | `"「销售额」在字典里有两个口径，你要哪个？"` |
| `missing` | `object[]` | 必填 | 非空，每个槽位一个 | 见下 |
| `missing[].slot` | `string` | 必填 | `metric` / `time_range` / `grain` / `dimension` / `filter` / `dataset` 之一 | `"metric"` |
| `missing[].why_needed` | `string` | 必填 | 为什么缺它就算不出来 | `"两个口径的数值差一倍"` |
| `missing[].candidates` | `object[]` | 必填 | 候选，每个带 `{id, name, definition}`；`id` 为 `null` 表示「候选不是字典条目」 | `[{"id":"gmv_paid","name":"已付款成交额","definition":"..."}]` |
| `asked_at_step` | `string` | 必填 | 固定 `"D1"`（P0 只有 D1 会反问） | `"D1"` |

### 2.11 `error`（错误体）

| 字段 | 类型 | 必填 | 含义 | 示例 |
|---|---|---|---|---|
| `code` | `string` | 必填 | 枚举见下 | `"SQL_EXEC_ERROR"` |
| `message` | `string` | 必填 | 原始错误一句（不许改写、不许吞掉） | `"no such column: amout"` |
| `readable_hint` | `string` | 必填 | 给用户看的、不含 SQL 的一遍解释 | `"数据里没有「金额」这一列，可能是列名「amount」拼错了"` |
| `stage` | `string` | 必填 | `ingest` / `D1` / `D2` / `D3` / `static_check` / `execute` / `D4` / `export` | `"execute"` |
| `attempts` | `object[]` | 必填 | 每次尝试一条：`{attempt, source_decision, sql_index, error_code, error_message, d3_snapshot}` | 见 2.11 示例 |
| `partial` | `object` | 必填 | 出错前已确定的部分：`{plan, sql, columns}`，没有就 `{"plan":[],"sql":[],"columns":[]}` | — |

`error.code` 枚举（封闭集）：`FILE_NOT_FOUND` / `FILE_PARSE_ERROR` / `SHEET_NOT_FOUND` / `SCHEMA_UNREADABLE` / `COLUMN_NOT_FOUND` / `METRIC_INVALID` / `DICT_LOAD_FAILED` / `SQL_STATIC_REJECT` / `SQL_EXEC_ERROR` / `SQL_RETRY_LOOP` / `RESULT_NONFINITE` / `ORDER_NOT_TOTAL` / `CLARIFY_EXHAUSTED` / `QUESTION_TOO_BROAD` / `INJECT_STALE` / `INJECT_SCHEMA_INVALID` / `INTERNAL`。

### 2.12 `meta`

| 字段 | 类型 | 必填 | 含义 | 示例 |
|---|---|---|---|---|
| `contract_version` | `string` | 必填 | 本契约版本，当前 `"1.1"`；不兼容改动必须 +1 | `"1.1"` |
| `created` | `string` | 必填 | 本地时间 `YYYY-MM-DDTHH:MM:SS`。**允许因运行时间而不同** | `"2026-10-26T21:03:11"` |
| `elapsed_ms` | `int` | 必填 | 端到端耗时。**允许因环境而不同** | `842` |
| `model` | `object` | 必填 | 四个决策点各一条（见下） | — |
| `model.D1..D4` | `object` | 必填 | `{source:"model"\|"inject"\|"fallback", model:string|null, injected:bool, latency_ms:int, output_sha256:string}` | `{"source":"model","model":"local-a","injected":false,"latency_ms":840,"output_sha256":"3a7d..."}` |
| `cache.hit` | `bool` | 必填 | 是否命中缓存。**允许因环境而不同** | `false` |
| `cache.key` | `string` | 必填 | `cache_key`，见第 5 节 | `"5b0e..."` |
| `cache.source_answer_id` | `string \| null` | 必填 | 命中时指向被复用的答案 id，否则 `null` | `null` |
| `cache.written` | `bool` | 必填 | 本次结果是否已写入缓存 | `true` |
| `files` | `object[]` | 必填 | `{path, sha256}`，按 `path` 字节序 | `[{"path":"data/sales.xlsx","sha256":"1c9f..."}]` |
| `metric_dict_sha256` | `string` | 必填 | 本次加载的字典文件整体哈希 | `"77aa..."` |
| `runtime` | `object` | 必填 | `{sqlite, python, os}`。**允许因环境而不同** | `{"sqlite":"3.45.1","python":"3.12.4","os":"win32"}` |
| `options` | `object` | 必填 | 参与缓存键的选项：`{row_cap, trace_row_id_cap, rows_preview_limit}` | `{"row_cap":5000,"trace_row_id_cap":200,"rows_preview_limit":20}` |

### 2.13 降级时必须为 `null` 的字段（**硬清单**）

> 总原则：**`null` 表示「没有」，`0` 表示「算出来是零」，`""` 表示「有但为空」。三者不许互相顶替。** 禁止用 `"-"` / `"'N/A'"` / 假数字填补缺失。

| 场景 | 必须为 `null` / `[]` 的字段 | 不许做的事 |
|---|---|---|
| `status="clarify"` | `sql: []`、`rows: []`、`columns: []`、`trace: []`、`charts: []`、`narrative: []`、`error: null` | 不许「先按最像的指标算一版再问」；`metrics_used` 只许放**已经唯一确定**的指标，若指标本身有歧义必须 `[]` |
| `status="error"` | `rows: []`、`trace: []`、`charts: []`、`narrative: []`、`clarify: null` | 不许拿上一次缓存答案的 `rows` 顶替（真要用缓存，必须 `cache.hit=true` 且 `cache.source_answer_id` 指向） |
| SQL 成功但 0 行 | `rows: []`、`trace: []`、`narrative: []`（+ `caveats` 含 `RESULT_EMPTY`） | 不许因为「空不好看」就自动放宽过滤条件重查一次 |
| 非字典口径的列 | `columns[].source_metric_id: null`、`trace[].metric_id: null` | 不许填一个「差不多」的指标 id |
| 表头行推断不出 | `source.header_row: null` | 不许写 `1` 蒙过去（同时必须记 `HEADER_ROW_GUESSED`） |
| CSV 源的 sheet | `source.sheet: null` | 不许写 `""` 或 `"Sheet1"` |
| D3 退化为全列 | `pruned: false`（在 D3 输出里），同时 `caveats` 必须含 `NOT_PRUNED` | 不许「默默多选了几列」；多选的列必须可见 |
| 值拿不到精确结果（如除零、NaN） | 该单元格 `null` 且 `status="error"`、`code="RESULT_NONFINITE"` | 不许四舍五入/截断成一个「看起来合理」的数 |
| 没有结论 | `narrative: []` + `caveats` 含 `NO_INTERPRETATION` | 不许写「总体表现良好」这类无证据句 |
| 没有图 | `charts: []` | 不许给一个空图占位 |

### 2.14 成功体（示意数据，数字是编的）

```json
{
  "question": "2025 年各区域的成交额是多少",
  "answer_id": "a3f1c8e20b9d4c17",
  "status": "ok",
  "plan": [
    {
      "step_id": 1,
      "intent": "detail",
      "title": "按区域汇总成交额",
      "needs": {"metric_ids": ["gmv"], "dimensions": ["region"]},
      "sql_index": 0,
      "depends_on": [],
      "fallback_of": null
    }
  ],
  "plan_summary": "单步：按区域聚合成交额",
  "metrics_used": [
    {
      "id": "gmv",
      "version": 3,
      "name": "成交额",
      "definition": "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
      "unit": "CNY",
      "owner": "运营-王工",
      "definition_sha256": "9f2c1e0d4b7a"
    }
  ],
  "sql": [
    "SELECT region AS \"region\", SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) AS \"gmv\" FROM sales WHERE year = 2025 GROUP BY region ORDER BY region ASC"
  ],
  "columns": [
    {"name": "region", "type": "text", "unit": null, "digits": 0, "source_metric_id": null, "source_column": "region"},
    {"name": "gmv", "type": "decimal", "unit": "CNY", "digits": 2, "source_metric_id": "gmv", "source_column": null}
  ],
  "rows": [
    {"__rid": "agg(sales#Sheet1:region=华东)", "region": "华东", "gmv": 1234567.89},
    {"__rid": "agg(sales#Sheet1:region=华南)", "region": "华南", "gmv": 987654.32}
  ],
  "trace": [
    {
      "trace_id": "t-0001",
      "value_path": "rows[0].gmv",
      "value": 1234567.89,
      "metric_id": "gmv",
      "source": {
        "file": "data/sales.xlsx",
        "file_sha256": "1c9f5b...",
        "sheet": "Sheet1",
        "header_row": 1,
        "columns": ["region", "status", "amount"]
      },
      "row_ids": ["sales#Sheet1!42", "sales#Sheet1!57"],
      "row_ids_total": 2,
      "row_ids_truncated": false,
      "filter": {"column": "region", "op": "=", "value_literal": "'华东'", "plus_year": 2025},
      "expression": "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
      "aggregation": [{"op": "sum", "over": "amount", "group_by": ["region"]}],
      "sql_index": 0
    },
    {
      "trace_id": "t-0002",
      "value_path": "rows[1].gmv",
      "value": 987654.32,
      "metric_id": "gmv",
      "source": {
        "file": "data/sales.xlsx",
        "file_sha256": "1c9f5b...",
        "sheet": "Sheet1",
        "header_row": 1,
        "columns": ["region", "status", "amount"]
      },
      "row_ids": ["sales#Sheet1!91"],
      "row_ids_total": 1,
      "row_ids_truncated": false,
      "filter": {"column": "region", "op": "=", "value_literal": "'华南'", "plus_year": 2025},
      "expression": "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
      "aggregation": [{"op": "sum", "over": "amount", "group_by": ["region"]}],
      "sql_index": 0
    }
  ],
  "charts": [
    {
      "chart_id": "c-01",
      "type": "bar",
      "title": "各区域成交额",
      "x": {"field": "region", "label": "区域", "type": "text"},
      "y": [{"field": "gmv", "label": "成交额", "unit": "CNY", "digits": 2}],
      "sort": {"by": "y", "desc": true},
      "source_trace_ids": ["t-0001", "t-0002"],
      "note": null
    }
  ],
  "narrative": [
    {
      "text_template": "华东区最高，为 {{trace:t-0001}} 元，比华南区多 {{trace:t-0002}}",
      "evidence": ["t-0001", "t-0002"],
      "claim_kind": "compare"
    }
  ],
  "caveats": [],
  "clarify": null,
  "error": null,
  "meta": {
    "contract_version": "1.1",
    "created": "2026-10-26T21:03:11",
    "elapsed_ms": 842,
    "model": {
      "D1": {"source": "model", "model": "local-a", "injected": false, "latency_ms": 210, "output_sha256": "3a7d11"},
      "D2": {"source": "model", "model": "local-a", "injected": false, "latency_ms": 190, "output_sha256": "9c02be"},
      "D3": {"source": "model", "model": "local-a", "injected": false, "latency_ms": 240, "output_sha256": "51ee7a"},
      "D4": {"source": "model", "model": "local-a", "injected": false, "latency_ms": 202, "output_sha256": "bb31f0"}
    },
    "cache": {"hit": false, "key": "5b0e7c1a9d", "source_answer_id": null, "written": true},
    "files": [{"path": "data/sales.xlsx", "sha256": "1c9f5b..."}],
    "metric_dict_sha256": "77aa0f",
    "runtime": {"sqlite": "3.45.1", "python": "3.12.4", "os": "win32"},
    "options": {"row_cap": 5000, "trace_row_id_cap": 200, "rows_preview_limit": 20}
  }
}
```

> 注意 `t-0001` 的 `filter` 只写了分区条件、`plus_year` 单列了一个键——这是刻意的：`filter` 允许除了三种必需键之外追加键，但追加的键必须同样出现在 `sql` 里。校验器不猜「多余键是什么意思」，只检查「SQL 里能找到它」。

### 2.15 错误体（示意数据，数字是编的）

```json
{
  "question": "2025 年各区域的成交额是多少",
  "answer_id": "d91b2f0c4a5e6677",
  "status": "error",
  "plan": [
    {
      "step_id": 1,
      "intent": "detail",
      "title": "按区域汇总成交额",
      "needs": {"metric_ids": ["gmv"], "dimensions": ["region"]},
      "sql_index": 0,
      "depends_on": [],
      "fallback_of": null
    }
  ],
  "plan_summary": "单步：按区域聚合成交额（执行失败）",
  "metrics_used": [],
  "sql": [],
  "columns": [],
  "rows": [],
  "trace": [],
  "charts": [],
  "narrative": [],
  "caveats": [
    {"code": "NOT_PRUNED", "message": "选列未精简（D3 兜底为全列），可能答非所问", "affects": {"fields": ["plan"], "trace_ids": []}}
  ],
  "clarify": null,
  "error": {
    "code": "SQL_EXEC_ERROR",
    "message": "no such column: amout",
    "readable_hint": "数据里没有「金额」这一列；最接近的是 amount，可能是拼写不对",
    "stage": "execute",
    "attempts": [
      {"attempt": 1, "source_decision": "D3", "sql_index": 0, "error_code": "SQL_EXEC_ERROR", "error_message": "no such column: amout", "d3_snapshot": {"tables": [{"name": "sales", "reason": "唯一含 region 的表"}], "columns": [{"table": "sales", "column": "region", "reason": "分组维度"}, {"table": "sales", "column": "amout", "reason": "命中「金额」同义词"}], "joins": [], "pruned": false, "prune_strategy": "all", "token_estimate": 0}},
      {"attempt": 2, "source_decision": "D3", "sql_index": 1, "error_code": "SQL_EXEC_ERROR", "error_message": "no such column: amout", "d3_snapshot": {"tables": [{"name": "sales", "reason": "回退：仍只有这张表"}], "columns": [{"table": "sales", "column": "region", "reason": "分组维度"}, {"table": "sales", "column": "amount", "reason": "回退：按 schema 里唯一的近似列重选"}], "joins": [], "pruned": true, "prune_strategy": "minimal", "token_estimate": 0}}
    ],
    "partial": {"plan": "见 plan", "sql": [], "columns": []}
  },
  "meta": {
    "contract_version": "1.1",
    "created": "2026-10-26T21:07:44",
    "elapsed_ms": 1903,
    "model": {
      "D1": {"source": "model", "model": "local-a", "injected": false, "latency_ms": 205, "output_sha256": "3a7d11"},
      "D2": {"source": "model", "model": "local-a", "injected": false, "latency_ms": 188, "output_sha256": "9c02be"},
      "D3": {"source": "model", "model": "local-a", "injected": false, "latency_ms": 610, "output_sha256": "7ff412"},
      "D4": {"source": "fallback", "model": null, "injected": false, "latency_ms": 0, "output_sha256": "e3b0c4"}
    },
    "cache": {"hit": false, "key": "5b0e7c1a9d", "source_answer_id": null, "written": false},
    "files": [{"path": "data/sales.xlsx", "sha256": "1c9f5b..."}],
    "metric_dict_sha256": "77aa0f",
    "runtime": {"sqlite": "3.45.1", "python": "3.12.4", "os": "win32"},
    "options": {"row_cap": 5000, "trace_row_id_cap": 200, "rows_preview_limit": 20}
  }
}
```

> 错误体里 `sql` 仍然是 `[]` —— 「试过的 SQL」在 `error.attempts` 的 `sql_index` 里，但 `sql[]` 只装**成功执行过的**语句。这个反直觉的取舍是为了让「`sql` 非空 ⟺ 至少跑成过一条」这条判据成立，避免下游把失败的 SQL 也拿去渲染。

---

## 3. 指标字典格式

字典是一个 JSON 文件，顶层 `{"contract_version":"1.1","metrics":[...]}`，`metrics` 按 `id` 字节序排列（排序是为了让字典整体哈希稳定）。

### 3.1 `metric` 字段

| 字段 | 类型 | 必填 | 含义 | 示例（示意数据） |
|---|---|---|---|---|
| `id` | `string` | 必填 | 稳定标识，**永不改名**。正则 `^[a-z][a-z0-9_]{1,31}$` | `"gmv"` |
| `name` | `string` | 必填 | 中文名，≤20 字，全局唯一 | `"成交额"` |
| `definition` | `string` | 必填 | **可执行 SQL 表达式**（聚合表达式，不是整条查询） | `"SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)"` |
| `unit` | `string` | 必填 | 单位，取自白名单 | `"CNY"` |
| `owner` | `string` | 必填 | 口径负责人（人/团队），非空 | `"运营-王工"` |
| `version` | `int` | 必填 | ≥1，单调不减 | `3` |
| `requires_tables` | `string[]` | 必填 | 该表达式用到的表，非空 | `["sales"]` |
| `aliases` | `string[]` | 必填 | 人工别名（中文匹配的第一优先级）；可空 | `["销售额", "成交总额"]` |
| `time_column` | `string \| null` | 必填 | 时间口径落在哪一列；纯无时间指标写 `null` | `"sale_date"` |
| `window` | `object \| null` | 条件 | 环比/同比类指标必须非空：`{grain:"month"\|"quarter"\|"year", compare:"yoy"\|"mom"}` | `{"grain":"month","compare":"yoy"}` |
| `default_aggregation` | `string` | 必填 | `sum` / `avg` / `count` / `count_distinct` / `min` / `max`；与 `definition` 里的外层聚合函数必须一致 | `"sum"` |
| `deprecated` | `bool` | 必填 | 弃用标记；弃用条目仍可被旧答案引用，但新问题不许再选它 | `false` |

单位白名单（封闭集）：`CNY` / `%` / `人` / `次` / `个` / `天` / `ratio` / `none`。

### 3.2 校验规则清单（**任何一条不过就报错，不许静默**）

| # | 规则 | 不过时的错误码 + 行为 |
|---|---|---|
| V1 | `id` 匹配 `^[a-z][a-z0-9_]{1,31}$` 且全局唯一 | `METRIC_INVALID`：列出重复的 id，加载失败 |
| V2 | `name` 非空、≤20 字、**同一 name 只许对应一个 id** | `METRIC_INVALID`：报「两个 id 抢同一个中文名」，因为中文匹配会歧义 |
| V3 | `definition` 非空，且通过第 8 节的只读静态检查（不许含 `;`、注释、DML 关键字） | `SQL_STATIC_REJECT` |
| V4 | `definition` 里出现的每个 `列名` / `表名` 都必须存在于当前 schema 快照 | `COLUMN_NOT_FOUND`（带「最接近的列名」提示，但不自动替换） |
| V5 | `definition` 实际引用的表集合 **恰好等于** `requires_tables` | `METRIC_INVALID`：多声明或少声明都报错 |
| V6 | `unit` 在白名单内 | `METRIC_INVALID`：不猜单位 |
| V7 | `owner` 非空 | `METRIC_INVALID` |
| V8 | `version` 是 ≥1 的整数；**同 id 同 version 的 `definition` 哈希必须一致**（相对于历史快照） | `METRIC_INVALID`：报「同一个版本两个口径」，这是最危险的一种漂移 |
| V9 | `definition` 不许出现 `LIMIT` / `ORDER BY`（指标只描述「一个数」，截断属于查询层） | `METRIC_INVALID` |
| V10 | `window` 非空 ⟺ `time_column` 非空；`grain`/`compare` 在枚举内 | `METRIC_INVALID`：同比到底比哪一期不允许猜 |
| V11 | `default_aggregation` 与 `definition` 外层聚合函数一致（`SUM(...)` 配 `sum`） | `METRIC_INVALID` |
| V12 | **任一条目失败 → 整份字典加载失败**，一次性列出所有错误（不 fail-fast 到第一条），**不许跳过坏条目继续跑** | `DICT_LOAD_FAILED` |

> V12 的理由：跳过坏条目会让系统悄悄用「另一个口径」把数字算出来，而用户以为那是字典口径。宁可不答。

### 3.3 「口径命中」的机械判据

一个输出数字算「命中口径」，当且仅当：

1. `columns[].source_metric_id` 与其对应的 `trace[].metric_id` 相等且非 `null`；
2. `metrics_used[]` 里存在同 id 条目，且 `definition_sha256` == 该条目 `definition` 的 sha256；
3. 该条目的 `definition` 与字典当前版本逐字相等（不相等则必须已记 `DICT_VERSION_DRIFT`）。

三条全过才算命中；只满足 1、2 不满足 3 记为「命中旧版本」，单列一档，不混进命中率。

---

## 4. 追溯链定义

### 4.1 什么算「一个输出数字」

`N` = `rows` 里所有 `columns[].type ∈ {int, decimal}` 的单元格个数。

- `charts` **不产生**新数字（它只引用 `rows` 的列 + `source_trace_ids`）。
- `narrative` **不产生**新数字（它只能用 `{{trace:...}}` 占位符引用已有 trace；文本里出现裸阿拉伯数字即为违规）。
- `plan_summary` / `caveats[].message` 里不许出现数据数字（只许出现步骤号、上限值这类配置常量）。

所以 `N` 是唯一需要追溯的数字总量，`|trace| == N` 是**必要条件**（不是充分条件，见 4.3）。

### 4.2 「一条 trace」必须装什么

| 要素 | 字段 | 判据 |
|---|---|---|
| 源文件 | `source.file` + `source.file_sha256` | 文件存在，且 `meta.files` 里有同 sha256 的条目 |
| sheet | `source.sheet`（CSV 为 `null`） | 非 CSV 时该 sheet 必须真实存在 |
| 列名 | `source.columns` | 非空，且每个列名都在该 sheet 的表头行里真实出现 |
| 过滤条件 | `filter` | 必须存在；无过滤时显式写 `{"none": true}` |
| 命中的行标识 | `row_ids` + `row_ids_total` + `row_ids_truncated` | 非空；每个 id 能定位到真实存在的源行 |
| 计算表达式 | `expression` | 非空；只引用 `source.columns` 里声明过的列名；通过第 8 节静态检查 |
| 聚合步骤 | `aggregation` | 非空数组；明细指标必须显式写 `[{"op":"identity"}]` |

### 4.3 完整性判据 T1–T8（可机械执行）

一条 trace 算「完整」，当且仅当下列八条全过：

| # | 判据 |
|---|---|
| **T1** | `source.file` 非空，且 `meta.files[].sha256` 里能找到 `source.file_sha256` |
| **T2** | `source.columns` 非空，且每个列名（大小写敏感）出现在该文件该 sheet 的表头行里 |
| **T3** | `row_ids` 非空；每个 `row_ids` 元素能按 2.5 的规则反解出「文件#sheet!行号」，且行号 ≤ 该 sheet 数据行数；若 `row_ids_truncated=true` 则必须 `row_ids_total > len(row_ids)` 且 `len(row_ids) == meta.options.trace_row_id_cap` |
| **T4** | `filter` 是对象；要么含 `{"none": true}`，要么含 `column` / `op` / `value_literal` 三个键；`op` 在 `= != > >= < <= in like is_null is_not_null` 之内。`op` 为 `is_null` / `is_not_null` 时 `value_literal` 必须**显式写 `null`**（不许省略、不许写 `""`、不许写字符串 `"null"`）；其余 `op` 的 `value_literal` 不许为 `null` |
| **T5** | `expression` 非空，且其中出现的所有标识符（去掉字符串字面量后）都属于 `source.columns` ∪ {SQL 关键字/函数白名单} |
| **T6** | `aggregation` 是非空数组，每个元素含 `op`，`op` 在 `sum avg count count_distinct min max identity` 之内 |
| **T7** | `sql_index` 是 `sql[]` 的合法下标，且 `sql[sql_index]` 的文本里包含 `expression` 的**子串**（不要求 AST 等价，但要求人能手抄出来核对） |
| **T8** | `value` 与 `value_path` 指向的位置**严格相等**（同类型同值，`decimal` 按 `columns[].digits` 对齐后比较）；`metric_id` 非 `null` 时该指标的快照哈希必须能验证 |

### 4.4 追溯完整率

```
追溯完整率 = (通过 T1–T8 全部八条的 trace 条数) / N
N == 0 时，追溯完整率 = null   （不写 0，也不写 100%）
```

- 分母是**数字**，不是「trace 条数」——这样「把 10 个数字塞进 1 条 trace」不会虚增。
- `null` 与 `0` 严格区分：空结果集没有「追溯不完整」这回事，它只是「没有可追溯的数字」。
- 这个率是**待测指标**，本文件不提供任何实测值（一条都没跑过）。

### 4.5 `trace_row_id_cap`

默认 200（`meta.options.trace_row_id_cap`）。命中行超过它时：

- `row_ids` 只放前 200 条（按 `__rid` 字节序，**不是**按数据库返回顺序，保证确定性）；
- `row_ids_total` 写真值；`row_ids_truncated = true`；
- 必须同时记 `caveats` 的 `TRACE_ROW_IDS_TRUNCATED`。

这是**已知短板**：行数巨大的聚合，追溯链只能证明「这 200 行确实被算进去了」，不能证明「后面没有了」。所以它必须出现在 caveats 里，而不是被藏起来。

---

## 5. 决策模型注入协议

### 5.1 四个决策点

顺序固定：**D1 → D2 → D3 →（静态检查）→ 执行 → D4**。下游只许读上游的**已校验输出**，不许读模型的原始文本（原始文本在 P0 不出现在 `answer.json` 里）。

#### D1 · 问题理解

**输入**
```json
{
  "question": "2025 年各区域的成交额是多少",
  "now": "2026-10-26",
  "schema": [{"file": "data/sales.xlsx", "sha256": "1c9f5b...",
              "sheets": [{"name": "Sheet1",
                          "columns": [{"name": "region", "type": "text", "null_ratio": 0.0, "unique_count": 7}]}]}],
  "metric_dict": [{"id": "gmv", "name": "成交额", "definition": "SUM(...)", "unit": "CNY", "version": 3}],
  "history": []
}
```

**输出**
```json
{
  "metric_ids": ["gmv"],
  "listing": false,
  "time_range": {"start": "2025-01-01", "end": "2025-12-31", "grain": "year"},
  "time_column": null,
  "time_grain_grouping": null,
  "comparison": null,
  "dimensions": ["region"],
  "filters": [{"column": "region", "op": "in", "value": ["华东", "华南"]}],
  "ambiguity": [{"slot": "metric", "why": "字典里有两个口径都叫「销售额」", "candidate_ids": ["gmv_paid", "gmv_created"]}],
  "confidence": 0.86
}
```

**四个新增字段的语义（v1.1，全部必填，不许省略）**

| 字段 | 类型 | 含义 |
|---|---|---|
| `listing` | `bool` | 纯明细列举（「把 2025 年的订单列出来」）。`true` 时 `metric_ids` 必须 `[]`、`dimensions` 必须 `[]`；`false` 时 `metric_ids` 非空才算信息充分。**这是 `metric_ids=[]` 唯一合法的出口**（§7.1 触发条件 1）。 |
| `time_column` | `string \| null` | `time_range` 作用在哪一列。`null` = 由 D3 按业务日期列判定；非 `null` 必须是 schema 里真实存在的列。有了它，「注册时间在 2025 年」写 `time_column:"signup_date"`，**不许**再退化成「时间词指哪列」的歧义。 |
| `time_grain_grouping` | `string \| null` | `day` / `week` / `month` / `quarter` / `year`。非 `null` ⟹ **按时间粒度分组**：D3 必须产出派生列 `period`（`columns[]` 里必须有一条 `name="period"`、`type="text"`），SQL 必须 `GROUP BY period`；`period` 的文本格式固定为 `YYYY-MM-DD`（day）/ `YYYY-Www`（week，ISO 周）/ `YYYY-MM`（month）/ `YYYY-Qn`（quarter）/ `YYYY`（year）。此时 `time_range.grain` 必须等于它或为 `null`。 |
| `comparison` | `object \| null` | 两期对比（「Q2 比 Q1」「本月比上月」「同比」）。形状：`{"kind":"period_over_period"\|"year_over_year","left":{"start","end"},"right":{"start","end"},"label_left":"2025Q1","label_right":"2025Q2"}`。非 `null` 时：`time_range` 必须等于 `left`（它是主区间）；两期**必须在同一条 SQL 里算完**，且这条 SQL 必须含 `LAG(` / `OVER(` / `WITH ` 三者之一（窗口函数或 CTE）；**不许**分两条 SQL 各查一期再由外层口算——那样差额和变化率没有 `sql[]` 可追溯，T7 直接不过。输出必须多两列：`<metric_id>__delta`（差值，`type`/`digits`/`unit` 与指标一致）与 `<metric_id>__pct`（变化率，`type="decimal"`、`digits=2`、`unit="%"`，`left` 为 0 时该格写 `null` 并记 `PCT_BASE_ZERO`）。**`<metric_id>__pct` 的算式固定为 `(right - left) / left * 100`，是百分数不是倍数**。**合并后的键集必须是两期键集的并集**：只在其中一期出现的键也要出现在结果里，缺失那一侧的值写 `null`（**不许写 0**——0 表示「有行、合计为 0」，与「这一期没有这个键」是两件事），该行的 `__delta` 与 `__pct` 都写 `null`，并记 `COMPARISON_KEY_MISSING`（`detail` 写清缺哪一期、缺了几个键）。 |

**硬规则**：
- `ambiguity != []` ⟺ `status="clarify"`。**没有独立的 `needs_clarify` 字段**（两个字段一旦不一致，校验器不知道该信谁；这里只留一个真源）。
- `confidence` **只记录，不参与判定**。低置信不是赌一把的理由。
- `metric_ids` 里每个 id 必须出现在传入的 `metric_dict` 里；不在 → 报 `METRIC_NOT_IN_DICT` 并走 `clarify`（不猜）。
- `time_range` 为 `null` 时，若问题文本里出现年份/季度/「上个月」等时间词 → 必须进 `ambiguity`（例：`now` 与「去年」的解释可能不同）。`comparison` 非 `null` 时时间词已落地，不受这条约束。
- **多表 / 多文件不是 D1 的歧义。** D1 只表达「哪个口径、哪段时间、按哪些维度看」；选表、JOIN 是 D3 的职责（`joins[]`），D3 必须在 `tables[].reason` 里写清为什么选这张表。只有**同一个指标存在多个字典口径**才进 `ambiguity`（v1.1 改动：v1.0 的「数据集不唯一」触发条件已删除，它把「要 JOIN 维表」误判成了「用哪份数据」）。
- `time_column` 为 `null` 且 schema 里**有两个以上候选时间列**、问题里又没有线索（如「2025 年的数据」而表里同时有 `sale_date` 与 `signup_date`）→ 这一种才进 `ambiguity`，槽位名 `time_column`，候选列名列全。


#### D2 · 分析计划

**输入**：`{"question", "d1": <D1 输出>, "schema": <同上>}`

**输出**
```json
{
  "steps": [
    {"step_id": 1, "intent": "distribution", "title": "先看各区域行数分布",
     "needs": {"metric_ids": [], "dimensions": ["region"]}, "sql_hint": null},
    {"step_id": 2, "intent": "detail", "title": "按区域汇总成交额",
     "needs": {"metric_ids": ["gmv"], "dimensions": ["region"]}, "sql_hint": null}
  ],
  "stop_reason": null
}
```

硬规则：
- `step_id` 从 1 连续升序；`depends_on` 只许引用更小的 id；`steps` 非空。
- 兜底：模型不可用或输出不合法 → **最小计划** = 单步 `{intent:"detail", title:"按问题直接取数"}`，`source="fallback"`，且 `caveats` 必须含 `PLAN_MINIMAL`。
- 一个 step 最多一条 SQL（2.2 已定）；`steps` 里出现两个相邻同 `intent` 同 `needs` 的步骤算不合法（说明模型在凑数）。
- **v1.1 新增**：`d1.listing=true` ⟹ 计划必须恰好一步，`intent="detail"`，`needs.metric_ids=[]`，且该步 SQL 是 `SELECT` 明细行（不许带 `GROUP BY`）。
- **v1.1 新增**：`d1.comparison != null` ⟹ 取数步 `intent` 必须是 `compare`，且该步那条 SQL 必须含 `LAG(` / `OVER(` / `WITH ` 之一（与 D1 `comparison` 那条硬规则一致）；`plan` 里不许出现「两期各一步、外层口算」的形状（口算出来的差值与变化率没有 `sql_index` 可挂，T7 必红）。
- **v1.1 新增**：`d1.time_grain_grouping != null` ⟹ 最终取数步的 `needs.dimensions` 必须含 `"period"`（派生列，见 D3 硬规则），即按时间粒度分组。`steps` 里不许再出现「按原始日期列分组」的写法（那会让同一粒度出多个值）。

#### D3 · 选表选列

**输入**：`{"question", "d1", "d2", "schema"}`
（D3 拿得到 D2，是为了知道「要哪些维度」；但 D3 **不许改** D2 的 `steps`。）

**输出**
```json
{
  "tables": [{"name": "sales", "reason": "唯一同时含 region 与 amount 的表"}],
  "columns": [{"table": "sales", "column": "region", "reason": "D1 的分组维度"},
              {"table": "sales", "column": "amount", "reason": "指标 gmv 的定义依赖它"},
              {"table": "sales", "column": "status", "reason": "指标 gmv 的定义依赖它"}],
  "joins": [{"left": "sales", "right": "region_dim", "on": [["region", "region_name"]], "type": "inner"}],
  "pruned": true,
  "prune_strategy": "minimal",
  "token_estimate": 0
}
```

硬规则：
- `pruned=false` ⟹ `prune_strategy="all"` ⟹ `answer.caveats` 必须含 `NOT_PRUNED` ⟹ `meta.model.D3.source="fallback"`。**四处必须同时成立**，缺一处校验器直接报错（这是最容易「悄悄变宽松」的地方）。
- `columns` 里出现的 `{table, column}` 必须在 schema 里存在，且**必须覆盖** `d2` 里所有 `needs.dimensions` 与所有 `metric_ids` 的 `definition` 引用到的列。少一列 → 报 `COLUMN_NOT_FOUND` 并回退 D3（第 7 节）。
- `joins[].type` 只许 `inner` / `left`；JOIN 后若行数大于任一源表行数 → 必须记 `JOIN_ROW_MULTIPLIED`。
- `token_estimate` 是估计值，可以为 0；**不许**把它当预算依据写进任何断言。
- **v1.1 新增**：`d1.time_grain_grouping != null` ⟹ D3 必须在 `columns[]` 里加一条派生列 `{"table": "<时间列所在表>", "column": "period", "derived": true, "from": "<时间列>", "grain": "<粒度>", "reason": "D1 要求按 <粒度> 分组"}`；SQL 必须 `GROUP BY period`，`period` 的文本格式按 §5.1 D1 的 `time_grain_grouping` 表。派生列**不受**「必须在 schema 里存在」那条约束，但 `from` 指向的列必须存在。
- **v1.1 新增**：`d1.time_column != null` ⟹ 时间过滤只能落在那列上；D3 的 `columns[]` 必须包含它，且 `reason` 写明「D1 指定的时间列」。`time_column` 为 `null` 时 D3 才自己挑业务日期列，并必须在 `reason` 里写出挑这一列的依据。
- **v1.1 新增（JOIN 的落地责任）**：当 `d1.dimensions` 里的某个维度在问题里要用「人看得懂的名字」（如区域中文全称）而该名字只存在于维表 ⟹ D3 必须显式写 `joins[]`，不许把需求悄悄降级成只查事实表、也不许为此回退去反问 D1。JOIN 的键必须在 `on` 里写全，`type` 按 §7 只许 `inner`/`left`。

#### D4 · 解读

**输入**
```json
{
  "question": "...",
  "d1": {...}, "d2": {...},
  "table": {"columns": [...], "rows_preview": [ {...}, {...} ]},
  "traces": [{"trace_id": "t-0001", "value": 1234567.89, "metric_id": "gmv", "unit": "CNY", "digits": 2}]
}
```

`rows_preview` 只放前 `meta.options.rows_preview_limit`（默认 20）行——**整表不进 prompt**，理由是 token，以及 D4 不该「看着全表编趋势」。

**输出**
```json
{
  "narrative": [
    {"text_template": "华东区最高，为 {{trace:t-0001}} 元",
     "evidence": ["t-0001"], "claim_kind": "fact"}
  ],
  "no_claim_reason": null
}
```

硬规则：
- 每个 `text_template` 里的所有 `{{trace:...}}` 引用必须 ⊆ `evidence`；`evidence` 里每个 id 必须在输入 `traces` 里存在。
- 文本里出现未被 `{{trace:...}}` 包裹的阿拉伯数字 → 校验失败（`narrative_invalid`）。
- 无证据 → `narrative=[]` 且 `no_claim_reason` 非空，且 `caveats` 含 `NO_INTERPRETATION`。
- `claim_kind="compare"` 的句子，`evidence` 必须 ≥2 条（「比谁多」至少要两个数）。

### 5.2 `--decide inject:` 注入模式约定

```
--decide d1=inject:inject/d1.json
--decide d2=inject:inject/d2.json
--decide d3=inject:inject/d3.json
--decide d4=inject:inject/d4.json
```

- **可只注入子集**：只给 `d3` 就只固定 D3，其余照常跑模型。「离线可跑」要求四个都给。
- 注入文件形状：

```json
{
  "decision": "D1",
  "contract_version": "1.1",
  "input_digest": "sha256(规范化后的本次输入 JSON)",
  "output": { "...该决策点的输出..." }
}
```

- `decision` 与参数里的 `dN` 不一致 → 报 `INJECT_SCHEMA_INVALID`。
- `input_digest` **存在但不等于**当前输入的规范化哈希 → 报 `INJECT_STALE`，**不许「差不多就用」**。留 `input_digest` 为空或不写 = 「我自己承担输入会变的后果」，此时 `meta.model.Dn.injected=true` 但额外记 `caveats` 一条 `INJECT_DIGEST_UNCHECKED`（P0 允许，因为调试时常这么干）。
- `output` 必须通过 5.1 的输出校验；失败即 `INJECT_SCHEMA_INVALID`，**不做「尽量修正」**（修正会让「可复现」变成「可复现地错」）。
- 注入命中标记：`meta.model.Dn = {"source":"inject", "model":null, "injected":true, "latency_ms":0, "output_sha256":"<output 的哈希>"}`。同时 `meta.model.Dn.inject_path` 记录注入文件的**相对路径**（不记绝对路径，否则换机器缓存键就变）。

### 5.3 缓存键

```
cache_key = sha256(canonical_json({
  "contract_version": "1.1",
  "question": <归一化后的提问>,
  "files": [ {"path": <相对路径>, "sha256": <文件哈希>} ... 按 path 字节序 ],
  "sheets": [ <所有 sheet 名，按字节序> ],
  "metric_dict_sha256": <字典文件哈希>,
  "decisions": {
     "D1": {"source": "model|inject|fallback", "output_sha256": "..."},
     "D2": ..., "D3": ..., "D4": ...
  },
  "options": {"row_cap": 5000, "trace_row_id_cap": 200, "rows_preview_limit": 20}
}))
```

- `canonical_json`：键按 UTF-8 码点升序、`separators=(",",":")`、`ensure_ascii=False`、整数不带 `.0`、不写浮点（这个键里本来就没有浮点）。
- **提问归一化**只做四件事：Unicode NFC、去首尾空白、全角转半角、连续空白压成一个。**不改**大小写、不改标点、不同义词替换——改了就变成「猜用户想问什么」。
- **不含**：绝对路径、用户名、`--verbose`、日志级别、时间戳、`elapsed_ms`、模型名、缓存命中状态。
- 单独强调：**`decisions` 进键决定了「同问同答」的真实口径是「同一问题 + 同一注入集合 + 同一字典版本 + 同一文件哈希」**。只换 D3 的注入内容，答案**应该**变，`answer_id` 也必须变——否则两份不同的答案共用一个 id，比不复现更坏。
- 文件哈希变了 → 键变 → 不可能命中旧答案。这是「换了一份 Excel 却拿到旧数字」这个经典事故的唯一防线。

---

## 6. 确定性协议

### 6.1 「同问同答」的精确定义

对同一个 `cache_key`，两次运行产出的 `answer.json`，在**稳定子集**上必须**字节级一致**。

稳定子集 = 全部字段 − `unstable_fields`：

```
meta.created
meta.elapsed_ms
meta.runtime
meta.cache            （hit / key 里的 written 会变）
meta.model.Dn.latency_ms
meta.model.Dn.inject_path 以外的路径差异 → 不，inject_path 是相对路径，稳定
```

除上述字段外，任何字段在两次运行里不同 → 判 `NONDETERMINISTIC` 缺陷。**特别点名**：`plan`、`sql`、`columns`、`rows`、`trace`、`charts`、`caveats`、`narrative`、`clarify`、`error.code` 全部属于稳定子集。

### 6.2 浮点

- **一律用 `Decimal` 计算，不用二进制 float 做任何比较或输出。**
- 输出精度 = `columns[].digits`；舍入用 `ROUND_HALF_EVEN`（不用 Python 内置 `round()`，它是二进制 float 上的 banker's rounding，跨平台不稳）。
- 禁止 `-0.0`：结果为零统一写 `0`（`int`）或 `0.00`（`decimal`，按 digits）。
- 序列化用定点形式，**禁止科学计数法**（`1e+20`、`1.5e-7` 都不许出现）。
- `NaN` / `±Infinity` 不许出现：出现即 `status="error"` + `code="RESULT_NONFINITE"`。理由：`NaN` 是「算出来了但不是数」，`null` 是「没有」，混淆会让下游把除零当缺失值静默跳过。
- `ratio` 类指标（单位 `%` 或 `ratio`）内部按 6 位小数算，输出按 `digits` 截——但**中间不许提前截断**（提前截会把 0.3333×3 算成 0.9999 这类误差放大）。

### 6.3 行序稳定

1. **SQL 必须带 `ORDER BY`**。缺 `ORDER BY` 的语句由执行器在末尾追加 `ORDER BY <输出第 1 列> ASC, __src_rowid ASC`（`__src_rowid` 是导入时写入的稳定行号）。
2. **排序必须全序**：若 `ORDER BY` 的键在结果里有并列且末尾没有 tiebreak，报 `ORDER_NOT_TOTAL`，不许「数据库碰巧这么排就接受」。
3. `rows` 最终按 `__rid` 做最后一道 tiebreak，保证「同一个 `__rid` 集合永远同一个顺序」。
4. `trace` 数组按 `value_path` 字节序排（**不是**生成顺序）——这样 D4 跑不跑都不影响 trace 顺序。
5. `caveats` 按 `code` 字节序，同 code 按 `message` 字节序。
6. `columns` 按 `sql` 里 SELECT 的顺序。
7. `metrics_used` 按 `id` 字节序。`charts` 按 `chart_id`。`narrative` 按生成顺序（这是唯一按「模型顺序」的字段，因为它读起来必须像人说话；所以它的一致性由「同一 prompt + 同一模型 + 温度 0」保证，而不是由排序保证——这是本契约里最脆的一处，已记在 README 的已知短板里）。

### 6.4 允许因环境而不同的字段（完整清单）

| 字段 | 为什么允许 |
|---|---|
| `meta.created` | 时间 |
| `meta.elapsed_ms` | 机器性能 |
| `meta.runtime.*` | sqlite / python / os 版本 |
| `meta.cache.*` | 命中与否天然依赖「跑过几次」 |
| `meta.model.Dn.latency_ms` | 同上 |

**不在这张表里的字段，一律不许有环境差异。**

### 6.5 序列化规格

- UTF-8 **无 BOM**，行尾 `\n`，缩进 2 空格。
- 键序 = 本契约里**声明的顺序**（不是字典序）——这样 `git diff` 才有意义。
- 文件末尾恰好一个 `\n`。
- 不许出现 `\u4e2d` 这种转义（`ensure_ascii=False`）。

---

## 7. 错误与降级

### 7.1 D1 信息不足 → 必须反问，不许猜

**触发条件**（任一成立即 `status="clarify"`；v1.1 重写，改动见每条后的括注）：

1. `metric_ids` 为空**且 `listing != true`**，或 D1 给了一个不在字典里的 id；（v1.1：`listing=true` 是纯明细列举的唯一合法出口，q002/q003 类题不再被迫反问）
2. 问题里含时间词但 `time_range` 为 `null` **且** `comparison` 为 `null`；（v1.1：两期对比时时间词落在 `comparison` 的 `left`/`right` 里）
3. 有两个以上候选指标并列（`ambiguity[].candidate_ids` 长度 ≥2）；
4. `time_column` 为 `null` 且 schema 里有**两个以上候选时间列**、问题又无线索；（v1.1 替换 v1.0 的「数据集不唯一」——**多文件/多表不再是反问理由**，选表与 JOIN 归 D3，见 §5.1 D1 硬规则第 5 条）

**反问的形状要求**：
- `clarify.question` 必须能让人**回一句话**就定，例如「A 还是 B？」而不是「请补充更多信息」。
- 每个 `missing[].candidates[]` 必须给 `id + name + definition`，让用户看到「这两个口径到底差在哪」。
- 一次最多问 3 个槽位；超过 3 个说明问题本身太大 → `status="error"`、`code="QUESTION_TOO_BROAD"`，并建议拆题（不硬问 5 个槽）。
- 反问**不消耗 SQL 尝试次数**。用户回答后算一次**新提问**，`answer_id` 重新计算（不沿用）。

**明确禁止**：拿「最像的那个指标」算一版，再把疑问塞进 `caveats`。理由见 README 铁律 3——那正是「模型说什么就是什么」。

### 7.2 D3 兜底必须显式标注「未精简」

四处联动（已在 5.1 D3 里写死，这里是执行检查点）：

```
D3 输出.pruned == false
D3 输出.prune_strategy == "all"
answer.caveats 含 {"code":"NOT_PRUNED", ...}
meta.model.D3.source == "fallback"
```

外加 `plan_summary` 或 `caveats[NOT_PRUNED].message` 里必须出现「未精简」三个字（这是给人看的，也是 `grep` 断言的落点）。

### 7.3 SQL 失败：重试上限与回退顺序

| 失败类型 | 第一次处理 | 是否重试 |
|---|---|---|
| 静态检查不过（第 8 节） | **不重试同一条 SQL**，直接回退 **D3** 重选列/表 | 是（算一次 attempt） |
| `no such column` / `no such table` | 回退 **D3**（列/表名错） | 是 |
| `ambiguous column` | 回退 **D3**（要求补表别名） | 是 |
| `misuse of aggregate` / `GROUP BY` 相关 | 回退 **D2**（计划要求分组但没分） | 是 |
| 其它执行错误 | 回退 **D1**（问题理解错了） | 是 |

上限与刹车：

- `max_sql_attempts = 3`（1 次原始 + 2 次回退）。
- **同一错误分类连续出现 2 次 → 立即停止**，报 `code="SQL_RETRY_LOOP"`（继续回退只会绕圈，且每圈都在烧 token）。
- 用尽 3 次仍失败 → `status="error"`、`code="SQL_EXEC_ERROR"`，`error.attempts` 必须**完整**列出每次的 `{attempt, source_decision, sql_index, error_code, error_message, d3_snapshot}`。`d3_snapshot` 必须保留，否则「昨天那个错今天复现不出来」。
- 回退**只回退决策，不回退数据**：不回退到「重新读文件」，因为文件哈希没变时重读只会得到同一份 schema。

### 7.4 结果为空（0 行）不是错误

- `status="ok"`、`rows=[]`、`columns` 保留、`trace=[]`、`N=0`、追溯完整率 `null`。
- `caveats` 加 `RESULT_EMPTY`，`narrative=[]` + `NO_INTERPRETATION`（没有数就没有结论）。
- **禁止**自动放宽过滤条件再查一次。唯一例外：D2 计划里显式写了兜底步骤（那时它就是一个正常的 `plan[]` 步骤，有 `fallback_of`，用户能看见）。

### 7.5 缓存不一致

命中缓存但字典版本已变 → **不许直接返回旧答案**。必须：重新校验 `metrics_used[].definition` 与字典当前版本；不一致则记 `DICT_VERSION_DRIFT`，并在 `narrative`/`caveats` 里说明「这是按 v2 口径算的，当前字典是 v3」。要么标清，要么重算，不许默默给旧数。

---

## 8. 安全

### 8.1 拦截清单（**静态检查，第一道闸**）

大小写不敏感、按**词边界**匹配。命中即 `SQL_STATIC_REJECT`，直接拒绝，不尝试改写。

**S1 · 写操作关键字**
```
INSERT  INSERT OR REPLACE  REPLACE  UPDATE  DELETE  UPSERT
CREATE  ALTER  DROP  TRUNCATE  RENAME
ATTACH  DETACH  VACUUM  REINDEX  ANALYZE
```
> `REPLACE` 与 `UPDATE` 要按词边界匹配，否则 `REPLACE(...)` 字符串函数会被误杀——所以白名单里同时允许 `REPLACE` 作为**函数**出现（`REPLACE(` 形态放行）。这条是唯一允许的「看起来像黑名单词但不是」的例外，必须在测试里各写一条正例反例。

**S2 · 事务/权限/管理语句**
```
BEGIN  COMMIT  ROLLBACK  SAVEPOINT  RELEASE
PRAGMA  GRANT  REVOKE  SET  COPY  MERGE  CALL  EXEC  EXECUTE
```

**S3 · 语句形态**
- 只允许以 `SELECT` 或 `WITH` 开头。`WITH` 必须最终落到 `SELECT`（CTE 里出现 `INSERT/UPDATE/DELETE` 即拒）。
- 多语句：除**末尾单个** `;` 之外，任何 `;` 都拒。判定前先剥离字符串字面量与（如果允许的话）注释。
- 注释 `--` 与 `/* */` **一律拒绝**（不让注释藏关键字）。
- `SELECT ... INTO` 拒。
- `UNION` 之后的每段都必须以 `SELECT` 开头。

**S4 · 副作用函数 / 破坏确定性**
```
load_extension(   readfile(   writefile(   edit(   fts3_tokenizer(
random()          randomblob(  changes()    total_changes(   last_insert_rowid(
```
> `random()` 与 `'now'` 这类「每次都不一样」的东西必须拦，否则**同问同答直接作废**。时间常量只能来自 D1 的 `time_range`，SQL 里出现 `'now'` / `date('now')` / `datetime('now')` 一律拒。

**S5 · 表名白名单**
`FROM` / `JOIN` 之后的表名必须 ∈ 本次导入的表 ∪ `metric_dict` 声明的表。`sqlite_master`、`sqlite_schema`、`sqlite_temp_master`、任何 `sqlite_*` 前缀一律拒。

**S6 · 标识符注入**
列名/表名进 SQL 前必须：① 来自 schema 白名单（不接受 `answer.json` 或用户输入里的任意字符串）；② 用双引号包裹并把内部 `"` 转义成 `""`。

### 8.2 第二道闸：只读连接（静态检查有 bug 的兜底）

- `sqlite3.connect("file:<path>?mode=ro", uri=True)`，并且连上后立刻 `PRAGMA query_only = 1`。
- 加上 `PRAGMA trusted_schema = OFF`。
- 单条语句超时（默认 30 s），超时即 `SQL_EXEC_ERROR`。
- 结果行数上限 `row_cap`（默认 5000）；超限则截断 + `caveats` 记 `TRUNCATED_BY_ROW_CAP` + `rows` 之外的元信息标 `truncated: true`。

> 两道闸都留的理由：S1–S6 是文本层，绕过的办法一直在长；只读连接是物理层，但只看得到「写」，看不到「读太多」「不确定」。缺一道就是缺一半。

### 8.3 文件侧

- 只读打开源文件；`.xlsx` 用 zip + XML 解析，**不解压到磁盘**（防 zip slip），不执行宏、不解析外部链接。
- 单文件大小上限与行列上限必须显式配置（默认值可以调，但不许「不设上限」）。
- 文件路径不许出现 `..`（目录穿越）。

### 8.4 工作表 / 导出侧

- 工作台只绑 `127.0.0.1`，只读；任何 POST 一律拒绝（P0）。
- 导出 `answer.json` / `report.md` / SVG 时：SVG 里不许出现未转义的 `<script>`、`on*` 属性、`javascript:` URL；`report.md` 里不执行任何 HTML（纯 Markdown + 数学用纯文本）。
- 导出文件不写回源数据目录。

---

## 9. 变更记录（**只记不兼容改动**；每升一版必须留一份迁移清单）

### v1.1（2026 年，第 7 步评测打回后由契约所有者改）

**起因**：第 7 步 60 题实测后，`eval/rework.json` 里有 6 条打回挂在 `contract/`，全部是「契约表达不了某些题」——不是模型笨，是契约缺字段。改契约之前，这 6 条无论重跑多少遍都不会过。

**改了什么**：

| # | 改动 | 修掉的打回 | 位置 |
|---|---|---|---|
| 1 | D1 新增 `listing`：纯明细列举（`metric_ids=[]` 的合法出口） | RW-0001（q002/q003） | §5.1 D1 |
| 2 | D1 新增 `time_column`：时间范围作用在哪一列 | RW-0002（q008） | §5.1 D1 / §7.1 |
| 3 | `filter.op` 新增 `is_null` / `is_not_null`（`value_literal` 必须显式 `null`） | RW-0003（q009） | §4.3 T4 |
| 4 | D1 新增 `time_grain_grouping` + D3 派生列 `period`（`GROUP BY period`） | RW-0004（q013/q025/q028） | §5.1 D1/D2/D3 |
| 5 | D1 新增 `comparison`（两期对比必须用一条 SQL 的窗口函数/CTE 算完，差额与变化率进 `columns[]`，不许外层口算） | RW-0005（q023/q024/q027/q029/q030） | §5.1 D1/D2 |
| 6 | §7.1 删除「数据集不唯一」触发条件：**多表/多文件不是 D1 的歧义**，选表与 JOIN 归 D3 | RW-0011（多表题被 D1 反问） | §7.1 / §5.1 D1 |

**迁移清单（升到 v1.1 必须同步改的地方，缺一处就是「版本对不上」）**：

| 归谁 | 文件 | 改什么 |
|---|---|---|
| `ask/` | `ask/decide.py`（`CONTRACT_VERSION`）、`ask/metric_dict.py`、`ask/ingest.py` | 常量改 `"1.1"`；D1 校验器接受并**强制要求**四个新字段；`filter.op` 白名单加两个 op；D3 支持三种派生列（`period`、`<metric_id>__delta`、`<metric_id>__pct`）；`comparison` 必须落到一条含 `LAG(`/`OVER(`/`WITH ` 的 SQL |
| `ask/` | `ask/inject/d1–d4.json`、`ask/aliases.json`、`ask/answer_out/smoke.json` | `contract_version` 改 `"1.1"`，D1 注入样例补四个新字段 |
| `dict/` | `dict/metrics.json`、`dict/history.snapshot.json`、`dict/schema.snapshot.json` | 顶层 `contract_version` 改 `"1.1"`；校验规则跟着 §3 走 |
| `eval/` | `eval/data/metrics.json`、`eval/data/make_data.py`、`eval/inject/q012.json`、`eval/rework.json` | 版本号改 `"1.1"`；打回清单重算（v1.1 落地后这 6 条应能过） |
| `show/` | — | 不用改：只显示 `meta.contract_version`，不校验值 |

**不改的东西（有意为之）**：`answer.json` 的顶层字段一个没动、trace 的 T1–T3/T5–T8 一条没动、60 题题库一题没动、JOIN 单列统计的策略没动（理由见 `slices.md` §4.4：失败模式仍与其它 5 类不可比）。

**这一版没有跑过任何实测**。v1.1 之后的准确率必须重新跑（旧数字 38%→90% 是 v1.0 口径下的，不能拿来当 v1.1 的成绩）。
