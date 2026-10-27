# tally 切片清单

> 本文件是**施工顺序 + 每步验收断言**，不是设计文档。字段级约定全在 `contract/answer.md`，这里不重复。
> 步数 **7 步**。每步一组机械断言，每个文件**恰好一个写者**。
> 本文件不含任何实测数字；出现的 `5000` / `200` / `20` / `60` 都是**配置常量或题量设计**，不是测量结果。
>
> **第 2 步是唯一的「多路并行里程碑」**：它的四条路各自独占一个目录、互不 import，所以可以同批并发。
> 其余各步都是单路，`tg gate` 红了只看那个目录。这样切的理由：并发只有在「文件不打架」时才是并发，
> 否则就是两个人抢同一支笔。

---

## 1. 步骤表

| 步号 | 标题 | 归属路 | 产物路径 | 依赖步 |
|---|---|---|---|---|
| 1 | 契约：`answer.json` + 指标字典 + 追溯链 + 决策模型注入协议 | `contract/` | `contract/answer.md`、`contract/slices.md` | — |
| 2 | **四路骨架里程碑**：调研词表 ／ 数据接入 ／ 指标字典 ／ 渲染器（互不重叠，可同批并发） | `research/` `ask/` `dict/` `show/` | 见 §1.1 | 1 |
| 3 | 决策四段 D1–D4 + `--decide inject:` 注入 + 缓存键 + 中文别名表 | `ask/` | `ask/decide.py`、`ask/inject/d1.json`…`d4.json`、`ask/aliases.json` | 2 |
| 4 | 生成 SQL + 只读静态检查 + 执行 + 追溯链 → 产出 `answer.json` 三种体 | `ask/` | `ask/query.py`、`ask/static_check.py`、`ask/trace.py`、`ask/answer_out/smoke.json` | 3 |
| 5 | 真答案渲染（纯 SVG 柱/折/散+结果表+追溯面板）+ 本地只读工作台 | `show/` | `show/render.py`、`show/svg.py`、`show/server.py`、`show/out/index.html` | 4 |
| 6 | 字典管理（提案制）+ 导出 `answer.json`/`report.md`/SVG | `show/` | `show/export.py`、`show/out/dict_proposal.json`、`show/out/report.md` | 5 |
| 7 | 60 题问题集 + 四项指标测量 + 同问两遍字节比对 + 打回清单 | `eval/` | `eval/questions.jsonl`、`eval/manifest.json`、`eval/metrics.py`、`eval/report.md`、`eval/rework.json` | 6 |

### 1.1 第 2 步：四条路各写各的（这是唯一一批真并发）

| 路（角色目录） | 这一步只产出 | 机械断言 |
|---|---|---|
| `research/` | `research/notes.md`（xlsx 内部结构要点、中文列名匹配优先级、单位换算口径）、`research/aliases.seed.json`（中文列名同义词种子表）、`research/units.seed.json`（单位词表） | `file:exists:research/notes.md` ／ `grep:"sharedStrings"@research/notes.md` ／ `file:exists:research/aliases.seed.json` ／ `file:exists:research/units.seed.json` |
| `ask/` | `ask/ingest.py`、`ask/schema.py`（解析 + schema 推断）＋ 自造 `ask/fixtures/smoke.xlsx`（zip+XML 手写，不许引第三方库；**必须落盘**） | `file:exists:ask/ingest.py` ／ `file:exists:ask/fixtures/smoke.xlsx` ／ `cmd:python -m ask.ingest --selftest ask/fixtures/smoke.xlsx` ／ `cmd:python -m ask.schema --dump ask/fixtures/smoke.xlsx > nul` ／ `grep:"header_row"@ask/schema.py` |
| `dict/` | `dict/metrics.json`、`dict/validate.py`（V1–V12，写错必报错） | `file:exists:dict/metrics.json` ／ `file:exists:dict/validate.py` ／ `cmd:python dict/validate.py --check dict/metrics.json` ／ `grep:"METRIC_INVALID"@dict/validate.py` |
| `show/` | `show/svg.py`、`show/render.py`、`show/fixtures/answer.sample.json`（**自造示意数据，必须显式标注「数字是编的」**）、`show/out/index.html` | `file:exists:show/render.py` ／ `file:exists:show/svg.py` ／ `grep:"数字是编的"@show/fixtures/answer.sample.json` ／ `cmd:python -m show.render --answer show/fixtures/answer.sample.json --out show/out/index.html` ／ `grep:<svg@show/out/index.html` ／ `nogrep:SELECT@show/render.py` |

