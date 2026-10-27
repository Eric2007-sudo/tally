# 调研词表 · xlsx 内部结构 / 中文列名匹配优先级 / 单位换算口径

> 状态：`research` 路产物（步骤 #2）。**唯一事实来源是 `contract/answer.md`**；本文所有「建议」「待定」都不是契约，
> 与 `answer.md` 冲突时以契约为准。契约没写的地方我**没有替它做主**，全部收在 §6 的缺口清单里，等上层定。
> 本文不提供任何实测统计（本项目一条 60 题都没跑过）。

证据标记：

- **[实测]** = 本机当场跑过（脚本见 §7，Python 3.14 / openpyxl 3.1.5 / zipfile）。
- **[实测·构造]** = 手工造的合规 OPC 包，模拟 Excel 的写法；**未用真 Excel 文件验证**，只用于演示解析规则。
- **[规范]** = 引自 ECMA-376 / ISO/IEC 29500 / Microsoft 文档（链接见 §8）。
- **[建议]** = 我的工程判断，可被推翻，不是契约。

---

## 1. xlsx 内部结构词表

### 1.1 包内必备部件（[规范] ISO/IEC 29500-2 OPC + §18.2）

| 部件路径 | 作用 | 关键属性 / 元素 | 陷阱 |
|---|---|---|---|
| `[Content_Types].xml` | 必需；声明每个 part 的 MIME | `<Override PartName="/xl/workbook.xml" ContentType="…sheet.main+xml"/>` | 别用它去猜「哪个是 sheet」（[实测] 只有 Override 列表，无顺序语义） |
| `_rels/.rels` | 包级关系，指向 officeDocument | `Type=…/officeDocument` | 入口可能不叫 `xl/workbook.xml`（Strict/三方工具会改名） |
| `xl/workbook.xml` | 工作表清单 | `<sheets><sheet name sheetId r:id state/>`；`<workbookPr date1904/>`；`definedNames` | `state` 可为 `visible` / `hidden` / `veryHidden`（[实测] 隐藏页写作 `state="hidden"`）；**`date1904` 必须在这里读，见 1.4** |
| `xl/_rels/workbook.xml.rels` | `r:id → 物理文件` | `<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>` | ⚠️ **`sheetN.xml` 的编号与 sheet 顺序、`sheetId` 都不保证一致**（删/重排 sheet 后 Excel 常保留旧文件名）。只能走 rels，不许猜文件名（[实测] rId1/rId2 与 sheet1/sheet2 恰好一致，但这是巧合，不能依赖） |
| `xl/sharedStrings.xml` | **可选**的共享字符串表 | `<sst count uniqueCount><si>` | ⚠️ **不是每个 xlsx 都有**：[实测] openpyxl 3.1.5 存出的文件整包**没有** `sharedStrings` 部件（它写 `t="inlineStr"`）。`count` / `uniqueCount` 可缺失、可不准，别拿它当校验依据；同包只许有一个（[规范]） |
| `xl/styles.xml` | 数字格式与样式 | `<numFmts><numFmt numFmtId formatCode>`；`<cellXfs><xf numFmtId …>` | 单元格 `c@s` 是 `cellXfs` 的**下标**，不是 numFmtId；`numFmtId ≥ 164` 是自定义格式 |
| `xl/worksheets/sheetN.xml` | 单元格数据 | `<dimension ref>`、`<sheetData><row r><c r t s><v/><f/><is/></c>`、`<mergeCells>`、`<sheetView>` | ⚠️ `dimension` 可缺失、可过时，**不能当数据边界** |

字号/字体/主题等（`xl/theme/`、`docProps/`）与取值无关，忽略即可。

### 1.2 单元格类型 `c@t`（ST_CellType，[规范] §18.18.11）

