# GaussDB / OpenGauss CBO 与诊断深度知识

> 判断层按需加载的深度参考。覆盖单 SQL 与游标 SELECT 调优（两者本质相同）。
> GaussDB 与 OpenGauss 同源；**仅 GaussDB / 与原生 PostgreSQL 不同**的点标 `[GaussDB]`。
> 纪律：本文是「判断知识」，不是规则穷举。每个结论仍须由 脚本的证据（`## Execution Plan` / `## Column Statistics` / `## Verified Index Candidates`）或 `verify.py` 背书；本文只帮你把证据解读得更准、改写提得更对、少提会被驳回的方案。

---

## 1. 成本模型（CBO）

### 1.1 cost 公式与默认参数

```
total_cost = startup_cost + run_cost
  run_cost ≈ seq_page_cost × pages              (Seq Scan)
           + random_page_cost × index_pages     (Index Scan)
           + cpu_tuple_cost × rows
           + cpu_index_tuple_cost × index_rows
           + cpu_operator_cost × rows × 谓词数
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `seq_page_cost` | 1.0 | 顺序读一页 |
| `random_page_cost` | 4.0（**SSD 实例常调 1.1**） | 随机读一页；越接近 1.0 越鼓励走索引 |
| `cpu_tuple_cost` | 0.01 | 处理一行 |
| `cpu_index_tuple_cost` | 0.005 | 处理一条索引项 |
| `cpu_operator_cost` | 0.0025 | 算一次谓词/函数 |

实务：在 SSD 实例上 `random_page_cost` 常被设到 1.1（`## Key Parameters (GUC)` 里若看到 1.1，说明优化器已被告知随机读不贵，更愿走索引；若仍走 Seq Scan，问题多半在选择性估算或 sargable，而非参数）。

### 1.2 self_cost vs total_cost（定位瓶颈的关键）

GaussDB/PG 的计划里**父节点 `total_cost` 累积了所有子节点的成本**。按 `total_cost` 直接排序找瓶颈是错的——顶层 Limit/Sort 的 total_cost 永远最大。

正确做法：`self_cost = max(node.total_cost − Σ children.total_cost, 0)`，优先看 self_cost 占比高的**叶子扫描或算子**。脚本的 `## Deterministic Findings` 与 `[Pn]` 已按此预标；本节用于你复核「这个高 total_cost 的父节点到底是自己贵，还是只继承了下层」。

### 1.3 选择性（selectivity）

来自 `pg_stats`（对应证据 `## Column Statistics`）：

- `col = X`：命中 `most_common_vals` → 用 `most_common_freqs[X]`；否则 ≈ `1 / n_distinct`。
- `col > X` / 范围：用 `histogram_bounds` 二分定位 bucket。
- `n_distinct` 为负（如 −1）表示「按行数比例」：−1 = 每行唯一（高基数，索引友好）。
- `correlation` 接近 ±1 = 物理有序（利于 Index Scan 顺序读、减少随机 IO）；接近 0 = 无序（Index Scan 随机 IO 重，CBO 可能弃索引走 Bitmap 或 Seq Scan）。
- 多谓词默认按**独立性假设** `sel_a × sel_b` 相乘；关联列（如 city 与 zip）会严重低估行数。

### 1.4 估算失真的修复方向

`## Execution Plan`（带 ANALYZE 时）`estimated_rows` 与 `actual_rows` 偏差：

- 偏差 > 10× → 统计失真是主因。修复：`ANALYZE <表>`；关联列用 `CREATE STATISTICS ... (dependencies)` 建扩展统计；高频值倾斜调 `default_statistics_target` 后重 ANALYZE。
- 偏差 < 2× → 统计 OK，问题在算子/索引/sargable，而非统计。

---

## 2. 算子选择与 Join

### 2.1 Join 算子

| 算子 | 适用 | cost 直觉 | 计划里看什么 |
|---|---|---|---|
| Nested Loop | 内层 rows 很小 **或** 内层有索引可走 | `outer_rows × inner_lookup` | 内层是 Index Scan 才健康；内层 Seq Scan 大表 = 灾难 |
| Hash Join | 两边都不小、hash 装得进 `work_mem` | `build + probe` | `Hash Cond`；溢出见下 |
| Merge Join | 两边已排序，或 sort 比 hash 便宜 | `sort_o + sort_i + merge` | 两侧需有序输入 |