三条不许破的：

1. **四路互不 import。** `show/` 在第 2 步只吃自己的 fixture，不 import `ask/`；`ask/` 不 import `dict/`（要校验就 `python dict/validate.py --check ...` 当子进程调）。两边一旦互相 import，就不是四路，是一路。
2. **`show/` 的 fixture 必须自己声明是编的。** 第 2 步的图是**示意**，`grep:"数字是编的"` 就是钉这一条。第 5 步起必须换成真 `ask/answer_out/smoke.json`，那时断言里的 `--answer` 参数会跟着换。
3. **`ask/` 的自检 fixture 落在 `ask/fixtures/smoke.xlsx`，不许落到 `eval/data/`。** 这一条是踩过的坑：
   §2.1 最初把 `eval/data/**` 的写入权给了 `ask/`（理由是「合成数据都是它生成的」），但并发派发时
   **一个角色只碰一个目录**，`ask/` 根本写不到 `eval/data/`，于是它回退成内存里造一份 xlsx ——
   `cmd:--selftest` 照样 exit 0，断言绿了，可盘上什么都没有。`eval/data/` 的真数据归第 7 步的 `eval/` 路。

### 1.2 关于 `dict/` 那条反向断言的说明（**必须保留这句话，别偷偷改成能过的**）

「坏字典必须报错」是一个**反向**要求：断言通过＝程序非 0 退出。`tg` 的六种前缀里没有「必须失败」这一种，所以第 2 步只拆成两条：

1. `cmd:python dict/validate.py --check dict/metrics.json` —— 正例，必须 exit 0；
2. `grep:"METRIC_INVALID"@dict/validate.py` —— 只证明错误码存在，**不证明它真的会抛**。

第 2 条是弱断言，这是已知妥协。真正验「坏字典报错」放在第 7 步的 `eval.metrics` 里（它会跑 `eval/bad_dict/*.json` 并统计「该报错的是否报错」），因为那一步允许写脚本、允许反向断言。**不要**为了「让断言好看」而删掉这条说明。

---

## 2. 边界

### 2.1 文件写入权（**每个文件恰好一个写者**）

| 目录 | 写者 | 其余各路的权限 |
|---|---|---|
| `contract/**` | 无（第 1 步写完即冻结） | 只读 |
| `data/**` | 无（用户投放的原始数据） | 只读 |
| `research/**` | `research/` | 只读 |
| `ask/**` | `ask/` | 只读（`show/` 只许看形状，不许 import） |
| `dict/**` | `dict/` | 只读 |
| `ask/answer_out/**` | `ask/` | 只读 |
| `eval/data/**` | `eval/`（合成数据；第 7 步才建） | 只读（`ask/` 的自检 fixture 在 `ask/fixtures/`，别来这里写） |
| `eval/**`（其余） | `eval/` | 只读 |
| `show/**` | `show/` | 只读 |

**声明：任何两条路都不碰同一批文件。** 上表任何一格出现「两个写者」，就是边界破了，改设计而不是加锁。
`dict/` 从 `ask/` 里独立出来的原因就是这个：字典是口径，`ask/` 是算法，两者改起来节奏不同，塞在一个目录里
就会出现「两个人改同一棵树」。

### 2.2 交接物只有 `answer.json`

- 唯一交接物：`ask/answer_out/<answer_id>.json`，形状 100% 由 `contract/answer.md` 第 2 节定义。
  它为什么在 `ask/` 里面而不是仓库根目录的 `answer_out/`：**一个角色只碰一个目录**，
  而这份文件是 `ask/` 这一路算出来的。跨目录写会让「并发时互不打架」这条失效 ——
  这是第 2 步踩过的同一个坑（`eval/data/**` 那次）。
