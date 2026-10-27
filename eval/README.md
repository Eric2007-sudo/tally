# eval/ · 评测路（#7 建；#10 按契约 v1.1 重跑 60 题 + 报告补「降级链路 / 模型链路」与「判据冲突」口径；#11 抽 20 题由 agent 扮演 D1–D4 真跑四段，名单钉死在 `fourd/fourd.py` 的 `ROSTER`）

60 题问题集 + 四项指标测量 + 同问两遍字节比对 + 打回清单。
**只写这里。** 本目录的代码不读、不改 `ask/` `show/` `dict/` `research/` 的源码，
测量对象是**产物**（契约 `contract/answer.md` §2 的 `answer.json`），通过命令行参数喂进来。

## 目录里有什么

| 文件 | 作用 |
|---|---|
| `questions.jsonl` | 60 题，6 类 × 10 题（筛选 / 聚合 / 同比环比 / 多表连接 / 排名 / 异常），每行带 `expect` 真值口径 |
| `check_questions.py` | 问题集自检：题量 / 分类 / 字段 / 分类与期望是否自洽 |
| `data/make_data.py` | **合成数据 + 配套字典生成器**（确定性，无随机数）：8 张 CSV + 14 条口径 |
| `d1_truth.json` | 每题的 **D1 输出**（问题理解真值），跑题时用 `--decide d1=inject:` 注入 |
| `run_questions.py` | 跑题器：60 题各跑两遍（run1 / run2 两次独立进程），产物落 `runs/` |
| `inject/qNNN.json` | 注入文件（跑题时按 `d1_truth.json` 生成，留在仓库里便于复现） |
| `runs/run1\|run2/qNNN.json` | 两遍 `answer.json` 产物 + `runs/run_log.json`（含测量协议） |
| `metrics.py` | 四项指标 + 同问两遍字节比对 + 写出 `report.md` 与 `rework.json` |
| `report.md` | 测量报告（由 `metrics.py` 生成，别手改） |
| `rework.json` | 打回清单（由 `metrics.py` 生成，每条带 `target_dir`） |
| `fourd.py` | #11 的跑题器：抽 20 题（含全部 10 道 JOIN），把 agent 扮演的 D1–D4 落成注入文件，再真跑四段并写出 `report.fourd.md` |
| `fourd/fourd.py` | **20 题的名单常量** `ROSTER`（+ 每题「为什么是它」的 `ROSTER_BASIS`、一句话口径 `ROSTER_RULE`）；`fourd.py` 按文件路径加载它（`eval.fourd` 是模块不是包，不能 `import eval.fourd.fourd`），`--check` 第一步就比对，不等 exit 1 |
| `fourd/decisions.json` | agent 扮演的逐题四段决策（D1–D4）+ 每题「为什么演它」的 `why`；题号集合必须**正好**等于 `ROSTER` |
| `fourd/qNNN/dN.json` | 每题四个决策点的注入文件（契约 §5.2 信封，`input_digest` 按 P0 留空）；同目录 `run1.json` / `run2.json` 是真跑产物，`answer.json` 是 `run1.json` 的字节副本（那一步 note 点名的路径） |
| `fourd/measure.json` / `fourd/rework.fourd.json` | 20 题的测量明细与打回清单（由 `fourd.py` 生成） |
| `fourd/probe_clarify.py` | 诊断脚本：JOIN 题为什么会在 D1 被反问时间列（对照产物落 `fourd/probe/`） |
| `report.fourd.md` | #11 的报告（由 `fourd.py --report` 生成，别手改） |
| `_selftest/make_fixtures.py` | 一次性冒烟 fixture 生成器（**数字全是编的**，跑完自己删） |

## 跑法（完整一轮）

```bash
# 0) 造合成数据与字典（幂等，同公式永远同字节）
python eval/data/make_data.py

# 1) 问题集自检
python -m eval.check_questions --count 60 --cats 6 --per-cat 10

# 2) 真值一致性自检（d1_truth.json ↔ questions.jsonl expect）
python -m eval.run_questions --check-only

# 3) 60 题各跑两遍（两遍是两次独立进程调用，互不复制）
python -m eval.run_questions

# 4) 测量（本路断言里的那条）
python -m eval.metrics --determinism-pairs 60 --out eval/report.md \
    --run1 eval/runs/run1 --run2 eval/runs/run2 --metric-dict eval/data/metrics.json

# 5) 判据实现自测（编的数字，验的是「检查器对不对」）
python -m eval.metrics --selftest
```

