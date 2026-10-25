# 存储过程调优方法论（OpenGauss/GaussDB）

本文是 `gaussdb-proctune` 工作流第 5 步加载的检查清单。对照 `proctune.py collect` / `proctune.py tune-cursor` 的证据各节逐项核对。

每条反模式按统一格式：**识别 → 根因 → 改法 → 验证 → 风险**。
「验证」一栏标明该改法在当前版本能否经脚本自动验证：

- **可验证（游标 SELECT）**：改法落在只读游标的 SELECT 上，走 `verify.py`（cost + 等价）+ `## Verified Index Candidates`。
- **只建议**：涉及写逻辑或过程结构，第一版只给循证建议，落地前需人工或测试实例确认。

## 检查清单总览

对照证据，依次核对：

1. `## Verified Index Candidates`：每个游标 SELECT 是否有已验证索引收益。
2. `## Execution Plan`：游标 SELECT 计划里是否有 Seq Scan、函数包裹谓词、Nested Loop 内层全表扫。
3. `## Structural Findings`：循环内 SQL、逐行 DML、循环内 EXCEPTION、动态 SQL、循环内不变查询。
4. `## Runtime Attribution`：哪条 embedded SQL / 哪个游标真正耗时（优先优化它，而非看着可疑就改）。
5. `## Skipped Cursors`：不可自动改写的游标，转为建议。

## 一、游标 SELECT 缺索引

- **识别**：`## Execution Plan` 里游标 SELECT 出现大 cost 的 Seq Scan；`## Verified Index Candidates` 给出候选。
- **根因**：游标驱动查询的过滤/连接列无可用索引。
- **改法**：按 `## Verified Index Candidates` 建索引。
- **验证**：**可验证（游标 SELECT）**——hypopg 假设索引实测 cost。
- **风险**：CREATE INDEX 锁与构建时间（大表用 CONCURRENTLY）、写放大。

## 二、游标 SELECT 谓词被函数包裹（阻断索引）

- **识别**：计划里 `Filter: func(col) = ...`（如 `to_char(d,'YYYY-MM-DD')=...`、`upper(c)=...`、隐式类型转换）导致列上的索引用不上。
- **根因**：列被包在函数里，优化器无法走该列索引。
- **改法**：改写成可走索引的形式——日期改范围条件、避免对列做函数/隐式转换、必要时配表达式索引。
- **验证**：**可验证（游标 SELECT）**——改写后 `verify.py`（cost ≥ 1.3× + 等价），常需配合 `--index` 把解锁的列索引一起验。
- **风险**：改写须保持游标输出列名；范围改写注意边界（半开区间）。

## 三、游标 SELECT 可改写为 JOIN / 窗口

- **识别**：游标 SELECT 含相关子查询、`SELECT ... INTO` 的逐行取值意图、可合并的标量子查询。
- **根因**：相关子查询逐行求值、计划退化。
- **改法**：相关子查询改 JOIN；多次标量子查询改窗口函数或一次 JOIN。
- **验证**：**可验证（游标 SELECT）**——`verify.py` cost + 等价；注意改写后列集合不变。
- **风险**：JOIN 改写可能改变重复行/NULL 语义，靠等价校验兜底，仍需人工复核列契约。

## 四、row-by-row：循环里逐行跑 SQL

- **识别**：`## Structural Findings` 标 `loop` 内含 `query`；`## Runtime Attribution` 显示该 SQL calls 极高、单次便宜但总量大。
- **根因**：本可一次集合操作的逻辑被拆成每行一次往返，过程引擎与 SQL 引擎反复上下文切换。
- **改法**：改成单条集合化 SQL（JOIN / 集合 UPDATE/INSERT），消除循环。
- **验证**：**只建议**（涉及结构与写逻辑）。落地前在测试实例验数据效果一致 + 计时。
- **风险**：语义可能依赖循环顺序或中间状态；异常/事务边界变化；高风险，需充分测试。

## 五、逐行 DML（循环内 INSERT/UPDATE/DELETE）

- **识别**：`## Structural Findings` 标循环内 per-row DML。
- **根因**：每行一条 DML，WAL/锁/往返成本随行数线性放大。
- **改法**：合并为单条集合 DML，或批量（A 兼容下 `FORALL`）。
- **验证**：**只建议**。v2 可用「只读投影」验数据效果（把净效果表达为只读 SELECT 比 md5，零 WAL）。
- **风险**：触发器、约束、错误处理粒度变化；高风险。

## 六、显式游标 fetch 循环

- **识别**：`OPEN ... FETCH ... LOOP` 取行后逐行处理。
- **根因**：游标遍历 + 过程逻辑，本可下推到 SQL。
- **改法**：把处理逻辑并入一条集合 SQL；批量场景用 `BULK COLLECT` / `FORALL`（A 兼容）。
- **验证**：**只建议**（游标本身的 SELECT 若只读，可单独走第二条「谓词/索引」路径验证）。
- **风险**：同四、五。

## 七、循环内 EXCEPTION 块

- **识别**：循环体内含 `BEGIN ... EXCEPTION ... END`。
- **根因**：openGauss/PG 中每个含 EXCEPTION 的块进入时建子事务（savepoint），循环内逐行建/释放 savepoint 成本很高。
- **改法**：把异常处理提到循环外；或改集合操作后整体处理；确实需要逐行容错时评估批大小。
- **验证**：**只建议**。
- **风险**：错误处理语义变化（原本逐行吞错，改后可能整批失败）；中高风险，须确认业务可接受。

## 八、动态 SQL（EXECUTE）

