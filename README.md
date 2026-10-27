# tally

**把 Excel / CSV 交给它，用中文提问，它给结论和图表 —— 而每个数字都能点开，看到它从哪张表、哪几列、哪一步算出来的。**

零依赖（标准库解 xlsx，纯 SVG 画图）、单机自用、只读工作台。
面向的场景是财务与运营：那里的数字**必须能追**，否则再好看的看板也没人敢用。

```
   .xlsx / .csv
        │
        ▼
  ┌───────────────┐    ┌────────────────────────────┐
  │ schema 推断    │───▶│ D1 问题理解 ─ D2 分析计划   │
  │ 中文列名匹配   │    │ D3 选表选列 ─ D4 解读       │
  └───────────────┘    └─────────────┬──────────────┘
                                     │ 结构化决策（可注入）
                                     ▼
                        ┌────────────────────────┐
                        │ 只读 SQL + 追溯链       │
                        │ SQL / 命中行 / 单元格   │
                        └───────────┬────────────┘
                                    ▼
                     answer.json → 图表 · 报告 · 工作台
```

---

## 1. 实现了什么

### 1.1 三条硬规矩（不可让步）

| 规矩 | 含义 | 怎么保证 |
|---|---|---|
| **可追溯** | 每个输出数字 → SQL + 命中行 + 计算步骤 + 原始单元格 | `answer.json` 里每条数值配一条 `trace`，`value_path` 指向 `rows[i].列名` |
| **同问同答** | 同一问题永远同一答案，不随机、不漂移 | 确定性执行 + 结果缓存；自检跑两遍比字节 |
| **口径字典** | 「毛利率」「活跃用户」怎么算，写在字典里 | `dict/metrics.json` + 校验器（V1–V12），模型不许自创 |

> ChatBI 的通病是「模型说什么就是什么」，所以业务方不敢用在财务与运营上。这三条是它敢被用的前提。

### 1.2 四个决策点，全部结构化、可注入

不是「模型生成一条 SQL 就完事」，而是四个决策点各出一份结构化决策，**每一个都能注入固定响应**（离线可跑、失败可复现）：

| 决策点 | 做什么 | 拿不准时 |
|---|---|---|
| **D1 问题理解** | 哪个指标 / 什么时间范围 / 什么维度 | 信息不足就**反问**，不硬猜 |
| **D2 分析计划** | 先看分布？查异常？做对比？出可读步骤 | 退化为最小计划 |
| **D3 选表选列** | 从 schema 挑**最小必要列**，跨表时推 join 键 | 全列兜底并标注「未精简」 |
| **D4 解读** | 每句结论挂证据，无证据不下结论 | 只给数，不说话 |

### 1.3 能力清单（12 项）

文件接入（`.xlsx` / `.csv`，xlsx 用 zip + XML 手解）· schema 推断 · 中文列名匹配（同义词 + 编辑距离 + 人工别名）·
指标字典 + 校验器 · 问题 → **只读** SQL（静态检查拦写操作）· 执行与结果表 · 追溯链 ·
决策四段可注入 · 反问机制 · 纯 SVG 图表 · 本地只读工作台 · 导出 `answer.json` / `report.md` / SVG

### 1.4 不是什么

- **不做写操作**：SQL 只允许 `SELECT`，静态检查拦截 `INSERT/UPDATE/DELETE/DROP`。
- **不做多用户 / 云端**：工作台只绑 `127.0.0.1`，单人用。
- **不是通用 BI**：不做大屏、不做数据仓库建模。
- **不给 20 题的「总体准确率」**：JOIN 题单独统计，不混进总分。

---

## 2. 如何使用

### 2.1 环境

Python 3.8+，**零第三方依赖**（xlsx 用标准库 zip + XML 解，图表是手写 SVG）。

```bash
git clone https://github.com/Eric2007-sudo/tally.git
cd tally
python -m dict.validate --selftest      # 口径校验器
python -m ask.ingest --selftest         # xlsx 解析器
python ask/_selfcheck/run_all.py        # 60 题全量自检（离线，不调模型）
```

### 2.2 问一个问题

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'

python -m ask.query --question-file ask/fixtures/question.first.txt `
  --data eval/data/sales.csv `
  --metric-dict eval/data/metrics.json `
  --out ask/answer_out/smoke.json
```

产物 `answer.json` 里每个数值都带 `trace`：SQL、命中行 id、计算表达式、来源文件与列。

### 2.3 不调模型也能跑（决策注入）

四个决策点都能用固定文件替换，所以评测与回归**离线可复现**：

```powershell
python -m ask.query --question "各区域成交额是多少" `
  --data eval/data/sales.csv --data eval/data/region_dim.csv `
  --metric-dict eval/data/metrics.json `
  --decide d1=inject:eval/inject/q031.json `
  --out ask/answer_out/q031.json
```

### 2.4 导出与工作台

```powershell
python -m show.export --answer ask/answer_out/smoke.json --out show/out
python -m show.server --port 7788          # 只读，只绑 127.0.0.1
```

### 2.5 自检（60 题）

```powershell
python ask/_selfcheck/run_all.py              # 全量；有失败会 exit 1
python ask/_selfcheck/run_all.py q031 q032    # 只跑指定题
```

题集在 `eval/questions.jsonl`（60 题：筛选 / 聚合 / 同比环比 / 多表连接 / 排名 / 异常），
判据在 `eval/metrics.py`，夹具数据在 `eval/data/`。

---

## 3. 实测结果

全部离线可复现，数字来自 `ask/_selfcheck/out/` 与账本。

### 3.1 总览

