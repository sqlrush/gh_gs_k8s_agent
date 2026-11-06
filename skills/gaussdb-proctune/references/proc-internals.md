# OpenGauss/GaussDB 存储过程内幕

本文为 `gaussdb-proctune` 在分析时按需查阅的背景知识，解释证据里几个机制为什么这么呈现、改法为什么这么定。

## 一、两种过程语言：PL/pgSQL 与 PL/SQL（A 兼容）

OpenGauss 有两种兼容模式，存储过程语言随之不同：

- **PL/pgSQL**：PostgreSQL 原生过程语言。游标声明常见 `DECLARE c CURSOR FOR SELECT ...`、`FOR rec IN (SELECT ...) LOOP`、`OPEN c FOR SELECT ...`。
- **PL/SQL（A 兼容，Oracle 模式）**：`CURSOR c IS SELECT ...`、包（package）内过程与游标、`%ROWTYPE`、`FORALL`、`BULK COLLECT`、参数化游标 `CURSOR c(p int) IS ...`、REF CURSOR。

`proctune.py collect` 的 `## Procedure Source` 标出 `lang`。v1 的 `ExtractCursors` 覆盖两种模式的**基础游标**（`CURSOR c IS SELECT` / `FOR rec IN (SELECT)` / `OPEN c FOR SELECT` / `DECLARE c CURSOR FOR SELECT`）；参数化游标、包内游标、REF CURSOR 归 `## Skipped Cursors`（v2 再支持）。

## 二、嵌套语句统计：track_stmt_stat_level

过程体内执行的 SQL 是「嵌套语句」。`dbe_perf.statement` 默认主要记录顶层语句；要把过程内部每条 SQL 的 calls/avg/total 也采下来，需要把 `track_stmt_stat_level` 调到捕获嵌套层级（形如 `'OFF,L1'` 之类，第二段控制完整 SQL 记录级别，具体取值见实例文档）。

`## Runtime Attribution` 就是按 embedded SQL 的 unique_sql_id 关联这些统计。**未开启时，gaussdb-proctune 优雅降级为纯静态分析并在该节标注**——此时「哪条最耗时」只能靠结构推断，建议提示 DBA 开启后复采以获得真实耗时排序。

## 三、子事务 / savepoint 成本（EXCEPTION 块）

PL/pgSQL/PL/SQL 中，**每个含 `EXCEPTION` 子句的块在进入时建立一个子事务（内部 savepoint）**，退出时释放。单次成本不高，但**放进循环逐行建立/释放**时会显著累积，并消耗子事务计数资源。这就是方法论「循环内 EXCEPTION」条目的根因。改法是把异常处理移出循环，或改集合操作后整体容错。

## 四、计划缓存：generic vs custom plan

过程内的静态 SQL 会被预编译并缓存计划。OpenGauss 在多次执行后可能从 custom plan（按当次参数生成）切到 generic plan（参数无关）。参数分布倾斜时，generic plan 可能不优。要点：

- **静态 SQL 吃计划缓存**；**动态 SQL（EXECUTE）每次重解析、不吃缓存**——这是「动态 SQL」反模式的核心代价。
- 必须动态时用 `EXECUTE ... USING` 传参，既利于缓存也避免拼串注入。

## 五、FORALL / BULK COLLECT（A 兼容）

A 兼容模式提供批量原语：

- `BULK COLLECT INTO`：一次把结果集批量取进集合变量，替代逐行 FETCH。
- `FORALL`：把一批绑定变量一次性提交给一条 DML，替代循环内逐行 DML。

它们是「逐行 DML / 游标 fetch 循环」反模式在 A 兼容下的常见改法，但仍属**只建议**（写逻辑），落地需测试实例验证。原生 PL/pgSQL 无 FORALL，对应改法是集合化 SQL。

## 六、游标 FOR UPDATE 与 WHERE CURRENT OF

`CURSOR c IS SELECT ... FOR UPDATE` 会对选中行加锁，通常配合后续 `UPDATE ... WHERE CURRENT OF c` 做定位更新。**它不是纯只读游标**——SELECT 与后续更新强耦合，改 SELECT 会破坏 `WHERE CURRENT OF` 的定位语义。因此这类游标归 `## Skipped Cursors`，只给建议（如评估能否整体改集合 UPDATE），不自动改写。

## 七、为什么游标 SELECT 能安全验证、写逻辑不能

游标的 SELECT 是过程体内少数**天然只读**的部分：

- 成本判断走 `EXPLAIN`（不执行），零副作用；
- 等价判断走 md5 行哈希（只读、有界）；
- 不涉及 WAL、回滚沙箱、锁。

而过程的写逻辑（DML、结构）几乎都改数据，验证其等价必须执行，会产生 WAL、锁、死元组——即便事务回滚，WAL 也已写出、锁与膨胀照旧。这是 v1 把自动改写限定在游标 SELECT、写逻辑只建议的根本原因。v2 才用「只读投影优先、沙箱执行设预算门、默认测试实例」去扩展写逻辑改写。