- `show/` 只吃这一个文件。它**不 import `ask/` 的业务模块**，不读源 Excel/CSV，不连数据库，不重算任何数字。
- `show/` 里出现 `SELECT` / `SUM(` / `GROUP BY` 这类字符串，就算越界 —— 用 `nogrep:SELECT@show/render.py` 机械检查（第 2、5 步各有一条）。
- `show/` 想看图/看表，只能从 `rows` 取数、从 `trace` 取解释。所以「图里的数」和「表里的数」不可能不一致。
- 第 2 步的唯一例外已在 §1.1 写明：`show/` 吃自造 fixture，且 fixture 自己声明「数字是编的」。

### 2.3 唯一的跨界通道：字典编辑

字典的写入权在 `dict/`（口径属于答案侧）。工作台要做「字典管理」，所以走**提案制**：

```
show/ 的字典编辑页
  → 写 show/out/dict_proposal.json   （只是「我想改成这样」，不落盘到 dict/）
  → 用户在工作台点「应用」
  → 调用 dict/ 的 CLI：python dict/validate.py --apply show/out/dict_proposal.json
  → 校验器跑 V1–V12，过了才写 dict/metrics.json，不过就原样报错回显
```

`show/` 自己**永远不写** `dict/metrics.json`。这样「口径写错要报错」这条不会因为图省事而被绕开。

---

## 3. 第一个可见产物在哪一步之后

| 里程碑 | 在哪一步之后 | 人能看见什么 |
|---|---|---|
| 第一个**能跑的骨架** | 第 2 步之后 | ×4：词表文件能读、`ask.schema` 能 dump 出列类型、字典校验器能过好字典、`show/out/index.html` 打开有一张**示意**图（图上写着数字是编的） |
| 第一个**机器可读**的真答案 | 第 4 步之后 | `ask/answer_out/smoke.json` 已经是一个完整 `answer.json`：用编辑器/`jq` 能看到 `sql`、`rows`、`trace`。数字已有追溯链，但**要人肉对着 JSON 读** |
| 第一个**人眼可读**的真产物 | **第 5 步之后** | `show/out/index.html` 双击就能打开：结果表 + 柱/折/散图 + 追溯面板（点一个数字展开它来自哪个文件、哪个 sheet、哪几列、哪几行、什么表达式）。**这一步是整个项目第一次「看得见真数」** |
| 第一个**能判分**的产物 | 第 7 步之后 | `eval/report.md`：五类主指标 + JOIN 单列 + 同问两遍字节比对 + `eval/rework.json`（哪类错、打回哪个目录） |

所以：**最早能在第 4 步之后验收「答案对不对」，最早能在第 5 步之后验收「这件事值不值得做」。**
第 5 步的 `ack:` 断言就是这一条的人眼确认点。

---

## 4. 60 题问题集的组织方式

### 4.1 分布

| 类别码 | 类别 | 题数 | 为什么是它 |
|---|---|---|---|
| `FILTER` | 筛选 | 10 | 单表 + 条件，最基础，用来抓「列名匹配错」 |
| `AGG` | 聚合 | 10 | 分组 + 字典口径，用来抓「口径没走字典」 |
| `YOY` | 同比环比 | 10 | 时间窗口，用来抓「时间范围猜错」 |
| `JOIN` | 多表连接 | 10 | **单独统计，见 4.4** |
| `RANK` | 排名 | 10 | 排序 + top-k，用来抓「排序不全序」 |
| `ANOM` | 异常 | 10 | 需要先算分布再判异常，用来抓「D2 计划退化成最小计划」 |
| | **合计** | **60** | |

### 4.2 放哪

- 题库：**单个** `eval/questions.jsonl`，**60 行，每行一题**，按 `category` 字节序再按 `qid` 排（排序是为了让文件哈希稳定，也为了 `diff` 时只看得到真改动）。
- `qid` 规则：`Q-<类别码>-<两位序号>`，例如 `Q-JOIN-03`。**`qid` 永不改**（改题就换新 qid，旧题标 `deprecated:true`）。
- 每行形状：

```json
{"qid":"Q-AGG-04","category":"AGG","question":"2025 年各区域的成交额是多少",
 "datasets":["eval/data/sales.xlsx"],
 "metric_ids":["gmv"],
 "expect":{"kind":"cells","columns":["region","gmv"],"digits":[0,2],"ref_sql_index":0},
 "deprecated":false,"note":"示意数据，数字是编的"}
```