| `t` | 含义 | 取值位置 |
|---|---|---|
| 缺省 / `n` | 数字 | `<v>`，按文本解析成数 |
| `s` | `sharedStrings` 的**下标** | `<v>` 是整数索引，不是值本身 |
| `inlineStr` | 行内字符串 | `<is><t>`（**不在** `<v>` 里） |
| `str` | 公式的字符串结果 | `<v>`（公式在 `<f>`） |
| `b` | 布尔 | `<v>0/1</v>` |
| `e` | 错误字面量 | `<v>#DIV/0!</v>` / `#N/A` / `#REF!` / `#VALUE!` / `#NUM!` |
| `d` | ISO 8601 日期字符串 | `<v>1976-11-22T08:30Z</v>`。ISO 29500:2016 才加进 ST_CellType，**Transitional 变体禁止、Excel 自身从不写、三方支持差**（SheetJS 明确说日常导出不用它）→ [建议] 遇到按「不支持的类型」处理，别猜（可对到 `SCHEMA_UNREADABLE`） |

[实测] 这些类型都在同一份包的不同单元格里出现过（`s` / `inlineStr` / `str` / `b` / `e` / 缺省数字）。

### 1.3 七个会咬人的解析陷阱（全部 [实测] 或 [实测·构造]）

1. **稀疏**：`<row>` 会整行缺失、`<c>` 会整列缺失。[实测] `row r="4"` 整行不存在，`row r="3"` 只有 A/C 两个单元格。
   → 列位置**只能**从 `c@r` 的字母部分解（`A`→1、`XFD`→16384，[实测] `get_column_letter(16384)="XFD"`），**不许**按出现次序数。
2. **富文本 run**：`<si><r><t>金</t></r><r><t>额</t></r></si>` —— 一个字符串被切成多段。
   → 必须**拼接**所有 run 的 `<t>`，否则列名变成「金」。
3. **注音 `rPh`**：[实测·构造] `<si><t>成交额</t><rPh sb="0" eb="2"><t>せいだい</t></rPh></si>`，
   天真地 `for node in si.iter()` 收所有 `<t>` 会得到 `成交额せいだい`（列名被污染 → 后面必然 `COLUMN_NOT_FOUND` 或别名匹配失败）。
   → 规则：只取 `si/t` 与 `si/r/t`，**跳过 `rPh` 子树**。
4. **`xml:space="preserve"`**：首尾空格是内容的一部分（[实测·构造] `" 金额(万元) "`）。
   → 匹配时可以归一化，但写进 `source.columns` 时必须**原样**（契约 §4.3 T2 要求「大小写敏感地出现在表头行里」）。
5. **公式可能没有缓存值**：`<c r="B3"><f>B2*2</f></c>` 或 `<c><f>…</f><v></v></c>`。[实测] 两种形态都出现了。
   → 语义是**未知**，不是 `0`（契约 §2.13 总原则：`null` 表示「没有」，`0` 表示「算出来是零」）。
6. **`t="e"` 是文本**：`#DIV/0!` 等读出来是字符串，别当数值参与聚合，也别悄悄丢行。
7. **日期=序列号+样式**：[实测] `45688` + `numFmtId=164`(`yyyy-mm-dd`) → `2025-01-31`；同一个 `45688` 去掉样式就是金额。
   → 判日期必须看**样式链**（`c@s → cellXfs[s]@numFmtId → numFmts/builtin`），不能只看数值范围。

样式判定用的内置 numFmtId（[规范] MS NumberingFormat 表）：日期 `14–22`、`45–47`；中文（zh-cn）日期/时间 `27–36`、`50–58`
（`yyyy"年"m"月"d"日"`、`h"时"mm"分"ss"秒"` 这类带中文单位的格式就在这里，正则别只认 `-/:`）。
百分比 `9`(`0%`) / `10`(`0.00%`) —— [实测] `0.1234` + numFmtId `10` 显示为 `12.34%`，见 §3.3。

### 1.4 日期系统（[规范] + [实测]）