| 分组 | 结果 |
|---|---|
| 非 JOIN（50 题） | **49/50** |
| 多表 JOIN（q031–q040，10 题） | **10/10** |
| **合计** | **59/60** |

唯一失败：`q056`（问「哪些客户有单笔订单金额超过其自身平均订单金额 5 倍」，需要按客户分组的**相关子查询**，当前返回 0 行）。

### 3.2 多表 JOIN 怎么做的

`ask/joins.py` + `ask/sqlgen.py`：

- D3 建「列 → 表」索引；D1 要的维度不在主表时，按同名列推出等值键，写进 `joins[]`；
- sqlgen 按 `joins[]` 拼 `JOIN`，**列名带表名限定**（两表都有 `region` 时不加就歧义）；
- 「哪些 X **没有** Y」这类反向条件走 `LEFT JOIN … WHERE 右表键 IS NULL`（用 `INNER JOIN` 会正好答反）；
- 一对多会把金额重复累加，用 `JOIN_ROW_MULTIPLIED` 拒掉。

### 3.3 一个判分器抓不到的回归（已修）

做 JOIN 时 `build_where` 的 `include_time` 被写成
`bool(source.anti and source.anti.get("time_in_outer"))`，对**所有非反连接查询**都算成 `False`，
于是**时间过滤整段丢失**：

> 54 道带 `time_range` 的题里，**47 道的 SQL 一个日期字面量都没有**。

判分器只看 SQL 结构（有没有 SUM / GROUP BY / JOIN），**不检查时间过滤**，所以 59/60 一直是绿的——
但 q007 问「2025 年 6 月」返回了全年 **48 行**，q025 问「2024 年」把 2025 年也扫了进去。

修法与守卫：

- `_time_in_where()` 明确「非反连接一律进 WHERE，反连接才可能放进 `ON`」；
- 门禁加一条 `cmd:python ../_scratch/audit_time_filter.py`：逐题比对「D1 给了 range」与「SQL 里有没有日期字面量」，有遗漏就非 0 退出。

修完 **47 → 0**。

### 3.4 其它必须说清的口径

- **追溯缺口只标记、不判失败**：`q008` / `q030` / `q058` 各有个别数字缺链（`q030` 还带 1 个 null）。
  这是已知的判据缺口——现在的判分只看「SQL 结果对不对」。
- **自检脚本此前永远 `exit 0`**：11 题不过，门禁照样绿。已修为有失败即 `exit 1`。
  一个不会失败的自检，等于没有自检。
- **口径命中率 / 同问一致性**由 `eval/run_questions.py` 另跑，不计入上面的 59/60。

### 3.5 这个项目自己花了多少

| 项 | 值 |
|---|---|
| 12 步门禁 | 全过，`tg reconcile` 重跑 12 步无回归 |
| 花费 | **$1.732028 / 上限 $1.80（96%）** |
| token 预算 | 实际 6711.2k / 预算 1398k（**480%**）—— 预算按「一次成型」估轮数，没算返工 |

---

## 4. 参考资料

### 4.1 设计来源

| 机制 | 来源 | 本项目的改动 |
|---|---|---|
| 计划 / 门禁 / 账本 / 归账 | [tollgate](https://github.com/Eric2007-sudo/tollgate) | 拿它当计划与验收基础设施 |
| 「每个数字可追溯」 | 财务审计的最小要求，不是新概念 | 落成 `trace.value_path` → `rows[i].列名`，机械可核 |
| 指标口径集中管理 | dbt / MetricFlow 的 metrics 思路 | 改成 JSON 字典 + V1–V12 校验器，写错必须报错 |
| 只读 SQL 守卫 | 只读账号 + 静态检查的通行做法 | 零依赖手写词法检查，白名单表名，拒一切写操作 |
| 决策点结构化 + 可注入 | 把「模型输出」当接口而不是文本 | 四个决策点各自成文件，评测可离线复现 |
| 纯 SVG 图表 | 标准库手写 | 不引前端依赖，图上不出现外链 |

### 4.2 相关生态（不做对比排名）

- **Vanna / Chat2DB / WrenAI**：都在做自然语言查数。本项目**不读它们的源码**，只借鉴公开的产品形态；
  差异点押在「可审计 + 确定性」这一段，而不是模型能力。
- **tau-bench 式的交互评测**：本项目**没有**做——用户侧是真人提问，不是脚本化模拟。

### 4.3 仓库里读什么

| 文件 | 内容 |
|---|---|
| `contract/answer.md` | `answer.json` 的字段级契约（追溯链、状态机） |
| `contract/slices.md` | 步骤切分与断言纪律 |
| `eval/README.md` | 60 题题集口径、判据定义、为什么 JOIN 单独统计 |
| `dict/README.md` | 指标字典格式与 V1–V12 校验规则 |
| `ask/_selfcheck/notes.md` | 每轮自检的结论与踩坑记录 |

### 4.4 版本库收什么

只收代码、契约、文档与夹具。`.plan/`、`.orch/`、`ask/_selfcheck/out/`、`show/out/`、`eval/fourd/q*/`
是本机运行产物，不进版本库——它们是**这台机器上的历史**，换台机器没有意义。

---

## 5. 已知短板

1. ChatBI 不是空白，**没被做透的只有「可审计 + 确定性」这一段**，力气全押这里。
2. 多表 JOIN 与中文列名模糊匹配是最容易翻车的地方，所以单独统计、不混总分。
3. `q056`（相关子查询）仍不过：`需要「按自身分组均值过滤」`这类条件目前表达不出来。
4. 追溯缺口只标记不判失败，等于这一项还没有门禁。
5. 模型生成的 SQL 天然有限，靠「静态检查 + 执行失败可读报错 + 回溯到 D3 重选列」兜。

## License

MIT
