"""eval/ · #11 的抽样名单（`ROSTER`）—— **名单钉死在这里，不由类别现算**。

为什么单独一个文件：`--check 20` 以前只验「20 题 / JOIN 全 / 字段齐 / 维度对」，
没有任何一处比对名单，所以换掉几道题照样全绿（上一轮就把 q008 / q020 / q025 /
q042 / q053 / q057 换成了 q002 / q007 / q009 / q021 / q055 / q058，四条 v1.1 新判据
的覆盖被悄悄丢掉，报告里没写一句）。名单是这一步的**输入**，不是这一步步跑出来的
结果，所以它必须写死、必须被比对，而不是从 `eval/questions.jsonl` 的类别里算出来。

口径（整步 note 里的原话）：
    20 题 = 10 道 JOIN 全部（q031-q040）+ 10 道其他类（每类 2 道，按 qid 字节序取前两个）
    —— 筛选 q001 q008 / 聚合 q013 q020 / 同比环比 q023 q025 /
       排名 q041 q042 / 异常 q053 q057

这 20 道正好覆盖契约 v1.1 四个新字段的代表题：
- q008  钉 `time_column`（时间过滤落在 customer.register_date）
- q013  钉 `time_grain_grouping` + 派生列 `period`
- q023 / q025 钉 `comparison` 与 `metric.window`（两期对比 / 同比）
- q031-q040 钉「多表不再反问」（D3 必须显式写 `joins[]`）
- q053 / q057 钉 `RESULT_EMPTY`（空集就是正确答案，不许编一个数）

用法：`eval/fourd.py` 按文件路径把这个模块读进来（`ROSTER` 与 `ROSTER_BASIS`），
`--check` 第一步比 `decisions.json` 的题号集合与 `ROSTER`，不等就 exit 1 并打出差的题号。
注意它**不能**被 `import eval.fourd.fourd` 引用：`eval.fourd` 是模块（`eval/fourd.py`），
不是包。所以这里只放常量，不放任何 import。
"""

from __future__ import annotations

#: 20 题的**唯一**名单（顺序 = 报告 §1 的顺序）。改这一行就是改抽样口径。
ROSTER: tuple[str, ...] = (
    "q031", "q032", "q033", "q034", "q035",
    "q036", "q037", "q038", "q039", "q040",
    "q001", "q008", "q013", "q020", "q023",
    "q025", "q041", "q042", "q053", "q057",
)

#: 每道题「为什么是它」（报告 §1 的「扮演这一步的理由」列与 `--check` 的提示共用）。
ROSTER_BASIS: dict[str, str] = {
    "q001": "筛选类第 1 个：单表单标量 + 区域过滤，四段全注入的最小用例",
    "q008": "筛选类第 2 个：钉 v1.1 的 time_column——时间过滤落在 customer.register_date",
    "q013": "聚合类第 1 个：time_grain_grouping=month → 派生列 period，12 行 12 条 trace",
    "q020": "聚合类第 2 个：全年合计的标量形态（无分组无过滤）",
    "q023": "同比环比第 1 个：comparison（Q2 比 Q1），要一条 SQL 里含 CTE/窗口并产出 __delta / __pct",
    "q025": "同比环比第 2 个：metric.window=yoy，缺 2023 基期时为 RESULT_EMPTY",
    "q041": "排名类第 1 个：ORDER BY DESC + LIMIT 10",
    "q042": "排名类第 2 个：ORDER BY ASC + LIMIT 5",
    "q053": "异常类第 1 个：RETURN 率口径 + 异常阈值，结果是空集（RESULT_EMPTY）",
    "q057": "异常类第 2 个：region × period 双维 + 自身月度均值基线（RESULT_EMPTY）",
    "q031": "JOIN 全集：区域中文全称只在 region_dim，必须 sales ⋈ region_dim",
    "q032": "JOIN 全集：行业取自 customer 维表（D3 必须显式写 joins，不许降级成只查事实表）",
    "q033": "JOIN 全集：反连接语义（没有任何成交的类目，只能从维表出发左连事实表）",
    "q034": "JOIN 全集：销售员取自 employee_dim，一题两个指标",
    "q035": "JOIN 全集：城市取自 customer 维表",
    "q036": "JOIN 全集：渠道与退款金额在 refund 表，时间范围在 sales",
    "q037": "JOIN 全集：分组维度一个来自事实表、一个来自维表",
    "q038": "JOIN 全集：下过单但从未付款（付款表配合），「从未」在契约里没有字段可表达",
    "q039": "JOIN 全集：两跳 JOIN（sales → product_dim → brand_dim）",
    "q040": "JOIN 全集：客户等级取自 customer 维表，一题两个指标",
}

#: 一句话说明名单的口径（`--check` 与报告都直接用这句，免得两处说法打架）。
ROSTER_RULE = ("10 道多表连接（q031-q040，JOIN 全集）+ 10 道其它类每类 2 道"
               "（按 qid 字节序取前两个）：筛选 q001 q008 / 聚合 q013 q020 / "
               "同比环比 q023 q025 / 排名 q041 q042 / 异常 q053 q057")


def main() -> int:
    """自检用：不依赖 ask/，也不写任何文件，只把名单打出来。"""
    import sys
    if len(ROSTER) != 20:
        print(f"ROSTER 长度 {len(ROSTER)} ≠ 20", file=sys.stderr)
        return 1
    if len(set(ROSTER)) != len(ROSTER):
        print("ROSTER 里有重复题号", file=sys.stderr)
        return 1
    missing = [q for q in ROSTER if q not in ROSTER_BASIS]
    if missing:
        print(f"ROSTER_BASIS 缺 {missing}", file=sys.stderr)
        return 1
    print(f"ROSTER {len(ROSTER)} 题：{' '.join(ROSTER)}")
    print(ROSTER_RULE)
    for qid in ROSTER:
        print(f"  {qid}  {ROSTER_BASIS[qid]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