- **1900 系统（默认）**：序列 `1` = `1900-01-01`。Excel **把 1900 当成闰年**，序列 `60` 是根本不存在的 `1900-02-29`（Lotus 1-2-3 兼容性遗留）。
- 等价算法（我用它复算了 [实测] 的值，对得上）：
  - `serial ∈ [1,59]`：`date = 1899-12-31 + serial 天`
  - `serial ≥ 61`：`date = 1899-12-30 + serial 天`
  - `serial = 60`：**不存在**。
- [实测] `1→1900-01-01`、`59→1900-02-28`、`61→1900-03-01`、`45688→2025-01-31`。
- [实测·坑] 序列 `60` 各家实现不一致：openpyxl `from_excel(60)` 返回 `1900-02-28`（往边界钳），别的库可能返回 `1900-03-01` 或直接抛错。
  → 同一份文件换库就差一天。必须**定死**一种处理（[建议] 判 `SCHEMA_UNREADABLE`，或按契约 §2.9 记 caveat，不许悄悄取邻值）。
- **1904 系统**：`<workbookPr date1904="1"/>` 时 epoch = `1904-01-01`，序列 `0` 合法（Mac 老文件的默认）。
  [实测] 同一个 `0`：1900 系统下是「时间 00:00:00」，1904 系统下是 `1904-01-01`。
  → **解析日期前必须先读 `workbookPr@date1904`**，漏读会让整表日期偏 4 年零 1 天。
- **时间-only**：`0 ≤ v < 1` 表示当日时刻（[实测] `from_excel(0)→00:00:00`）；负序列（Mac 老文件）不是日期。

### 1.5 规模与精度（[规范] MS「Excel specifications and limits」）

| 项 | 上限 | 对我们的意义 |
|---|---|---|
| 行 × 列 | 1,048,576 × 16,384（末列 `XFD`） | 列数上限就是 16,384；超了说明不是正常表格 |
| 数字精度 | **15 位有效数字** | 身份证/订单号超过 15 位会被 Excel 抹末位 → **不能靠「值相等」反查源行**，只能靠行号。这正好印证契约 §2.5 用「文件#sheet!源行号」做 `__rid` 的设计 |
| 单元格字符数 | 32,767 | 长文本备注列别整列进 prompt |
| 单个工作簿 sheet 数 | 仅受内存限制 | 隐藏页可能很多，遍历要显式过滤 `state` |

### 1.6 表头行相关的 xlsx 事实（喂给 `source.header_row`，形状以契约 §2.6 / §2.9 / §2.13 为准）

- 表头行号是 **1 基的文件行号**，且**不一定是第 1 行**：中文报表常见「标题行 → 说明行 → 单位行 → 空行 → 表头」。
- **合并单元格**（`<mergeCells>`）是双行表头/跨列标题的标配：合并区内只有**左上角**有值，其余为空。
  [实测] `mergeCell A5:C5` 只有 A5 有值、B5/C5 不存在 → 按单行读会得到「大量空列名」。
- 双层表头（上行「销售」大类、下行「金额/数量」指标）无法用单行表达 → [建议] 判 `SCHEMA_UNREADABLE`，或用最下一行并记 `HEADER_ROW_GUESSED`（**不许默默选一行**）。
- 空列名 / 重复列名（两列都叫「金额」）→ 必须报错，**不许自动去重**（去重后 `source.columns` 就不再与表头逐字一致，违反 T2）。
- 隐藏行（`row hidden="1"`）与隐藏列同理：默认不当数据，若被算进去必须可见（caveats）。
- [建议] 纵向合并的记录行（一行记录被拆成 2 行）在中文报表里很常见，但无法机械判断 → 不自动合并，标成风险项。

---

## 2. 中文列名匹配的优先级

### 2.1 契约拍死的三条（其余都是建议）

| 锚点 | 原文要旨 |
|---|---|
| §3.1 `aliases` | 「人工别名（**中文匹配的第一优先级**）」→ 人工别名必须压过一切算法匹配 |
| §2.9 `COLUMN_ALIAS_FUZZY` | 「中文列名靠编辑距离匹配上的，**非人工别名**」→ 允许 fuzzy，但**必须**在 caveats 留痕（封闭集，新增要升契约版本） |
| §3.2 V4 + §7.1 触发 3 | V4 只提示「最接近的列名」，**不自动替换**；候选并列 ≥2 → 反问，**禁止**「拿最像的那个算一版」 |