- 数据集：`eval/data/` 下的**自建合成数据**（不联网、不抓外部数据）。每个文件的 sha256 写进 `eval/manifest.json`；数据文件改动必须同时改 manifest，否则 `eval.metrics` 拒绝跑（防止「改了数据忘了改真值」）。
- 参考 SQL：`eval/ref/q<NN>.sql`，一题一条（`ref_sql_index` 指它）。

### 4.3 真值怎么标

`expect.kind` 四选一，**全部由脚本产出，手写数字一律不接受**：

| kind | 用法 | 判定方式 |
|---|---|---|
| `cells` | 结果集小（≤20 行） | 逐格比对：`columns` 名一致 + 每个单元格按 `digits` 对齐后**严格相等** |
| `scalar` | 结果就一个数 | 与参考值按 `digits` 对齐后严格相等（**不放容差**；有容差就等于允许口径漂移） |
| `invariant` | 排名/异常题（结果集大或答案不唯一） | 只比不变量：`top_k_ids` 的顺序、同比值的**符号 + 两位小数**、异常行的 `__rid` 集合 |
| `sql_equivalent` | 结构复杂、结果大 | 用 `eval/ref/q<NN>.sql` 在同一份冻结数据快照上跑一遍，**跑出来的结果就是真值**；比的是「我们的结果 == 参考 SQL 的结果」 |

- 校验流程：`eval/check_questions` 会强制检查「每条题要么有 `expect.kind` 且配套字段齐全，要么 `deprecated:true`」。缺真值的题**不许混进 60 行**。
- 判断题本身必须能过：`expect.kind="invariant"` 的题必须在 `note` 里写清「不比什么」（例：「不比排序里第 4 名以后」）。否则事后无法解释一次失败。
- 题目文本里的年份/季度等时间词必须与 `eval/manifest.json` 里声明的数据时间跨度兼容 —— 兼容性由脚本检查，不靠人看。

### 4.4 JOIN 题为何单独统计

三条理由，都不是审美：

1. **README 已认定它是最容易翻车的两处之一**（多表 JOIN、中文列名模糊匹配）。既然已知薄弱，就不该把它稀释进总分里当「平均水平的证据」。
2. **失败模式与其它 5 类不可比。** 其它类的错通常是「数不对」（口径错、时间窗错、排序错），人一眼能看出来；JOIN 的错是「行数悄悄放大/悄悄漏掉」，结果**非空、量级合理、看上去对**。把两种性质不同的错误按题数加权平均，得到的那个百分数读不出任何决策。
3. **一个 JOIN 题会同时考两件事**（连接键对不对 + 行数有没有被放大），一件题占两个失分点，题数加权天然不公平。

所以：`eval/report.md` 里 JOIN 单独一行、单独给准确率，**不进总分分母**。总分只覆盖其余 5 类共 50 题。
报告里必须同时给出「含 JOIN 的 60 题数字」和「不含 JOIN 的 50 题数字」两个，并且**明写哪个是主指标**
—— 答：不含 JOIN 的 50 题是主指标，JOIN 那 10 题是单列的已知短板证据。

> **v1.1 附注**：契约 v1.1 起 JOIN 已经**能表达**了（D1 不再把「要多表」当歧义，`joins[]` 由 D3 落地）。但上面这三条理由（失败模式不可比、一题两个失分点、漏行/放大看不出）**与版本无关**，所以 JOIN 仍然单列统计、仍不进总分分母。变的是「JOIN 能不能做对」，没变的是「JOIN 的分怎么记」。

---

## 5. 每步断言的写法

### 5.1 只许这 6 种前缀

| 前缀 | 形态 | 回答什么问题 |
|---|---|---|
| `file:exists:` | `file:exists:<相对路径>` | 产物落地了吗 |
| `file:absent:` | `file:absent:<相对路径>` | 该删/该没有的东西还在吗 |
| `grep:PAT@FILE` | 正则 `PAT` 在 `FILE` 里至少命中一次 | 关键事实写进去了吗 |
| `nogrep:PAT@FILE` | 正则 `PAT` 在 `FILE` 里一次都不命中 | 越界的东西混进来了吗 |
| `cmd:` | `cmd:<命令>`，exit 0 即通过 | 脚本能跑通吗 |
| `ack:` | `ack:<一句人话>` | 只有人眼能判的事（`tg ack` 确认） |

### 5.2 三条铁律

