# 贡献指南

## 先跑起来

```bash
git clone https://github.com/Eric2007-sudo/tally.git
cd tally
python ask/_selfcheck/run_all.py      # 60 题，离线，不调模型
```

零依赖，Python 3.8+。xlsx 用标准库 zip + XML 解，图表是手写 SVG。

## 改动前后都跑这一条

```bash
python ask/_selfcheck/run_all.py
```

它是唯一的总口径。当前基线是 **59/60**（唯一失败 q056 见 README §5）。
**改动让它变少就是回归**，别用「反正本来就不过」糊过去。

涉及这些子系统时，另跑对应自检：

```bash
python -m dict.validate --selftest     # 指标字典 V1–V12
python -m ask.ingest --selftest        # xlsx / csv 解析
python -m eval.check_questions         # 60 题题面与期望的合法性
```

## 三条不许破的规矩

1. **可追溯**：新增任何输出数值，必须同时给 `trace`（`value_path` 指到 `rows[i].列名`）。
   没有追溯链的数字等于没做完。
2. **只读 SQL**：任何路径都不许生成 `INSERT/UPDATE/DELETE/DROP/ALTER/CREATE`。
   `ask/static_check.py` 是唯一入口，别绕过它。
3. **口径来自字典**：不许在代码里硬写「毛利率 = ...」。口径写进 `dict/metrics.json`，
   由 `dict/validate.py` 校验。

## 不许做的事

- **不许改题或改期望来让数字好看。** `eval/questions.jsonl`、`eval/inject/*`、`eval/metrics.py`
  是尺子，不是被改的对象。题做不过就是实现不对。
- **不许放宽判据**：`status` 只能是 `ok` / `error` / `clarify`，不许加「部分通过」。
- **不许在自检里吞掉失败**：脚本有失败必须非 0 退出（这条踩过：此前永远 `exit 0`，
  11 题不过门禁照样绿）。

## 提交信息怎么写

一条提交说清**改了什么、为什么、代价是什么**。例如：

```
修时间过滤整段丢失：include_time 对非反连接算出 False

54 道带 time_range 的题里 47 道 SQL 没有日期字面量。
判分器只看结构不看过滤，所以一直是绿的。
```

## 提 issue 时带上

- `python --version`
- 触发命令（越短越好）
- 期望 vs 实际
- 如果和某个数字有关，附上对应的 `answer.json`（**先删掉绝对路径**）