`--run1` / `--run2` 默认是 `eval/runs/run1` / `eval/runs/run2`；目录空时四项指标一律写 `null`
并注明「无可测样本」——**不编数字，也不把「没跑」记成「0 分」**（契约 §4.4）。

## 跑法（#11：四个决策点全部由 agent 扮演）

```bash
# 1) 名单比对 / 抽样 / D2 形状 / JOIN 责任 / 注入文件自检（评测器自己的闸，不过就别跑）
python -m eval.fourd --check 20

# 2) 20 题 × 2 遍真跑四段 + 写报告
python -m eval.fourd --report eval/report.fourd.md
```

20 题的名单是这一步的**输入**，所以它钉死在 `eval/fourd/fourd.py` 的 `ROSTER` 里
（`q031-q040` JOIN 全集 + `q001 q008` 筛选 + `q013 q020` 聚合 + `q023 q025` 同比环比 +
`q041 q042` 排名 + `q053 q057` 异常），**不由 `questions.jsonl` 的类别现算**：`--check` 的
第一步就是比 `decisions.json` 的题号集合与 `ROSTER`，多一道少一道都 exit 1 并把差集打出来。
`fourd/fourd.py` 只能按文件路径加载（`eval.fourd` 是模块、不是包），见文件头注释。

`fourd.py` 只**写注入文件**（`fourd/qNNN/d1..d4.json`），`answer.json` 一律来自
`python -m ask.query` 的真跑，本目录不手写任何答案。判据复用 `metrics.py` 的
`judge_question` / `compare_pair` / `metric_hit_tiers` / `trace_completeness`，不另写一套。
它量的是「四段都给对之后，实现能不能把它们执行成可追溯的数字」，不是模型效果；
`report.fourd.md` §0 把「模型链路（本次，agent 扮演 D1–D4，经 inject 注入）」与
「降级链路（旧 60 题两遍，见 `report.md`）」分列，两边的数字不互相引用。

## 测量协议（为什么带 `--decide d1=inject:`）

- 契约 §7.1：**D1（问题理解）没有兜底**。没有模型回调时 D1 直接失败，`answer.json` 只会是 `error`。
- 契约 §5.2 给了唯一合法的离线跑法：`--decide d1=inject:<文件>`，把「问题理解」这一步的输入固定下来。
- 所以本轮的测量协议是：**D1 注入 + D2/D3/D4 走 `ask/` 自己的兜底**。量的是
  「给定问题理解，取数链路能不能建计划、选表选列、生成并执行 SQL、给出可追溯的数字」，
  **不量**「模型理解得对不对」（那一环本轮没有模型）。
- D1 注入内容就是 `d1_truth.json`，它和 `questions.jsonl` 的 `expect` 一一对应，
  `run_questions.py --check-only` 会交叉校验（指标名、维度、ambiguity 与 status 是否自洽）。
- `--now` 固定成常数 `2026-01-01`，只为可复现；时间口径只允许来自 `D1.time_range`（契约 §6.1）。
- 两遍独立：run1 与 run2 各自 `--out` 到不同目录、各自一个进程，产物互不复制；
  唯一共享的是只读输入（数据、字典、注入文件）。
- **契约 v1.1 口径**：`d1_truth.json` 与 `eval/inject/*.json` 都按 v1.1 的四个新必填字段
  （`listing` / `time_column` / `time_grain_grouping` / `comparison`）写，缺一个 `ask/` 就报
  `INJECT_SCHEMA_INVALID`；`make_data.py` 生成的 `metrics.json` 也标 `contract_version="1.1"`。
  `time_grain_grouping != null` 的题，`expect.dims` 写派生列 `period`（契约 §2.4），
  `run_questions.py --check-only` 会把「D1 维度 + period」和 `expect.dims` 对齐后再比。