- **识别**：`## Structural Findings` 标 `EXECUTE 'SELECT/...'`；尤其循环内。
- **根因**：动态 SQL 每次重解析、不吃计划缓存；拼串还有注入面。
- **改法**：能静态化就静态化；必须动态时用 `EXECUTE ... USING` 传参（吃计划缓存、避免注入）。
- **验证**：**只建议**（动态游标的 SELECT 文本静态不可知，归 `## Skipped Cursors`）。
- **风险**：静态化可能损失灵活性；须保证参数化覆盖所有分支。

## 九、循环内不变查询（可外提）

- **识别**：循环体内某查询的结果不随循环变量变化。
- **根因**：每次迭代重复同一查询。
- **改法**：外提到循环前求值一次存入变量；或并入驱动查询的 JOIN。
- **验证**：**只建议**（若该查询是只读游标 SELECT，可单独验证其本身的索引/改写）。
- **风险**：低（语义通常等价），仍需确认查询确实不变。

## 十、VOLATILE 函数误标 / 谓词里的易变函数

- **识别**：谓词里调用 VOLATILE 函数；或函数本可 STABLE/IMMUTABLE 却标了 VOLATILE。
- **根因**：VOLATILE 函数逐行重算、且阻断某些优化与索引。
- **改法**：纠正 volatility 标注；把不变计算外提。
- **验证**：**只建议**（改 volatility 是 DDL 层改动，需人工评估正确性）。
- **风险**：错标 IMMUTABLE 会导致结果缓存错误；必须确认函数确为对应级别。

---

## GaussDB 存储过程深度要素（量化与特有）

上面是反模式清单；本节是 GaussDB 特有的量化知识，支撑「建议（未验证）」层——这一层没有 `verify.py` 兜底，知识准确度直接决定建议质量。

### A. 批量与集合化原语（反模式四/五/六的改法量化）

- **原生 PL/pgSQL**：无 `FORALL`。改法是集合化 SQL（一条 `INSERT/UPDATE ... FROM/JOIN` 替代循环），或 `unnest(array)` 批处理。
- **`[GaussDB]` A 兼容（Oracle 模式）**：
  - `SELECT ... BULK COLLECT INTO coll [LIMIT n]`：批量取行替代逐行 `FETCH`；`LIMIT n` 分块防集合变量吃爆内存。
  - `FORALL i IN coll.FIRST..coll.LAST INSERT/UPDATE/DELETE ...`：一批绑定一次性提交给一条 DML，替代循环内逐行 DML。
- **收益量级**：逐行 → 批量/集合化，往返与过程引擎↔SQL 引擎上下文切换从 N 次降到 1 次（或 N/chunk）。实测常达**数量级（10×+）**，行数越多越显著。但属写逻辑改动，**只建议**，落地在测试实例验。
- **等价 / 事务注意**：`FORALL` 是单条 DML 语义，逐行错误处理粒度变化（需 `SAVE EXCEPTIONS` 收集逐行异常）；集合化改写要保证 `WHERE/SET` 逻辑与原循环逐行结果一致（触发器、约束、计算顺序）。

### B. 过程内的成本要素：子事务 / 计划缓存 / 并行

- **子事务 / undo（对应反模式七）**：每个含 `EXCEPTION` 的块进入时建子事务（savepoint）。`[GaussDB]` Astore 回滚到 savepoint 丢弃新行版本（留死元组）；Ustore 走 undo 段。**循环内逐行建/释放 savepoint，成本随行数线性放大**，是高频循环里的隐形热点。改法：异常处理移出循环，或集合操作后整体容错。
- **`[GaussDB]` 计划缓存 generic vs custom**：过程内**静态** SQL 预编译缓存计划。openGauss/GaussDB 先按当次参数生成 custom plan，多次执行后可能切 generic plan（参数无关）。**参数分布倾斜的过程里 generic plan 可能用"平均"选择性而选错**——信号是「同一过程对不同入参性能忽好忽坏」。处置：必要时（版本支持）`plan_cache_mode` 强制 custom，或拆分。动态 SQL（`EXECUTE`）不吃缓存、每次重解析。
- **`[GaussDB]` Streaming / query_dop**：循环改成集合化 SQL 后，大数据量可能被 SMP 并行（计划出现 `Streaming`），`query_dop` 控并行度。**并行是否在过程内生效与 GUC/版本有关**，且 Streaming 的重分布/广播本身有成本——集合化收益要在测试实例实测，不能假设一定更快。

### C. `[GaussDB]` 存储引擎：Ustore vs Astore（更新密集过程）

- 更新密集的过程（循环 `UPDATE`、状态机推进、批量改状态）：**Astore** 每次更新产生新版本 + 死元组，过程跑完留大量待 VACUUM 的膨胀；长事务还顶住 xmin、拖全库 VACUUM。
- **Ustore** 原地更新 + undo，更新密集场景膨胀显著更小。
- **建议（未验证、DDL 层）**：诊断「过程跑完表膨胀大 / 越跑越慢」且表是 Astore + 更新密集时，可评估迁 Ustore。需人工评估其权衡（如 undo 空间、长事务影响），不自动改。

### D. 验证可行性：rollback-safe 决定能验到什么程度

能否对过程做整体计时/等价验证，取决于它是否 **rollback-safe**（`## Procedure Source` 已给该布尔）：

- 含内部 `COMMIT`/`ROLLBACK`（GaussDB 过程支持），或 `[GaussDB]` A 兼容 `PRAGMA AUTONOMOUS_TRANSACTION` → **不可回滚**，事务沙箱验证失效。
- 含 `nextval` 序列、`dblink`、`NOTIFY`、外部调用 → 非事务副作用，回滚也泄漏。

处置：不可回滚的过程，整体改写**只能逐语句验**（其中只读游标 SELECT 走 `verify.py`），并明确标注「整体等价需人工 / 测试实例确认」，**绝不假装验过**。这与 sqltune 的验证纪律一致：拿不到确定性背书的，归未验证分区。