### 2.2 阶梯（P 越小越优先；P0–P2 有契约锚点，P3–P5 是 [建议]）

| 档 | 规则 | 命中后必须做的 | 依据 |
|---|---|---|---|
| **P0** | 逐字相同（字节相等、大小写敏感） | 无（这是唯一「干净」的命中） | 契约 §4.3 T2 |
| **P1** | 命中字典条目的 `aliases`（人工别名） | 无额外 caveat | 契约 §3.1 |
| **P2** | 归一化后相同（全角→半角、U+3000→空格、连续空白压缩、首尾空白、去 U+200B/U+FEFF） | 记 caveat —— **但封闭集里没有对应 code，见缺口 G3** | [建议] |
| **P3** | 结构等价：剥掉括号里的单位后缀 / 尾部 `%` / 末尾「(万元)」后相同（`金额(万元)` ≈ `金额`） | 同上（G3） | [建议]，单位词见 §3.2 |
| **P4** | 编辑距离相似度 ≥ 阈值 | **必须**记 `COLUMN_ALIAS_FUZZY` | 契约 §2.9 + [建议]阈值 |
| **P5** | 语义同义（销售额≈成交额） | 只许来自 P1 的人工别名；不允许算法/模型现编 | 契约 §7.1（禁止挑最像的） |

### 2.3 阈值与唯一性（[建议]，为了让「同输入同结果」成立）

[实测] 中文短串的 `difflib.SequenceMatcher.ratio()`：

| A | B | ratio | 结论 |
|---|---|---|---|
| 成交额 | 成交金额 | 0.857 | 该命中（但长度差 1 才安全） |
| 销售额 | 销售金额 | 0.857 | 该命中 |
| 交易金额 | 成交金额 | 0.75 | **灰区** → 建议升 clarify，别硬匹配 |
| 金额 | 成交额 | 0.400 | 不该命中 |
| 金额(万元) | 金额 | 0.500 | **靠阈值永远抓不到** → 必须走 P3 剥后缀 |
| 日期 | 订单日期 | 0.667 | 不该命中（维度列，语义不同） |

建议规则：`ratio ≥ 0.80` **且** `|len(A)-len(B)| ≤ 1` **且**最高档只有唯一候选；否则不匹配（宁可报 `COLUMN_NOT_FOUND` 或反问，也不猜）。
排序键用 `(档位升, 分数降, 列名 byte 序升)` —— 末位必须用字节序做 tiebreak，否则同输入两次跑可能给出不同列（违反契约 §6.1 的稳定子集要求）。

### 2.4 归一化具体做什么（[实测] 表）

| 输入 | NFC | NFKC + `" ".join(s.split())` | 说明 |
|---|---|---|---|
| `金额（万元）` | `金额（万元）` | `金额(万元)` | 全角括号只有 NFKC 折 |
| `ＡＢＣ` | `ＡＢＣ` | `ABC` | **NFC 不折全角字母** |
| `U+3000 金额 U+3000` | 保留 | `金额` | NFKC 把 U+3000 折成空格 |
| `金额` + `U+200B` | 保留 | 保留 | **零宽空格 NFKC 也不处理**，必须显式清 |

注意与契约 §5.3 的差别：契约对**提问**只允许「NFC + 全角转半角 + 空白压缩」四件事，**不许**改大小写/标点；
而裸 NFKC 比这更宽（会把 `①→1`、`㎏→kg`、`Ⅲ→III` 一起折叠，可能把两个本来不同的列名折成同一个）→ 缺口 G4。

---

## 3. 单位换算口径

### 3.1 契约已经定的（不许改）