- **降级链路 / 模型链路**：报告 §0.3 逐题读 `meta.model.Dn.source` 汇总（`model` / `inject` /
  `fallback`）。本轮 60 题的 D1 全是 `inject`、D2/D3/D4 全是 `fallback`，**模型链路 0 次**，
  所以分数衡量的是「注入 + 降级链路」，不是模型效果；换真模型必须重跑。
- **判据冲突（报告 §0.4）**：契约里有几处「同一件事两种写法」+ 一处没写分母，本轮都在真产物上对着了，
  报告把两条判据的数字都列出来并写明采用哪一条，不挑好看的那个：
  ① §6.1 稳定子集 vs 「整份文件原始字节」（0/60 vs 60/60，取 §6.1）；
  ② §4.4 分母 N（数字单元格） vs §4.3 逐条判的 trace 条数（216/219 vs 216/218，取 §4.4）；
  ③ §3.3 没写「口径命中率」的分母（本步定义 218，若改 N=219 则是 218/219，取本步定义）；
  ④ §2.10 `clarify.missing[].slot` 的封闭六选一 vs §7.1 触发条件 4 要求写 `time_column`
  （q032 / q035 / q040 的反问体就是这个槽位：按 §2.10 判「不合法」，按 §7.1 判「正确反问」）；
  ⑤ §4.3 T3 要求 `row_ids` 非空 vs §4.4「空结果集没有追溯不完整这回事」
  （q008 / q058 是「零行命中、聚合值就是 0」，没有源行可指：严格 216/219，按 §4.4 读则是 218/219）。
  这五条由 `metrics.py` 的 `judge_conflicts()` 从产物现算，不是手写常量；没有产物时一条都不写。

## 合成数据（`eval/data/`）

- `sales.csv`（48 行，宽表快照：region / category / customer_level / city / industry / sales_owner …）
  —— 非 JOIN 题只靠它就能算；埋了四种脏数据：重复单号（i=20）、负金额（i=9）、空金额（i=17）、测试账号（`U06`）。
- 维表：`customer.csv` / `region_dim.csv` / `product_dim.csv` / `employee_dim.csv` / `brand_dim.csv`；
  另外 `payment.csv` / `refund.csv` 供 JOIN 题用。
- `metrics.json`：14 条口径，**只描述这份合成数据**，不是项目字典（`dict/` 本步按边界不读）。
- 真值 = `make_data.py` 里的固定公式；`questions.jsonl` 的 `expect` 就是真值口径。

## 四项指标怎么算（判据全部来自契约，不自创）

| 指标 | 判据 | 分母 |
|---|---|---|
| SQL 执行准确率 | `expect` 机械核对：`status` / 指标 / 维度 / `GROUP BY`、`JOIN`、`ORDER BY` 方向与 `LIMIT` / 行数形态 / `sql_all_any` 结构要求 | 有产物的题（覆盖率单列，缺产物不算答错） |
| 同问一致性 | 契约 §6.1 稳定子集：去掉 §6.4 允许变动的字段后**逐字节**相等 | 两遍都有的题的对数 |
| 口径命中率 | 契约 §3.3 三条：`columns[].source_metric_id` = `trace[].metric_id`、快照哈希自洽、与当前字典逐字相等 | 带 `metric_id` 的数字单元格数（契约没定分母，这里定并写明） |
| 追溯完整率 | 契约 §4.3 T1–T8 全过 / 契约 §4.4 的 N（数字单元格数，不是 trace 条数） | N |

**JOIN 题（`多表连接` 类 10 题）单独统计，不混进总分** —— `report.md` 里单列一行。

> 判据实现的口径提醒：T4 的 `op` 枚举必须按契约 §4.3 收全（`= != > >= < <= in like is_null is_not_null`），
> 且 `is_null` / `is_not_null` 的 `value_literal` 必须**显式为 `null`**、其余 `op` 的 `value_literal` 不许为 `null`。
> 少抄这两个 op 会把用了空值过滤的 trace 在 T4 上误判成「不过」，那是评测器自己的缺陷，不是 `ask/` 的。

