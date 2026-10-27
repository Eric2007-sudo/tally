"""ask.joins · 跨表列解析 + JOIN 路径（契约 §5.1 D3 `joins[]` / §2.5 JOIN 行 id）

**这一步（#12）要解决的事**：D1 只给「列名」，不给「哪张表的列」。D3 走兜底时，
若某个维度/指标列不在主表里，必须自己找到「含这一列的表」并推出等值 JOIN。
本模块只做**结构推导**（列 → 表 → 等值键 → 连接路径），不生成 SQL 文本、不执行。

**契约写清了的**（照做）：
- `joins[]` 形状 `{"left","right","on":[[左列, 右列],...],"type":"inner"|"left"}`（§5.1 D3）；
- 反向条件（「没有 / 没有任何 / 从未 / 不存在」）必须 `LEFT JOIN … WHERE 右表键 IS NULL`，
  **不许**用 `INNER JOIN` 糊过去——那正好答反（本步简报第三节 ③）；
- 多对一（维表）可以，一对多会把金额重复累加，必须拒绝（简报 ③/④）。

**契约没写清、本实现自己定的四条（报告里要报）**：
1. **主表怎么选**：含全部指标定义列的候选里，优先指标字典 `requires_tables` 声明的那张，
   其次行数多的（事实表），最后按导入顺序。
2. **维表优先**：某个维度在主表里有同名列、同时在另一张「可等值连接、且右表键唯一」的
   表里也有时，取**维表**的那一列（主表里那列是冗余反规范化列）。只在「本次导入的表 ≥ 2 张」
   时才会发生，单表题不受影响。
3. **唯一键判据**：`unique_count == 行数` 且无空值才算「右表键唯一」（多对一）。
   右表键不唯一 = 一对多，**不连**（会放大金额）；找不到唯一键就换别的同名列，
   全部不唯一则如实报 `COLUMN_NOT_FOUND`，不硬连。
4. **桥接表**：两张表没有同名列时，允许经过**同目录下另一张表**做一跳桥（q039：
   `sales.product_id → product_dim.product_id`、`product_dim.brand → brand_dim.brand`）。
   桥只用作中间表，且每一跳都要求「右表键唯一」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .decide import DecisionError

# 反向条件：这些词出现在问题里 → 「不存在」语义（简报 ③）
NEGATION_WORDS = ("没有任何", "从未", "没有", "不存在", "未曾", "未有过")

# 「付款/支付」类问题：用来在候选表里认出「存在性表」（如 payment）
PAY_WORDS = ("付款", "支付", "付费")
PAID_MARKERS = ("已", "paid", "yes")

# 「状态」类列名：认存在性表的判据之一
STATUS_HINTS = ("status", "state", "状态")

JOIN_TYPES = ("inner", "left")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class JoinStep:
    """一跳 JOIN：`left` 是已经在前面的表，`right` 是新加进来的表。"""

    left: str
    right: str
    on: list  # [[左列, 右列], ...]
    type: str = "inner"
    on_extra: str = ""  # 追加在 ON 后面的条件（反向条件的时间范围落在这里）

    def as_dict(self) -> dict:
        return {"left": self.left, "right": self.right, "on": [list(p) for p in self.on], "type": self.type}


@dataclass
class JoinSource:
    """一步 SQL 的取数源：主表 + 连接链 + 每列从哪张表取。"""

    base: str
    tables: list = field(default_factory=list)  # 参与连接的全部表名（base 在前）
    joins: list = field(default_factory=list)  # list[JoinStep]
    owner: dict = field(default_factory=dict)  # 输出列名 → 表名
    projected: list = field(default_factory=list)  # [(表名, 列名, 输出别名)]
    anti: dict | None = None  # 反向条件：{"fact","key","predicate"}
    pool_used: list = field(default_factory=list)  # 用到哪些桥接表（要补进 meta.files）

    @property
    def joined(self) -> bool:
        return bool(self.joins)

    @property
    def rid_text(self) -> bool:
        """JOIN 之后源行号来自子查询里的文本（`__src_rowid`），不再是内存表行号。"""
        return self.joined

    def as_joins(self) -> list:
        return [step.as_dict() for step in self.joins]


# --------------------------------------------------------------------------- #
# 表结构判据
# --------------------------------------------------------------------------- #


def column_names(table) -> list:
    return [c.name for c in table.columns]


def _column(table, name):
    for col in table.columns:
        if col.name == name:
            return col
    return None


def is_unique_key(table, name: str) -> bool:
    """多对一的「一」：无空值 + 唯一值个数等于行数。"""
    col = _column(table, name)
    if col is None or not table.rows:
        return False
    if col.null_ratio:
        return False
    return col.unique_count == len(table.rows)


def _shared(left, right) -> list:
    names = set(column_names(right))
    return [name for name in column_names(left) if name in names]


def _key_for(left, right) -> list | None:
    """`left → right` 的等值键（`[左列, 右列]`，两侧同名）。

    要求**右表**在键上唯一（多对一）：否则 JOIN 会放大行数、把金额重复累加。
    """
    candidates = [name for name in _shared(left, right) if is_unique_key(right, name)]
    if not candidates:
        return None
    # 优先「主表这边不唯一（外键）→ 右表唯一（主键）」的形态；再按列名字节序稳定
    candidates.sort(key=lambda name: (0 if not is_unique_key(left, name) else 1, name))
    return [candidates[0], candidates[0]]


def find_path(tables: dict, start: str, goal: str, *, banned=()) -> list | None:
    """从 `start` 到 `goal` 的等值连接路径（BFS，每跳都要求右表键唯一）。→ list[JoinStep] 或 None。"""
    if start == goal:
        return []
    seen = {start}
    queue = [(start, [])]
    while queue:
        current, steps = queue.pop(0)
        for name, table in tables.items():
            if name in seen or name in banned or name == start:
                continue
            key = _key_for(tables[current], table)
            if key is None:
                continue
            seen.add(name)
            step = JoinStep(left=current, right=name, on=[key], type="inner")
            if name == goal:
                return steps + [step]
            queue.append((name, steps + [step]))
    return None


# --------------------------------------------------------------------------- #
# 选主表 / 选维表
# --------------------------------------------------------------------------- #


def choose_base(tables: dict, needed_columns, metric_tables=(), prefer=()) -> str:
    """含全部指标定义列的表里挑主表。规则见模块 docstring 第 1 条。"""
    needed = [c for c in needed_columns if c]
    candidates = [
        name for name, table in tables.items()
        if all(c in set(column_names(table)) for c in needed)
    ]
    if not candidates:
        available = {name: column_names(table) for name, table in tables.items()}
        raise DecisionError(
            "COLUMN_NOT_FOUND",
            f"没有任何一张表同时含有这一步需要的指标列 {needed}（各表列：{available}）",
            stage="D3",
            readable_hint="这一步要的指标列分散在不同数据里，没法确定用哪张表算",
        )
    declared = [name for name in metric_tables if name in candidates]
    if declared:
        return declared[0]
    order = [name for name in prefer if name in candidates]
    rest = [name for name in candidates if name not in order]
    rest.sort(key=lambda name: (-len(tables[name].rows), name))
    return (order + rest)[0]


def _pick_dim_table(tables: dict, base: str, dim: str, *, prefer=()):
    """维度列落在哪张表 → `(表名, 路径)`；找不到可连接的表 → None。"""
    base_cols = set(column_names(tables[base]))
    best = None
    for name, table in tables.items():
        if name == base or dim not in set(column_names(table)):
            continue
        path = find_path(tables, base, name)
        if path is None:
            continue
        # 主表里已经有这一列时，只在对方是「更小的维表」时才换过去（模块 docstring 第 2 条）
        if dim in base_cols and len(table.rows) >= len(tables[base].rows):
            continue
        rank = (
            0 if name.endswith("_dim") else 1,
            len(table.rows),
            list(prefer).index(name) if name in prefer else len(prefer),
            name,
        )
        if best is None or rank < best[0]:
            best = (rank, name, path)
    return None if best is None else (best[1], best[2])


# --------------------------------------------------------------------------- #
# 反向条件（不存在 / 从未）
# --------------------------------------------------------------------------- #


def has_negation(question: str) -> bool:
    """「没有 / 没有任何 / 从未 / 不存在」才算反向条件。

    「有没有…」「是否存在…」是**是非问**，不是「不存在」语义（q060 被误判会去连维表），
    所以先去掉这两种问法再找否定词。
    """
    text = (question or "").replace("有没有", "").replace("是否存在", "").replace("是否", "")
    return any(word in text for word in NEGATION_WORDS)


def _paid_value(column, values):
    """从状态列的值里挑出「已付」那一侧（模块 docstring 未写死，只认三个标记）。"""
    for value in sorted(values, key=lambda v: str(v)):
        text = str(value).lower()
        if any(marker in text for marker in PAID_MARKERS):
            return value
    return None


def _presence_table(tables: dict, base: str, question: str):
    """找「存在性表」：与主表有唯一键、且带一个「状态」列、问题里点了这一列相关的词。"""
    if not any(word in (question or "") for word in PAY_WORDS):
        return None
    for name, table in tables.items():
        if name == base:
            continue
        key = _key_for(tables[base], table)
        if key is None:
            continue
        for col in table.columns:
            low = col.name.lower()
            if not any(hint in low for hint in STATUS_HINTS):
                continue
            values = {row.get(col.name) for row in table.rows if row.get(col.name) is not None}
            paid = _paid_value(col, values)
            if paid is None:
                continue
            return {"table": name, "key": key, "column": col.name, "value": paid}
    return None


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def plan_source(
    tables: dict,
    *,
    dimensions,
    metric_columns,
    metric_tables=(),
    prefer=(),
    question: str = "",
    pool: dict | None = None,
) -> JoinSource:
    """算一步 SQL 的取数源。

    `tables`：本次导入（`--data` 声明）的表；`pool`：同目录下别的表（只在需要桥接时才用）。
    """
    pool = pool or {}
    needed = list(dict.fromkeys(list(metric_columns)))
    base = choose_base(tables, needed, metric_tables, prefer)

    source = JoinSource(base=base)
    source.owner = {name: base for name in column_names(tables[base])}

    negation = has_negation(question) and len(tables) >= 2

    # ---- 反向条件 ①：有维度 → 以维表为基，LEFT JOIN 事实表，WHERE 事实键 IS NULL ----
    if negation and list(dimensions):
        dim = list(dimensions)[0]
        picked = _pick_dim_table(tables, base, dim, prefer=prefer)
        if picked is not None:
            owner, _path = picked
            available = {**tables, **pool}
            fact = base
            if dim in column_names(available[owner]) and dim in column_names(available[fact]):
                source.base = owner
                source.tables = [owner, fact]
                source.owner = {name: owner for name in column_names(available[owner])}
                source.owner[dim] = owner
                source.joins = [JoinStep(left=owner, right=fact, on=[[dim, dim]], type="left")]
                source.anti = {"fact": fact, "key": [dim, dim], "predicate": None}
                if owner in pool and owner not in tables:
                    source.pool_used.append(owner)
                return _finish(source, available, dimensions, metric_columns, fact)

    # ---- 反向条件 ②：没有维度 → 主表 LEFT JOIN 存在性表，WHERE 存在性键 IS NULL ----
    if negation and not list(dimensions):
        found = _presence_table(tables, base, question)
        if found is not None:
            step = JoinStep(
                left=base,
                right=found["table"],
                on=[found["key"]],
                type="left",
            )
            source.joins = [step]
            source.tables = [base, found["table"]]
            source.anti = {
                "fact": found["table"],
                "key": found["key"],
                "predicate": {"column": found["column"], "value": found["value"]},
            }
            return _finish(source, tables, dimensions, metric_columns, base)

    # ---- 普通：主表 + 每个维度所在的维表（可按池子桥接） ----
    joins: list = []
    linked = {base}
    available = {**pool, **tables}
    for dim in dimensions:
        if dim == "period":
            continue
        picked = _pick_dim_table(tables, base, dim, prefer=prefer)
        if picked is None and dim not in column_names(tables[base]):
            # 已导入的表里没有这一列 → 才去同目录的池子里找桥接（不许把单表题连成多表）
            picked = _pick_dim_table(available, base, dim, prefer=prefer)
        if picked is None:
            if dim in column_names(tables[base]):
                continue
            available_cols = {name: column_names(table) for name, table in tables.items()}
            raise DecisionError(
                "COLUMN_NOT_FOUND",
                f"维度 {dim!r} 只在别的表里，且没有任何一张表能与主表 {base} 等值连接"
                f"（各表列：{available_cols}）",
                stage="D3",
                readable_hint=f"「{dim}」这张表与主表没有共同键，不能凭猜拼过去",
            )
        owner, path = picked
        for step in path:
            if step.right in linked:
                continue
            joins.append(step)
            linked.add(step.right)
            if step.right in pool and step.right not in tables:
                source.pool_used.append(step.right)
        source.owner[dim] = owner
    source.joins = joins
    source.tables = [base] + [step.right for step in joins]
    return _finish(source, available, dimensions, metric_columns, base)


def _finish(source: JoinSource, available: dict, dimensions, metric_columns, fact: str) -> JoinSource:
    """把「要输出的列」落成 `(表, 列, 别名)`：主表全列 + 别的表提供的维度 + 指标定义列。"""
    projected: list = []
    seen: set = set()

    def add(table: str, column: str) -> None:
        if column in seen:
            return
        projected.append((table, column, column))
        seen.add(column)

    base_cols = column_names(available[source.base])
    overridden = {dim for dim in dimensions if source.owner.get(dim, source.base) != source.base}
    for name in base_cols:
        if name in overridden:
            continue
        add(source.base, name)
    for dim in dimensions:
        owner = source.owner.get(dim, source.base)
        if owner != source.base:
            add(owner, dim)
    for column in metric_columns:
        if column in seen:
            continue
        holder = fact if column in column_names(available[fact]) else source.base
        if column not in column_names(available[holder]):
            for name, table in available.items():
                if column in column_names(table):
                    holder = name
                    break
        if column in column_names(available[holder]):
            add(holder, column)
    source.projected = projected
    return source