- 单位白名单（**封闭集**，§3.1）：`CNY` / `%` / `人` / `次` / `个` / `天` / `ratio` / `none`；V6 不过 → `METRIC_INVALID`，**不猜单位**。
- `columns[].unit`：`type="decimal"` 时非空；文本/日期列必须 `null`（§2.4）。`trace`/`metrics_used` 用同一个白名单。
- `columns[].digits`：`int` 列必须 `0`；`decimal` 列 ≥ 0；文本列 `0`（§2.4）。
- 浮点纪律（§6.2）：`Decimal` 计算、`ROUND_HALF_EVEN`、定点输出**禁科学计数法**、禁 `-0.0`、`NaN/±Inf` → `RESULT_NONFINITE`；`%`/`ratio` 内部按 6 位小数算且**中间不许提前截断**。

### 3.2 中文单位词 → 白名单映射（[建议]；`units.seed.json` 是它的机读版）

| 原始 token | 规范 unit | 倍率 | 备注 |
|---|---|---|---|
| 元 / 人民币 / RMB / CNY / ¥ / 块钱 | `CNY` | 1 | |
| 千元 | `CNY` | 1 000 | |
| 万元 / 万 | `CNY` | 10 000 | 倍率落点见 G2 |
| 亿元 / 亿 | `CNY` | 100 000 000 | 同上 |
| % / 百分比 / 百分点 | `%` | — | 语义见 G1 |
| 占比 / 比率 / 比例（不带百分号） | `ratio` | 1 | 内部 6 位小数（§6.2） |
| 倍 | `ratio` | 1 | |
| 人 / 人数 / 人次 | `人` | 1 | 「人次」是否算 `次` → G5 |
| 次 / 次数 / 笔 / 单 / 条 | `次` | 1 | G5 |
| 个 / 件 / 只 | `个` | 1 | |
| 天 / 日（时长口径） | `天` | 1 | 若「日」是日期列则是 dimension，不是单位 |
| 纯计数 / 无单位 | `none` | 1 | |

**不在白名单里的一律不映射**（箱 / 包 / 平方米 / 小时 / 美元 …）→ 报 `METRIC_INVALID`，由字典加白名单并**升契约版本**，不许现场造单位。

### 3.3 三个必须写下来的坑

1. **百分比是存成小数的**：[实测] Excel 里显示 `12.34%` 的单元格，`<v>` 是 `0.1234`、`numFmtId=10`。
   → 读进来的是 `0.1234`。若 `unit="%"` 指「百分点」，输出前要 ×100；若指「比例」，那它和 `ratio` 无法区分。**契约没说 → 缺口 G1，我不定。**
   唯一确定的要求：别把 `0.1234` 当 `12.34%` 报出去，也别反过来 —— 这类错在 audit 时看不出来。
2. **倍率没有落点**：契约的 unit 是封闭枚举，没有 `scale` / `factor` 字段（缺口 G2）。
   可行落点只有一个：把倍率写进 `metric.definition`（`SUM(amount) * 10000`）—— 它是 SQL 表达式，V4/V5/V9 都不禁止算术。
   但那是**改口径**，必须 owner 认、在字典里认一次，**不许 import 侧临时乘**；`source.columns` 里仍要照抄原始列名 `金额(万元)`（否则 T2/T8 对不上）。
3. **单位写在列名里**：`金额(万元)`、`占比%`、`单价（元/件）` 这种，单位是**匹配信息**，不是列名的一部分。
   剥离正则建议（用于 §2.2 的 P3 档）：
   `^(?P<base>.+?)\s*[（(\[【]\s*(?P<unit>万元|亿元|千元|元|%|人|次|个|天)\s*[)）\]】]\s*$` 或尾巴 `[_\-\s]*(万元|亿元|千元|元|%)$`。
   **剥离只用于匹配**，`source.columns` 必须写原列名全串。

### 3.4 digits 建议（[建议]，非契约；契约只要求与 `type` 自洽）

`CNY` → 2；`%` → 2；`ratio` → 6（对齐 §6.2 内部精度）；`人/次/个/天/none` → 0。真正的 `digits` 由字典条目/列定义给定，不许 import 侧自创。