`work_mem` 不足的信号：Hash/Sort 节点出现 `Sort Method: external merge` 或 hash 分批（spill）。`## Key Parameters (GUC)` 里 `work_mem` 实例常见 16MB；改 `SET work_mem='64MB'`（会话级）可让溢出节点回到内存。

### 2.2 Join 顺序枚举

| `from_collapse_limit` / `join_collapse_limit` | 算法 | 后果 |
|---|---|---|
| ≤ 8 表（默认 8） | 动态规划全排列 | 全局最优 |
| > 8 表 | GEQO 遗传算法（`geqo_threshold` 默认 12） | 局部最优、不稳定 |

复杂 SQL（> 8 表 join）计划忽好忽坏时，`SET from_collapse_limit=20; SET join_collapse_limit=20;` 让 DP 完整跑。这是「计划不稳定」类问题的常见根因，纯 SQL 改写解决不了。

### 2.3 `[GaussDB]` Streaming 算子（与原生 PG 最大的不同）

原生 PG 并行用 `Gather`/`Gather Merge`。**GaussDB/openGauss 用 `Streaming` 算子**表示线程间/DN 间的数据流动（SMP 并行或分布式）。计划里会看到（真实样例）：

```
Streaming(type: LOCAL GATHER dop: 1/2)
Streaming(type: BROADCAST dop: 2/1)
```

类型含义与调优方向：

| Streaming 类型 | 含义 | 代价 / 优化方向 |
|---|---|---|
| `LOCAL GATHER` | 把本地多个并行线程结果汇聚到一个 | SMP 并行收尾，一般正常 |
| `LOCAL REDISTRIBUTE` | 本地线程间按 hash 重分布 | 并行 join 需要，关注分布是否均衡 |
| `BROADCAST` | 把一侧整表广播给所有线程/DN | **广播大表很贵**；应让被广播侧是小表，否则改写/调分布 |
| `REDISTRIBUTE` | 按分布列跨 DN 重分布（分布式部署） | **跨 DN 网络重分布最贵**；让 join 键 = 分布列可消除它 |

- `dop: a/b` 是并行度（生产者/消费者线程数），`query_dop` GUC 控制 SMP 并行度。
- **诊断要点**：计划里若大表走 `BROADCAST` 或 `REDISTRIBUTE`，往往是「分布列（分布式）/并行重分布」没对齐 join 键。分布式部署里，把两张大表的**分布列设成 join 键**可把 join 下推为本地 join，消除最贵的 REDISTRIBUTE——这是 GaussDB 调优里 PG 完全没有的一类根因。
- 这类是「结构/DDL（分布列）」层面问题，本技能当前只读 SELECT 不改分布；遇到时归「建议（未验证）」，明确指出分布列与 join 键不一致。

### 2.4 `[GaussDB]` 行存 vs 列存对计划的影响

- `ORIENTATION=ROW`（默认，OLTP）走普通算子；`ORIENTATION=COLUMN`（列存，OLAP）走**向量化算子**：计划里出现 `CStore Scan`、`Vector Sort`、`Vector Hash Aggregate`、`Vector Streaming` 等 `Vector*` 前缀。
- 列存按 CU（Compression Unit，默认 6 万行/CU）批量扫描，**点查/高选择性单行查询在列存上很差**；列存适合大范围聚合扫描。
- 诊断边界：**不要给列存表建 B-tree 然后期待点查走索引**；列存的「索引」是 min/max 稀疏索引 + CU 裁剪。看到 `CStore Scan` 时，索引类建议要按列存逻辑给，不能照搬行存。

### 2.5 `[GaussDB]` Ustore vs Astore（存储引擎）