### 答错的题再分三档（`report.fourd.md` §1）

非 JOIN 组答错的题不只报一个「错」，还要说清**错在谁头上**。三档由
`eval/fourd.py` 的 `nonjoin_tiers()` 从产物与 `expect` 现算，不按题号手写：

| 档 | 判据 | 退给谁 |
|---|---|---|
| 复现差异 | 两遍稳定子集不一致（同问不同答，题本身没定死） | `ask/` |
| 契约表达不了 | `expect.sql_all_any` 或 `expect.limit` 要求的语义，D1/D2/D3 里没有字段能写 | `contract/` |
| 实现缺陷 | 扣掉前两档仍然答错的（契约写了、代码没做） | `ask/` |

**三档不许加总、也不许合成一个「准确率」**：分母不同、责任方向也不同。答对的题另算一档
「实现范围内」，它的分母是「题面语义在契约里表达得出的题」，不是全部题。
同理，`report.fourd.md` §7.1 那张按根因分的表（实现缺陷 / 契约表达不了 / 观测口径 / 本步自己）
的分母是**打回清单条数**，与 §1 的三档表分母不同，两处的「实现缺陷」不可互比。
「观测口径」指产物写对了、但两件不同的事在产物上共用一个值（例：`meta.model.Dn.source`
把「没执行」和「兜底生效」都记成 `fallback`），既不是代码错也不是契约缺字段。

## `questions.jsonl` 每行一个 JSON 对象

```json
{"id":"q011","cat":"聚合","q":"2025 年各区域的成交额是多少？","intent":"detail",
 "expect":{"status":"ok","metrics":["成交额"],"dims":["region"],"group_by":true,
           "order_by":null,"join":false,"tables":["sales"],"rows":"many"}}
```

| 字段 | 说明 |
|---|---|
| `id` | `q001`…`q060`，与行序一致 |
| `cat` | 六类之一；`多表连接` = JOIN 题 |
| `q` | 问题原文，逐字交给 D1（契约 §2.1 要求逐字保存） |
| `intent` | 契约 §2.2 的六选一：`distribution` / `trend` / `compare` / `anomaly` / `rank` / `detail` |
| `expect.status` | `ok` / `clarify` / `error` |
| `expect.metrics` | 期望用到的指标**中文名提示**（是提示不是硬绑 id，见局限 1） |
| `expect.dims` | 期望的分组维度（原始列名提示） |
| `expect.group_by` / `order_by` | `true` / `false` / `null`（`null` = 不检查） |
| `expect.join` | 是否必须连表；JOIN 类必须 `true`，其它类必须 `false` |
| `expect.tables` | 涉及的表名提示（JOIN 类必须 ≥2），跑题器据此决定传哪几个 `--data` |
| `expect.rows` | `scalar`（单行）/ `many`（非空）/ `any` |
| `expect.sql_all_any` | 可选：数组的数组，每组要求 SQL 里**至少出现一个**子串。用来抓「结构对了但语义没落地」（同比环比要有窗口、异常要有基线比较、重复单号要有 `HAVING`） |
| `expect.order` | 可选：`desc` / `asc`，排名题必须真的这么排 |
| `expect.limit` | 可选：排名题必须真的截断条数 |
| `expect.gap` | 可选：这题撞到的**契约空白**，`metrics.py` 会把它变成 `target_dir="contract/"` 的打回条目（同一条空白按原文去重）。v1.1 已把原来的 13 处空白补齐，本轮**没有题再带 `gap`**；入口保留给下一轮用 |

## 打回清单 `rework.json`

> `target_dir` 说明该退回哪一路：`ask/`（取数与追溯）、`show/`（渲染导出）、
> `dict/`（字典条目）、`contract/`（契约没写或写不下）、`eval/`（评测器自己）。

