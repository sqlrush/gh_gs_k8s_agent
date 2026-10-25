# GaussDB / OpenGauss 改写正例库

> 判断层按需加载。这是「等价保持、GaussDB 上真有效」的改写**候选菜单**——给 LLM 提供高质量改写候选，喂进 `verify.py`。
> 负向边界（什么看着像问题其实不能瞎改）见 `gaussdb-cbo-and-diagnosis.md` §5。
> 纪律：
> - 每条都是**候选**，必须过 `verify.py`（cost ≥ 1.3× 且 md5 结果集等价）才能呈现为已验证。
> - 游标 SELECT 改写须**保持输出列名与列序**（循环体用 `rec.col`）。
> - 标 ⚠️ 的有 NULL / 语义等价坑，未确认前**不得声称等价**，让 verify 的等价门判定。

每条格式：**场景 → 改写 → 为何快 → 等价性 → `[GaussDB]` 特有**。

---

## 1. 相关子查询 → JOIN

- **场景**：`WHERE col IN (SELECT k FROM t2 WHERE t2.x = t1.x)` 或相关 `EXISTS`，计划里子查询逐行求值。
- **改写**：提成 `JOIN`（或半连接 `EXISTS`）。
- **为何快**：优化器可选 Hash/Merge Join，避免逐行探测。
- **等价性 ⚠️**：JOIN 可能放大行数 → 需 `DISTINCT` 或确保连接键唯一；`EXISTS` 是半连接不放大，更安全。

## 2. 标量子查询逐行 → 窗口函数 / 一次 JOIN

- **场景**：`SELECT a, (SELECT max(b) FROM t2 WHERE t2.id=t.id) FROM t`，标量子查询每行执行一次。
- **改写**：`max(b) OVER (PARTITION BY id)` 窗口，或一次性 `LEFT JOIN (SELECT id, max(b) ... GROUP BY id)`。
- **为何快**：从 N 次子查询变一次聚合/JOIN。
- **等价性**：注意无匹配时窗口/JOIN 返回 NULL 与原标量子查询一致；分组键要对齐。

## 3. 谓词 sargable 化（解锁索引，最高频）

- **场景**：`WHERE to_char(order_date,'YYYY-MM-DD')='2024-01-15'`、`upper(email)=...`、`col+0=...`、列与字面量类型不一致。
- **改写**：日期改范围 `order_date >= '2024-01-15' AND order_date < '2024-01-16'`；去掉列上的函数/运算；字面量类型对齐列；必要时建表达式索引。
- **为何快**：谓词变 sargable，CBO 可走列索引（常配合 `--index` 把解锁的列索引一起验）。
- **等价性**：范围改写注意**半开区间**与时区/精度；`upper()=` 去函数需配大小写不敏感索引或确认数据已规整。

## 4. IN / EXISTS / NOT IN 选择 ⚠️

- **场景**：`IN`/`NOT IN` 子查询。
- **改写**：`IN`→`EXISTS` 或 `JOIN`；大表反连接用 `NOT EXISTS` 或 `LEFT JOIN ... WHERE r.k IS NULL`。
- **等价性 ⚠️**：**`NOT IN` 与 `NOT EXISTS` 在子查询列可空时不等价**——`NOT IN` 遇 NULL 返回空集。除非确认列 `NOT NULL` 或补 `IS NOT NULL`，否则不得声称等价，交 verify 判。

## 5. 跨列 OR → UNION ALL ⚠️

- **场景**：`WHERE a = ? OR b = ?`，OR 跨不同列阻断索引，走 Seq Scan。
- **改写**：`SELECT ... WHERE a=? UNION ALL SELECT ... WHERE b=?`，两支各走自己的索引。
- **等价性 ⚠️**：两支可能命中**同一行**（a、b 同时满足）→ `UNION ALL` 会重复。需 `UNION`（去重，但有 sort 成本）或加排他条件。让 verify 的等价门兜底。

## 6. 深分页 OFFSET → 键集分页（keyset / seek）

- **场景**：`ORDER BY id LIMIT 20 OFFSET 100000`，仍扫前 10 万行。
- **改写**：记住上页末键 → `WHERE id > :last_id ORDER BY id LIMIT 20`。
- **为何快**：直接定位，避免扫 + 丢弃。
- **等价性**：语义从「第 N 页」变「某键之后」，**不是任意 OFFSET 的逐行等价**；适合顺序翻页，需调用方配合传 last_id。改 API 形态，按场景采用。