- **Astore**（append-only，类 PG heap）：更新产生新版本 + 死元组，靠 VACUUM 回收；更新密集表易膨胀。
- **Ustore**（In-place Update Store，GaussDB/openGauss 引入）：原地更新 + undo 回滚段，**更新密集场景膨胀显著更小**、长事务对膨胀的放大也更可控。
- 调优影响：诊断「表膨胀/更新慢/VACUUM 压力」时，先看表是 Astore 还是 Ustore——Astore 更新密集表的膨胀，结构层建议可含「评估迁 Ustore」（属建议，未验证）。这是 PG 没有的一条诊断分支。

---

## 3. 索引与 sargable

### 3.1 CBO 走 Index Scan 的前提

1. 谓词命中索引**前导列**（或表达式索引匹配谓词里的函数）。
2. 估算命中行数足够小（经验 < 表的 10–30%，否则全表/位图更划算）。
3. 谓词 **sargable**：列上无函数包裹、无隐式类型转换。

### 3.2 CBO 不走索引的常见原因（按出现频率）

- **谓词被函数包裹**：`to_char(d,'YYYY-MM-DD')=...`、`upper(c)=...`、`substr(...)`、`col+1=...` → 列索引失效。改法：改写成 sargable 形式（日期改范围 `d >= ... AND d < ...`），或建表达式索引。
- **隐式类型转换**：列是 `varchar` 谓词给数字、列是 `bigint` 谓词给字符串字面量 → 转换阻断索引。改法：让字面量类型与列一致。
- **估算行数过大**（统计失真）：见 §1.4。
- **correlation 接近 0**：物理无序，Index Scan 随机 IO 太贵，CBO 主动弃索引。可考虑 Bitmap 或按该列 CLUSTER。

### 3.3 `[GaussDB]` 原生索引验证手段

- `gs_index_advise('<SQL>')`：GaussDB/openGauss 内置索引顾问，直接给候选索引（PG 无此函数）。
- hypopg 假设索引：`SET enable_hypo_index=on;` + `hypopg_create_index('CREATE INDEX ...')` → 虚拟索引，只 `EXPLAIN` 估算、不真正建（毫秒级、零 WAL）。脚本的 `## Verified Index Candidates` 即基于此。
- 索引类型：B-tree（默认）、GIN（数组/全文/JSONB）、GiST、Hash；列存表用 PSORT/min-max，不用 B-tree 点查。

---

## 4. 读 EXPLAIN：GaussDB 节点速查

脚本已用 `scan_plan` 预标 `## Deterministic Findings` 与 `[Pn]`，本表用于补充推理：

| 节点 | 健康信号 | 风险信号 |
|---|---|---|
| Seq Scan | 小表 / 高选择率 | 大表 + 选择性高谓词 → 缺索引或不可 sargable |
| Index Scan / Index Only Scan | 高选择率点查 | correlation≈0 时随机 IO 重 |
| Bitmap Heap/Index Scan | 中等选择率 | — |
| Nested Loop | 内层 Index Scan、内层小 | 内层 Seq Scan 大表（loops × 全表）|
| Hash Join | hash 进 work_mem | spill / 分批 → work_mem 不足 |
| Sort / HashAggregate | 内存内 | `Sort Method: external` → work_mem 不足 |
| `[GaussDB]` Streaming BROADCAST/REDISTRIBUTE | 小表广播 | 大表广播/跨 DN 重分布 → 分布列/并行未对齐 |
| `[GaussDB]` CStore Scan / Vector* | 大范围聚合 | 点查走列存 → 选错存储 |

判读顺序：① self_cost 最高的叶子 → ② estimated vs actual rows 偏差（统计） → ③ 算子是否选错 → ④ `Filter` 里出现本应进 `Index Cond` 的谓词（sargable 问题） → ⑤ `[GaussDB]` 有无昂贵 Streaming。

---

## 5. 诊断边界 guardrails（防误判，价值最高）

这些是「看起来像问题、其实未必」的边界。**踩这些会被 verify.py 当场驳回，浪费一轮；写进建议层（不验证的部分）更是直接降低报告可信度。**

