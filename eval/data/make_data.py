"""eval/data · 合成数据生成器（确定性，无随机数）

这是**评测用的合成数据**，不是真实业务数据：数值按固定公式算出来，所以「真值」是已知的，
questions.jsonl 里的 `expect` 就是真值口径（哪一题该用哪个指标、按什么维度分组、要不要连表）。

产物（全部落在 eval/data/）：
    sales.csv / customer.csv / region_dim.csv / product_dim.csv /
    employee_dim.csv / payment.csv / refund.csv / brand_dim.csv
    metrics.json          —— 配套指标字典（只描述这份合成数据，别当成项目字典）

设计取舍（写在 eval/README.md 里，这里只留一句话）：
- `sales.csv` 是一张**宽表快照**：除 region 编码外还冗余了 customer_level / city / industry /
  sales_owner，这样「筛选/聚合/同比环比/排名/异常」这些**非 JOIN 题**只靠 sales 就能算；
  维度维表（region_dim / customer / employee_dim / product_dim / brand_dim）保持主数据原样，
  留给「多表连接」那 10 题真去连。
- 埋了四种脏数据，异常题才有东西可查：重复单号（i=20）、负金额（i=9）、空金额（i=17）、
  测试账号（U06）。

重跑幂等：同样的公式永远产出同样的字节。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

REGIONS = [("R1", "华东", "王工"), ("R2", "华南", "李工"), ("R3", "华北", "赵工"), ("R4", "西南", "钱工")]
CATEGORIES = [("C1", "生鲜"), ("C2", "礼盒"), ("C3", "日用")]
BRANDS = [("B1", "青禾", "自有"), ("B2", "云岭", "代理"), ("B3", "海角", "自有")]
CUSTOMERS = [
    ("U01", "杭州云杉", "杭州", "零售", "VIP", "2023-05-11"),
    ("U02", "南京禾木", "南京", "零售", "普通", "2024-03-02"),
    ("U03", "上海镜像", "上海", "电商", "VIP", "2022-11-20"),
    ("U04", "广州南风", "广州", "电商", "普通", "2024-08-09"),
    ("U05", "成都青瓦", "成都", "餐饮", "VIP", "2023-01-30"),
    ("U06", "杭州测试账号", "杭州", "测试", "普通", "2025-01-05"),
]
EMPLOYEES = [("E1", "陈一", "R1"), ("E2", "林二", "R2"), ("E3", "周三", "R3")]
CHANNELS = ["线上", "线下"]
PAY_METHODS = ["微信", "支付宝", "转账"]
STATUSES = ["paid", "created", "refunded"]


def rows() -> list[dict]:
    """48 行：2024 年 12 行、2025 年 36 行；金额、数量按公式算，可复算。"""
    out: list[dict] = []
    for i in range(48):
        year = 2024 if i < 12 else 2025
        month = (i % 12) + 1
        day = (i % 27) + 1
        region = REGIONS[i % 4][0]
        cat = CATEGORIES[i % 3]
        cust = CUSTOMERS[i % 6]
        emp = EMPLOYEES[i % 3]
        status = STATUSES[i % 3]
        amount = 100 + (i % 7) * 50 + (i % 5) * 10          # 100 ~ 460
        if i == 9:
            amount = -30                                    # 留一条负金额（异常题用）
        if i == 17:
            amount = ""                                     # 留一条空金额（空值题用）
        quantity = 1 + (i % 4)
        refund = 0
        if status == "refunded":
            refund = round(amount * 0.5, 2) if amount not in ("", -30) else 0
        out.append({
            "order_id": f"O{1000 + i}" if i != 20 else f"O{1000 + 19}",   # i=20 重复单号
            "sale_date": f"{year}-{month:02d}-{day:02d}",
            "region": region,
            "category": cat[0],
            "customer_id": cust[0],
            "customer_level": cust[4],      # 宽表快照：非 JOIN 题要按客户等级分组
            "city": cust[2],
            "industry": cust[3],
            "sales_owner": emp[1],
            "employee_id": emp[0],
            "product_id": f"P{i % 5 + 1}",
            "channel": CHANNELS[i % 2],
            "payment_method": PAY_METHODS[i % 3],
            "amount": amount,
            "quantity": quantity,
            "status": status,
            "refund_amount": refund,
        })
    return out


def write_csv(name: str, header: list[str], body: list[list]) -> None:
    path = HERE / name
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(body)
    print(f"  写出 {name}（{len(body)} 行）")


def _m(**kw) -> dict:
    base = {
        "unit": "CNY",
        "owner": "运营-合成",
        "version": 1,
        "requires_tables": ["sales"],
        "aliases": [],
        "time_column": None,
        "window": None,
        "deprecated": False,
    }
    base.update(kw)
    return base


# 按 id 字节序排列（契约 §3 形状要求），否则字典整体哈希不稳
METRICS = {
    "contract_version": "1.1",     # 契约 v1.1（迁移清单：eval/data/metrics.json 必须跟着升）
    "metrics": [
        _m(id="avg_order_amount", name="平均订单金额", definition="AVG(amount)",
           aliases=["客单价"], default_aggregation="avg"),
        _m(id="customers", name="客户数", definition="COUNT(DISTINCT customer_id)", unit="个",
           aliases=["客户数量"], default_aggregation="count_distinct"),
        _m(id="customers_mom", name="客户数环比", definition="COUNT(DISTINCT customer_id)", unit="个",
           aliases=["客户数环比增幅"], default_aggregation="count_distinct",
           time_column="sale_date", window={"grain": "month", "compare": "mom"}),
        _m(id="gmv", name="成交额", definition="SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
           aliases=["销售额", "成交总额"], default_aggregation="sum"),
        _m(id="gmv_mom", name="成交额环比", definition="SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
           aliases=["成交额环比增长率"], default_aggregation="sum",
           time_column="sale_date", window={"grain": "month", "compare": "mom"}),
        _m(id="gmv_yoy", name="成交额同比", definition="SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
           aliases=["成交额同比增长"], default_aggregation="sum",
           time_column="sale_date", window={"grain": "month", "compare": "yoy"}),
        _m(id="max_order_amount", name="最大订单金额", definition="MAX(amount)",
           aliases=["最大单笔订单金额"], default_aggregation="max"),
        _m(id="min_order_amount", name="最小订单金额", definition="MIN(amount)",
           aliases=["最小单笔订单金额"], default_aggregation="min"),
        _m(id="order_amount", name="订单金额", definition="SUM(amount)",
           aliases=["订单总额"], default_aggregation="sum"),
        _m(id="orders", name="订单数", definition="COUNT(DISTINCT order_id)", unit="个",
           aliases=["订单量", "订单笔数"], default_aggregation="count_distinct"),
        _m(id="quantity", name="成交量", definition="SUM(quantity)", unit="个",
           aliases=["销量"], default_aggregation="sum"),
        _m(id="records", name="记录数", definition="COUNT(*)", unit="个",
           aliases=["明细记录数"], default_aggregation="count"),
        _m(id="refund_amount", name="退款金额", definition="SUM(refund_amount)",
           aliases=["退款总额"], default_aggregation="sum"),
        # 退货率是比值口径：契约 V11 要求 default_aggregation 与定义外层聚合一致，
        # 外层被识别为 SUM(refund_amount)，所以这里写 sum。
        _m(id="refund_rate", name="退货率", definition="SUM(refund_amount) / NULLIF(SUM(amount), 0)",
           unit="%", aliases=["退款率"], default_aggregation="sum"),
    ],
}


def main() -> None:
    data = rows()
    header = ["order_id", "sale_date", "region", "category", "customer_id", "customer_level",
              "city", "industry", "sales_owner", "employee_id", "product_id", "channel",
              "payment_method", "amount", "quantity", "status", "refund_amount"]
    write_csv("sales.csv", header, [[r[h] for h in header] for r in data])
    write_csv("customer.csv",
              ["customer_id", "customer_name", "city", "industry", "customer_level", "register_date"],
              [list(c) for c in CUSTOMERS])
    write_csv("region_dim.csv", ["region", "region_name", "region_owner"],
              [list(r) for r in REGIONS])
    write_csv("product_dim.csv", ["product_id", "product_name", "category", "brand"],
              [[f"P{i}", f"商品{i}", CATEGORIES[(i - 1) % 3][0], BRANDS[(i - 1) % 3][0]] for i in range(1, 6)])
    write_csv("employee_dim.csv", ["employee_id", "sales_owner", "region"], [list(e) for e in EMPLOYEES])
    write_csv("payment.csv", ["order_id", "pay_status", "channel"],
              [[f"O{1000 + i}", "已付" if i % 3 == 0 else "未付", CHANNELS[i % 2]]
               for i in range(48) if i != 20])
    write_csv("refund.csv", ["order_id", "refund_amount", "refund_status", "channel"],
              [[f"O{1000 + i}", round((100 + (i % 7) * 50) * 0.5, 2), "已退款", CHANNELS[i % 2]]
               for i in range(48) if i % 3 == 2 and i != 20])
    write_csv("brand_dim.csv", ["brand", "brand_group"], [[b[0], b[2]] for b in BRANDS])

    (HERE / "metrics.json").write_text(
        json.dumps(METRICS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  写出 metrics.json（{len(METRICS['metrics'])} 条指标）")


if __name__ == "__main__":
    main()