---

## 4. 与契约字段的对接点（只做指路，不下新约定）

| 契约字段 | 本节给的 xlsx 事实 |
|---|---|
| `source.sheet`（§2.6） | 取 `workbook.xml/sheet@name`，大小写与空格按**原样**；CSV 必须 `null`（§2.13），xlsx 隐藏页名同样是真名 |
| `source.header_row`（§2.6） | 1 基、指表头行；推断不出必须 `null` + `HEADER_ROW_GUESSED`（§2.9 / §2.13），**不许写 1 蒙过去**。见 §1.6 |
| `source.columns`（T2，§4.3） | 逐字、大小写敏感；富文本要拼接、`rPh` 要跳过、`xml:space` 空格算内容（§1.3） |
| `__rid` 的源行号（§2.5） | `__rid` 里的行号是**数据行 1 基**（表头不计）；xlsx 的 `<row r>` 是**文件行号**（含表头）→ 换算 `源行号 = r − header_row`。差一是最容易静默错的地方 |
| `caveats`（§2.9） | 已存在的 `HEADER_ROW_GUESSED` / `COLUMN_ALIAS_FUZZY` 可直接用；归一化命中缺 code（G3） |

---

## 5. 本次没做的事（避免越界）

- 没定义 `answer.json` 任何字段、没改契约、没写 `ask/` `dict/` `show/` 任何文件。
- 没给 `ALIAS`/`UNIT` 之外的词表（路由、状态码、错误体都不在我的范围）。
- 没跑任何统计（本项目一条题都没跑过）。

---

## 6. 契约没写、需要上层定的口子（我停在这里报告，没有自己加约定）

| # | 缺口 | 影响谁 | 我的观察（不是结论） |
|---|---|---|---|
| **G1** | `unit="%"` 的**值域**：百分点（12.34）还是比例（0.1234）？§6.2 只说「% 或 ratio 内部按 6 位小数」 | `dict/`（条目定义）、`ask/`（读入换算）、`show/`（显示） | 两者算出来差 100 倍，且都「看起来合理」。必须二选一并写进契约 |
| **G2** | 单位**倍率**（万/亿）没有落点：unit 是封闭枚举，没有 `scale` 字段 | `dict/`（definition 里乘？）、`ask/` | 可行落点是 `definition` 里的算术；但「算不算改口径」契约没表态 |
| **G3** | 归一化命中（P2）/ 结构等价命中（P3）**没有 caveat code**；封闭集新增要升契约版本 | `ask/`（要留痕） | 只有 `COLUMN_ALIAS_FUZZY` 覆盖「编辑距离」，覆盖不到 P2/P3 |
| **G4** | 列名归一化允许多宽：契约只规定了**提问**的四件事（§5.3），没规定列名；裸 NFKC 会把 `①→1`、`㎏→kg` 折叠，可能误合并两个不同列名 | `ask/` | 建议白名单式显式字符映射，而不是裸 NFKC |
| **G5** | 「笔/单/条/人次」是否并入 `次`；「箱/包/m²/小时」要不要进白名单 | `dict/`（白名单） | 我只能给映射建议，不能扩白名单 |
| **G6** | fuzzy 匹配的**阈值与唯一性**契约没给（只要求记 `COLUMN_ALIAS_FUZZY`） | `ask/` | 我给 0.80 + 长度差≤1 + 唯一命中是 [建议]，不是契约 |

---

## 7. 复现（证据脚本，一次性；不在 `research/` 留文件）

要求：`python -V` ≥ 3.9、`openpyxl` 可用（本机 3.1.5 / Python 3.14.3）。

**7.1 造一份带各种坑的 xlsx 并看原始 XML**

