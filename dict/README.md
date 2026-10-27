# `dict/` · 指标字典 + V1–V12 校验器

契约来源：`contract/answer.md` §3（字典格式）、§3.2（V1–V12）、§8.1（静态检查闸）、§2.11（错误码枚举）。
契约版本：**v1.1**（`validate.py` 的 `CONTRACT_VERSION = "1.1"`，三份 JSON 的顶层 `contract_version` 同为 `"1.1"`）。
v1.1 相对 v1.0 在本目录**只动了版本串与快照文档壳**：§3.1 字段、V1–V12 的判据、错误码枚举一条没改（见下面判断 4）。
本目录只做这一件事：**定义字典条目格式并机械校验**。不做 SQL 执行、不做选表、不做渲染。

## 文件

| 文件 | 作用 |
|---|---|
| `metrics.json` | 指标字典，顶层 `{"contract_version":"1.1","metrics":[...]}`，`metrics` 按 `id` 字节序排列 |
| `validate.py` | V1–V12 校验器；`--check` 一个字典文件，退出码 0/1 |
| `schema.snapshot.json` | schema 快照，顶层 `{"contract_version":"1.1","schema":[...]}`（`schema` 的值就是契约 §5.1 那个形状），V4/V5 的唯一依据 |
| `history.snapshot.json` | V8 的历史快照 `{contract_version, history: {id: {version: definition_sha256}}}`，由 `--write-history` 生成 |
| `fixtures/metrics.bad.json` | 故意写坏的字典，用来演示 V12「一次列出全部错误」 |

三份侧挂/主文件都带 `contract_version`，与 `validate.py` 的 `CONTRACT_VERSION`（当前 `"1.1"`）逐字相等才加载。

## 怎么跑

```bash
python dict/validate.py --check dict/metrics.json        # 0 = 可加载
python dict/validate.py --selftest                       # 32 条正反例
python dict/validate.py --check dict/fixtures/metrics.bad.json   # 预期 1
python dict/validate.py --check dict/metrics.json --write-history dict/history.snapshot.json
```

改过口径（`definition` 变了）时**必须同时升 `version`**，否则 V8 报「同一个版本两个口径」。

## 我在这里做的四个判断（契约没写死 / 写得有歧义，需要 lead 拍一下）

### 1. schema 快照里的「表名」怎么解析（契约 §3.2 V4/V5 只写「当前 schema 快照」）

`requires_tables` 写的是 `"sales"`，但 §5.1 的 `schema` 形状里只有 `file` / `sheets` / `columns`，没有 `table`。
本文件取契约里**已经出现过**的两个名字（§2.5 `__rid` 的 `<文件basename>#<sheet>`、§2.15 `d3_snapshot` 的 `{"table":"sales"}`）：

- 主名 = 文件 basename 去掉扩展名（`eval/data/sales.xlsx` → `sales`）；
- 别名 = sheet 名；
- 一个文件多个 sheet 时主名降级成 `sales#Sheet1` 这种形式，避免撞名。

`requires_tables` 与 `definition` 里的 `表.列` 限定名都能用主名或别名解析；解析不到就报 `COLUMN_NOT_FOUND`。
一张表里没有的东西不猜：同一列在多张表里都存在时，靠 `requires_tables` 收窄；收不窄就报错，不替用户选。

### 2. V10 是**双向**的（`window` 非空 ⟺ `time_column` 非空），后果有点反直觉

按契约字面实现，双向都校验。结果是：**非同比/环比指标不能填 `time_column`**——
`gmv` 填了 `sale_date` 但 `window` 是 `null`，就违反「⟺」。所以本字典里只有 `gmv_mom` 带 `time_column`，其余写 `null`。
这与 §3.1 里 `time_column` 字段说明（「时间口径落在哪一列；纯无时间指标写 null」）读起来是矛盾的：
「有时间列、但不做同比/环比」这种最常见的情况在双向读法下无法表达。

**没有放宽**：仍然双向校验（`dict/validate.py` 的 `V10` 分支），`--selftest` 里两个方向各有一条反例。
如果 lead 裁定应改成单向（`window` 非空 ⟹ `time_column` 非空），是一行改动，改完字典再把 `time_column` 填回去。

### 3. 静态检查已否决的 `definition` 不再解列名

`SUM(amount) DELETE` 这种已经被 §8.1 判 `SQL_STATIC_REJECT` 的文本，不再继续抽列名去撞 V4。
理由：那条 SQL 无论如何都不会被执行，再报一串 `COLUMN_NOT_FOUND` 只会淹没第一条错误。
**没有放宽**——`SQL_STATIC_REJECT` 本身就是字典加载失败。

另外两处按「不放宽」处理，都是刻意选择的严格读法：

- **V11**：`definition` 必须以 `SUM/AVG/COUNT/MIN/MAX(` 开头（可带一层外括号），否则「外层聚合函数」不可判定，报错而不是放过。比率型指标如果写成 `SUM(a)/COUNT(b)` 会被拒；
- **§3 排序**：`metrics` 不按 `id` 字节序排列即报错（否则 `meta.metric_dict_sha256` 不稳）。

### 4. 快照文件加了一层带版本串的文档壳（v1.1 对齐时做的）

契约 §5.1 里 `schema` 的值是**裸数组** `[{file, sha256, sheets}]`，本身没有地方放 `contract_version`。
可 v1.1 要求 `dict/` 侧所有文件版本串对齐（迁移清单：`dict/metrics.json`、`dict/history.snapshot.json`），
而 `dict/schema.snapshot.json` 同样是「字典侧快照」，版本串对不上就无法判断它属于哪一版契约。

所以本目录把它包成 `{"contract_version":"1.1","schema":[<裸数组>]}`：

- 裸数组形态**仍然接受**（`--selftest` 和调用方直接传 §5.1 的 `schema` 值都走这条）；
- 包装形态下 `contract_version` **必填**且必须等于 `CONTRACT_VERSION`，缺了或对不上 → `SCHEMA_UNREADABLE`
  （`history.snapshot.json` 同理，缺/对不上 → `DICT_LOAD_FAILED`）；
- 这是**容器形状**的收口，没改任何一条 V 规则、没碰任何口径定义。

如果 lead 裁定这条不许自己定（比如要求 `schema.snapshot.json` 保持裸数组、版本串只写在 `metrics.json`），
改回去是 `build_schema` 三行 + 一个文件的事；但那样 `dict/schema.snapshot.json` 里就不会出现 `contract_version`。

## 错误码

- 条目级：`METRIC_INVALID`（V1/V2/V5–V11）、`SQL_STATIC_REJECT`（V3）、`COLUMN_NOT_FOUND`（V4）——全部取自 §2.11 封闭枚举，**没有新增码**；
- 整份字典：按 V12 汇总后报 `DICT_LOAD_FAILED`，**一次列出全部错误**，不 fail-fast、不跳过坏条目；
- 快照缺失 / 形状不对 / `contract_version` 不是 `"1.1"`：`SCHEMA_UNREADABLE`（schema 快照）、`DICT_LOAD_FAILED`（history 快照）——不放宽成「跳过 V4/V5」或「跳过 V8 历史比对」。

## 数据须知

`metrics.json` 里的 `id` / 名称 / 口径表达式 / `owner` / `version` 是**骨架期的示意条目**（沿用契约里
`gmv`、`sales`、`amount`、`status`、`sale_date` 这套例子名），`schema.snapshot.json` 的 `sha256`
写 `null`（表示「没有」，不是编一个哈希）。真实导入的 schema 落地后，用真实 schema 快照替换这两个文件，
并逐个确认口径负责人；本文件不提供任何实测统计数字。