## 7. UNION → UNION ALL ⚠️

- **场景**：`UNION` 做了去重 sort，但业务上不会有重复。
- **改写**：`UNION ALL`，省掉去重 sort。
- **等价性 ⚠️**：仅当两支结果**可证不相交**或重复可接受时等价；否则改变结果。

## 8. COUNT 后分支 → EXISTS

- **场景**：`SELECT count(*) ... ; IF cnt > 0 THEN ...`，为判存在却数全表。
- **改写**：`EXISTS (SELECT 1 ... )` / `SELECT 1 ... LIMIT 1`。
- **为何快**：命中一行即返回，不扫全集。
- **等价性**：仅当原意是「存在性判断」而非真要计数。

## 9. HAVING → WHERE（非聚合谓词前移）

- **场景**：`GROUP BY ... HAVING non_agg_col = ?`，对非聚合列的过滤放在了 HAVING。
- **改写**：把非聚合谓词移到 `WHERE`（聚合前过滤），减少进聚合的行。
- **等价性**：仅非聚合谓词可前移；对聚合结果的过滤（`HAVING sum(...)>?`）必须留在 HAVING。

## 10. 隐式类型转换消除

- **场景**：`WHERE varchar_col = 123`、`bigint_col = '123'` 触发隐式转换、阻断索引。
- **改写**：字面量类型对齐列类型（`= '123'` / `= 123`）。
- **等价性**：等价；注意前导零、空格等字符语义。

## 11. ORDER BY + LIMIT 借索引消除 Sort

- **场景**：`ORDER BY c LIMIT n` 上方出现独立 Sort 节点。
- **改写**：建/利用与 `ORDER BY` 同序的索引，使计划走有序索引直接取前 n 行（Top-N 免排序）。
- **为何快**：消除 Sort、且 LIMIT 早停。
- **等价性**：等价（同序）；属索引候选，走 `## Verified Index Candidates` 验证。

## 12. `[GaussDB]` 分区裁剪触发

- **场景**：分区表谓词被函数包裹或不落在分区键 → 全分区扫（计划无 `Partition Iterator` 裁剪或裁剪到全部分区）。
- **改写**：让谓词直接作用在**分区键**且 sargable（去函数、给确定范围），触发静态/动态分区裁剪。
- **为何快**：只扫相关分区。
- **等价性**：等价（仅改谓词形式）；`[GaussDB]` 计划里确认 `Selected Partitions` 收窄。

## 13. `[GaussDB]` 分布列对齐消除 REDISTRIBUTE / BROADCAST

- **场景**：分布式部署，计划出现大表 `Streaming(type: REDISTRIBUTE)` 或 `BROADCAST`（join 键 ≠ 分布列）。
- **改写**：让 join 键 = **分布列**使 join 下推为本地 join；或小表广播代替大表重分布。
- **为何快**：消除最贵的跨 DN 网络数据移动。
- **等价性**：等价，但**分布列是建表 DDL**，属结构层——归「建议（未验证）」，指出分布列与 join 键不一致，不自动改。

## 14. `[GaussDB]` CTE 物化语义

- **场景**：`WITH cte AS (SELECT ...)` 被单次引用，却被当优化围栏物化，丢失谓词下推。
- **改写**：若版本支持，用 `NOT MATERIALIZED` 让其内联；复用的昂贵 CTE 反而显式 `MATERIALIZED` 物化一次。
- **等价性 ⚠️**：`[GaussDB]` **WITH 物化行为随版本不同**（早期总是物化=围栏；较新版支持 `MATERIALIZED`/`NOT MATERIALIZED` 控制）。改前确认实例版本，并过 verify 看 cost 是否真降。

---

## 配合验证

- 索引类改写（3、11、12）→ 用 `verify.py --auto-index --index '...'` 把解锁的索引一起验。
- 纯改写（1、2、4–10、14）→ `verify.py --original '<替换后>' --rewrite '<改写>'`，看 ACCEPTED + 等价。
- 结构/DDL 类（13 分布列、12 的分区设计）→ 不自动改，归「建议（未验证）」。
- 标 ⚠️ 的 NULL/重复/版本语义，必须由 verify 的等价门确认，不得在报告里直接断言等价。