每条：`id` / `kind` / `severity` / `target_dir` / `question_id` / `category` / `contract_ref` / `evidence` / `action`。
按产物自动生成：答错 → `ask/`；两遍不一致 → `ask/`；口径未命中 / 命中旧版本 → `ask/`；
T1–T8 不过 → `ask/`（例外：零行命中导致 `row_ids=[]` 的 T3 不过，是契约判据写不下，退 `contract/`）；
反问槽位名不在 §2.10 枚举内 → `contract/`；契约空白 → `contract/`；评测器短板 → `eval/`。
每条都带 `question_id`（能定位到题的就填，聚合类缺陷留空），报告 §4 的追溯表也按「题号 / trace_id」两列排，
因为同一题里 `trace_id` 会重复，只写 `t-0001` 追不回是哪道题。
**判据冲突题不退回 `ask/`**：q032 / q035 / q040 的反问槽位 `time_column` 是「§2.10 的封闭枚举 vs §7.1 触发条件 4」
的契约打架（报告 §0.4 JC-4），准确率里照旧算错，但打回清单只在 `contract/` 留一条（点名这三个题号），
不写「修 qNNN 的取数」——同一份产物一边说「契约要改」一边说「ask/ 要改」，读者只会信一边；
等契约定下唯一口径再判 `ask/` 的对错。
本轮还按「跑出来的根因」聚合，但 caveat 分三类处理，不一律当缺陷：
**真兜底**（`PLAN_MINIMAL` / `NOT_PRUNED` / `NO_INTERPRETATION`）→ 退回 `ask/`；
**契约定义里就是正常状态**（`RESULT_EMPTY` / `PCT_BASE_ZERO` 等）→ 不打回；
`INJECT_DIGEST_UNCHECKED`（契约 §5.2 允许留空 `input_digest`）→ 记成 `eval/` 自己的待办
（要补规范化输入哈希 + `INJECT_STALE` 用例）。错误码、反问槽位同样按目录归口。
**空清单也要留这个文件**：它同时声明了字段与去向规则。

## 本步自己承认的局限

1. `expect.metrics` 是**中文名提示**：本步按边界不许读 `dict/`，所以没拿真实字典条目硬绑 id（核对时 id 或 name 命中任一即算）。真实字典若改名，这里是误判来源。
2. T2 / T3 要回源表头与行数，本步只解 CSV；xlsx 源记「不可验证」并单独计数，**不冒充已验**。所以严格率是**下界**，报告里同时给了「可验证口径」那一行。
3. 口径命中率的分母契约没定义（§3.3 只判「一个数字算不算命中」），本步定义为「带 `metric_id` 的数字单元格数」，非字典口径单列不计入。本轮它**接近自证**（`metrics_used` 与 `source_metric_id` 出自同一次字典加载），信息量在未命中/旧版本/非字典三个桶里。
4. 「SQL 执行准确率」是**机械核对 + 结构要求**，不是语义评测：抓得住「答非所问」和「语义没落地」，抓不住「结构对了但数算错」。
5. D1 是注入的（契约 §5.2）：本轮**不测**模型的问题理解能力，只测 D1 之后的链路。
6. 反问阈值从 3 放宽到 5：实跑发现契约 §7.1（`metric_ids` 为空必须反问）与 §5.1（有「时间词」就必须进 `time_range` 或 `ambiguity`）会把「纯明细列举」和「时间词落在别的列上」也判成反问，详见 `report.md` §0.2 与 `rework.json` 的 `contract/` 条目。
7. 早期探针有一两次忘了带 `PYTHONDONTWRITEBYTECODE=1`，`ask/__pycache__` 里可能留了 `.pyc`（只是字节码缓存，不是源码改动）；正式跑题全程带该环境变量，没有再往 `ask/` 写任何东西。
8. 「异常」类题（q053/q057/q060）与同比题 q025 的 `expect.rows` 放宽成 `any`：按合成数据真值复算，这几题确实没有超过阈值的行 / 没有可比基期（销量只有 2024、2025 两年，2024 同比缺 2023 数据），空结果 `RESULT_EMPTY` 就是正确答案。放宽是「承认判据不足」（阈值没写死就判不了阈值对不对），不是把答错改成答对；这几题现在只判 SQL 结构。
9. 本轮**模型链路 0 次调用**（环境没配模型）：报告里的分数全部来自「D1 注入 + D2/D3/D4 兜底」，只能说降级链路能跑通到这个程度，不能说模型也一样。