```python
import zipfile
from openpyxl import Workbook
from datetime import date
wb = Workbook(); ws = wb.active; ws.title = "销售明细"
ws["A1"] = "区域"; ws["B1"] = "金额(万元)"; ws["C1"] = "月份"; ws["D1"] = "占比"
ws["A2"] = "华东"; ws["B2"] = 1234.5
ws["C2"] = date(2025, 1, 31); ws["C2"].number_format = "yyyy-mm-dd"
ws["D2"] = 0.1234;           ws["D2"].number_format = "0.00%"
ws.merge_cells("A5:C5"); ws["A5"] = "合并表头"
ws2 = wb.create_sheet("隐藏页"); ws2["A1"] = "x"; ws2.sheet_state = "hidden"
ws["E2"] = "=B2*2"
wb.save("p.xlsx")
z = zipfile.ZipFile("p.xlsx"); print(z.namelist())
# 观察点：没有 xl/sharedStrings.xml；单元格是 t="inlineStr"；C2 的 <v></v> 是空；隐藏页 state="hidden"
```

**7.2 手工构造 sharedStrings（含富文本 + `rPh` 注音）并验证「怎么取文本」**

```python
# si 内取文本的正确写法：只认 si/t 与 si/r/t，跳过 rPh
def text_of(si):
    out = []
    for child in si:
        tag = child.tag.split("}")[1]
        if tag == "t":
            out.append(child.text or "")
        elif tag == "r":
            out += [t.text or "" for t in child if t.tag.split("}")[1] == "t"]
    return "".join(out)
# 反例（会污染列名）：对 si 做 iter() 收所有 t
# <si><t>成交额</t><rPh sb="0" eb="2"><t>せいだい</t></rPh></si>
#   正确 -> '成交额'      反例 -> '成交额せいだい'
```

**7.3 日期序列号**

```python
from datetime import date
base_1900 = date(1899, 12, 30)   # 仅对 serial >= 61 成立；60 是不存在的 1900-02-29
assert date.fromordinal(base_1900.toordinal() + 45688) == date(2025, 1, 31)
# 实测 1->1900-01-01, 59->1900-02-28, 61->1900-03-01；openpyxl from_excel(60) -> 1900-02-28（往边界钳）
```

**7.4 归一化**

```python
import unicodedata
norm = lambda s: " ".join(unicodedata.normalize("NFKC", s).split())
# '金额（万元）' -> '金额(万元)'；'ＡＢＣ' -> 'ABC'；'\u3000金额\u3000' -> '金额'
# 但 '金额\u200b'（零宽空格）原样保留 —— 必须显式清 U+200B / U+FEFF
```

**7.5 列名相似度**

```python
from difflib import SequenceMatcher
SequenceMatcher(None, "成交额", "成交金额").ratio()   # 0.857
SequenceMatcher(None, "金额",   "成交额").ratio()     # 0.400
SequenceMatcher(None, "金额(万元)", "金额").ratio()   # 0.500  ← 靠阈值抓不到，须剥单位后缀
```

---

## 8. 引用清单

| 事实 | 来源 |
|---|---|
| 包结构、worker/sheet/rels、sharedStrings 必需性与唯一性、`v` 的含义、`t="d"` 示例 | MS Learn — *Structure of a SpreadsheetML document*；*Working with the shared string table*；*CellValue（§18.3.1.96）* |
| `ST_CellType` 枚举（`b`/`d`/`e`/`inlineStr`/`n`/`s`/`str`） | ISO/IEC 29500-1 §18.18.11；ECMA-376 |
| 内置 `numFmtId` 表（9/10 百分比、14–22 日期、zh-cn 27–36 / 50–58） | MS Learn — *NumberingFormat Class*（ISO/IEC 29500-1 摘录） |
| 1900 被当成闰年 | MS Learn — *Excel incorrectly assumes that the year 1900 is a leap year* |
| 15 位有效数字、1,048,576 × 16,384、单元格 32,767 字符、最早日期 1900-01-01（1904 系统为 1904-01-01） | MS Support — *Excel specifications and limits*；*Floating-point arithmetic may give inaccurate result in Excel* |
| `t="d"` 默认不写、三方支持差、Transitional 不允许 | SheetJS docs — *Dates* |