- **逗号隐式连接 vs 显式 INNER JOIN**：性能**等价**。没有「漏连接条件 / 笛卡尔积」证据时，只能作可读性风格提示，**不得列为性能反模式**。
- **DISTINCT 与 GROUP BY 共存**：只有当 `SELECT DISTINCT` 列集与 `GROUP BY` key 语义等价才可判冗余；**仅凭共存只能标「不确定」**，不能直接说去掉 DISTINCT 更快（多半 1.0×，会被 verify 驳）。
- **`NOT IN` 改 `NOT EXISTS`**：**NULL 语义不等价**。子查询列可空时，`NOT IN` 遇 NULL 返回空集，`NOT EXISTS` 不会。除非确认子查询列 `NOT NULL` 或补 `IS NOT NULL`，否则**不得声称等价**——这类改写必须过 `verify.py` 的等价门。
- **`SELECT *`**：本身不是性能问题，除非确证宽行/大字段（如大 TEXT/BYTEA）被无谓拉取或阻断 Index Only Scan；否则只作风格提示。
- **`OR` 谓词**：跨列 `OR` 常阻断索引；可考虑 `UNION ALL` 拆分或建组合索引——但**收益要 verify**，不要默认更快。
- **`LIMIT` + 大 `OFFSET` 深分页**：`OFFSET 100000` 仍扫前 10 万行；改键集分页（`WHERE id > last_id`）。这是真问题，但属改写，需保持游标列契约并过 verify。
- **`[GaussDB]` 列存表上想靠 B-tree 点查**：见 §2.4，列存逻辑不同，别照搬行存索引建议。
- **`[GaussDB]` Ustore 表用 Astore 的膨胀直觉**：Ustore 膨胀模型不同，别套 Astore 的 VACUUM 结论。

---

## 6. 正反例（带 GaussDB 数字）

**❌ 含糊**：「Hash join 内存不够可能溢出，建议加 work_mem。」
**✅ 带证据**：「Hash 节点 #5 `Sort Method: external merge`，当前 `work_mem=16MB`，hash 表估算需 ~48MB。`SET work_mem='64MB'` 后该节点回内存 hash，`## Execution Plan` 复跑 cost 8500 → 1200（7.08×）。」

**❌ 泛泛**：「建议加索引优化查询。」
**✅ 具体**：「`orders.order_date` n_distinct=730、谓词命中约 1 天 ≈ 0.14%，但 `Filter: to_char(order_date,'YYYY-MM-DD')='2024-01-15'` 函数包裹使索引失效。改写为 `order_date >= '2024-01-15' AND order_date < '2024-01-16'` 并建 `orders(order_date)`，`verify.py` 实测 cost 29165 → 4030（7.24×），结果集等价。」

**❌ `[GaussDB]` 漏看 Streaming**：「join 慢，建议加索引。」
**✅**：「计划 `Streaming(type: BROADCAST)` 把 1000 万行的 `order_items` 广播给所有并行线程，self_cost 占比最高。根因是并行 join 未按 join 键重分布；属分布/并行层（DDL）问题，加索引无效——归建议层，指出对齐分布列/降 `query_dop` 再评估。」

---

## 7. GaussDB / OpenGauss vs 原生 PostgreSQL 差异速查

| 维度 | 原生 PostgreSQL | GaussDB / OpenGauss |
|---|---|---|
| 并行算子 | Gather / Gather Merge | **Streaming**（LOCAL GATHER / REDISTRIBUTE / BROADCAST），`query_dop` |
| 存储引擎 | heap（append） | **Astore + Ustore**（Ustore 原地更新、少膨胀） |
| 列存 | 无（需扩展） | **原生行存/列存**，向量化 `Vector*` / `CStore Scan` |
| 索引顾问 | 无内置 | **`gs_index_advise`** + hypopg（`enable_hypo_index`） |
| 性能视图 | pg_stat_* | **dbe_perf.\***（statement / statement_history），`track_stmt_stat_level` 控嵌套语句采集 |
| 兼容模式 | 单一 | PG 兼容 + **A 兼容（Oracle）**：package、`CURSOR c IS`、`FORALL`、`BULK COLLECT` |
| 分布式 | 单机 | 可分布式（DN/CN），**分布列**决定 join 是否下推为本地 |

判断时优先用左右差异定位 GaussDB 特有根因（Streaming 重分布、列存选错、Ustore/Astore、分布列），这些是把「泛 PG 知识」升级成「确实是 GaussDB 知识」的关键。