1. **断言只回答「这一步做完了吗」，不回答「整个项目对不对」。** 所以禁止写全仓检查。
2. **反例（写了就算违规）**：
   - `cmd:python -m pytest` —— 全仓测试，第 4 步会因为第 6 步没写完而红
   - `grep:SELECT@ask/` —— 目录级 grep，`tg` 只接受文件路径，而且它验的是「全仓风格」
   - `cmd:python -m eval.metrics --all` —— 出现在第 4 步，把第 7 步的工作提前扛上来
   - `grep:.*@ask/query.py` —— 恒真断言，等于没写
3. **每条断言必须能独立解释失败原因。** 写不出来「它红了我要去看什么」的断言，删掉。
4. `cmd:` 断言必须**幂等、可重复跑**（不产生必须手工清理的中间状态）。跑一次留一堆临时文件，第二次跑就红的断言不算断言。
5. 断言的退出码由脚本决定，不由任何人自述决定 —— 这是 README 铁律 2（同问同答）在施工侧的对应物：**「完成」是断言说的，不是作者说的。**

### 5.3 各步断言的覆盖意图（为什么是这几条，不是别的）

| 步号 | 覆盖意图 |
|---|---|
| 1 | 两个文件存在 + 关键字段名/目录名出现（**已通过**） |
| 2 | ×4 路各自「产物存在 + 能跑」：解析器能 dump 出 schema（`header_row` 是 2.6 里显式要求可 `null` 的字段，所以 grep 它）；字典好例能过；渲染器能出含 `<svg` 的页且 `show/` 里没有 SQL 字样；调研词表三件齐备且 notes 里写明 xlsx 内部键名（`sharedStrings`） |
| 3 | 四个注入样例齐备 + 输出 schema 自检通过 + 注入标记字段真的存在 + 别名表落地 |
| 4 | 静态检查自检（正/反例都在 `--selftest` 里）+ 真跑一次落出 `answer.json` |
| 5 | 真答案喂进渲染器（`--answer ask/answer_out/smoke.json`）+ 产物含 `<svg` + 只绑本地 + `show/` 里没有 SQL 字样 + 人眼确认图表可读（`ack:`） |
| 6 | 提案文件落地（**不碰 `dict/`**）+ 导出三件套落地 + `show/` 里没有 SQL 字样 |
| 7 | 真的 60 题 / 6 类 / 每类 10 题 + 两遍字节比对 + 报告里 JOIN 单列 + 打回清单带 `target_dir` |

---

## 6. 并发点（唯一一批真并发在第 2 步）

- **第 2 步四路可同批并发**（`research/` ∥ `ask/` ∥ `dict/` ∥ `show/`）：四条路的产物目录互不重叠，
  依赖只有「第 1 步的契约」，彼此不 import。
  对应命令是 `orch dispatch 2 --roles research,ask,dict,show --max-fanout 4 --concurrency 2`
  —— 4 路分两批，`--max-fanout` 是「总共付几路的钱」，`--concurrency` 是「同时跑几路」。
- **其余各步都是单路**：第 3、4 步在 `ask/`，第 5、6 步在 `show/`，第 7 步在 `eval/`。
  同一路内不许并行（一个目录一个写者），所以这几步并发数是 1，别硬凑。
- 硬规矩：**任何一步的断言如果跨了两个目录，就说明这步该拆**；反过来，**不能把两条路的产物塞进同一个目录来「并行」**。

---

## 7. 评测反馈回路（第 7 步产出，决定下一轮派谁）

`eval/rework.json` 的形状（这是**打回清单**，不是报告）：

```json
{"generated_from":"eval/report.md","items":[
  {"category":"JOIN","fails":3,"target_dir":"ask/","symptom":"行数被放大：连接键选了非唯一列",
   "evidence":["Q-JOIN-02","Q-JOIN-07","Q-JOIN-09"]}
]}
```

- `target_dir` 只许写 §2.1 里真实存在的写者目录，**不许写 `show/` 去修 `ask/` 的错**（那等于让前端改口径）。
- 派回的方式：`orch dispatch 2 --roles <那个目录的角色>` 重派那一路；不是重跑整步。
- `fails` 是**这一轮实测的失败题数**，不是估计值。算不出来就写 `null` 并在 `symptom` 里说明为什么算不出来 —— 不许填个好看的数。
