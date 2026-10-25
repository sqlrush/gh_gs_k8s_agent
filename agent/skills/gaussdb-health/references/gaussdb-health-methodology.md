# GaussDB 健康检查方法论

逐维度解读清单 + 跨维度关联。每维都对照 `health.py` 证据包里对应的 `## <Dimension>` 小节与 `## Deterministic Findings`。**每个结论都要落到一个真实数字上。**

## 逐维度清单

### Overview（总览）
- 缓存命中率 < 99% 关注、< 95% 告警：OLTP 下命中率低意味着热数据放不下或大批顺序扫；结合 Slow SQL 看是不是某些大查询在冲刷 buffer。
- 连接使用率 > 80%/90%：接近 max_connections，关注连接池配置与泄漏（看 Connections 维 idle/IIT）。
- `in_recovery=true`：实例处于恢复/备机角色，部分写类指标含义不同。

### Wait Events（等待事件）
- 数据源 `pg_thread_wait_status`（GaussDB 细分等待模型，通用 PG 无）。看「非空闲」等待的集中度。
- 集中在 `acquire lock` 类 → 转 Transaction Locks & Blocking Chains 维找根阻塞。
- 集中在 IO 类 → 结合 checkpoint/慢 SQL 看是否大量物理读。
- 集中在 lwlock 类 → 转 LWLock 维。

### Slow SQL（慢 SQL）
- 来自 `dbe_perf.statement`。gaussdb-health 只做发现与导流，**不在此深调**——对具体 SQL 用 `/gaussdb-sqltune`，对存储过程用 `/gaussdb-proctune`。
- 关注 avg 高 × calls 多（总耗时大）的条目。
- **辨别"假慢"——`cpu_s` 列是关键判据**：慢 SQL 表带 `cpu_s`（该语句累计 CPU 秒）。若 Top1 的 `cpu_s / total_s` 极低（<10%），脚本会产 `SLOWSQL_LOW_CPU` 确定性发现——它的确定性结论是**这条不是 CPU/缺索引类资源问题**：耗时花在了等待（锁/睡眠）或 I/O（物理读/排序溢出）上，而非计算。典型：小表上的单行 `UPDATE/DELETE ... WHERE pk=?` avg 极大（几十秒~几小时），是**锁等待累积**进了 `dbe_perf.statement`（其耗时含锁等待、且为累计值，一次长阻塞会永久抬高 avg），**不是缺索引**。
- **执行中、还没结束的 SQL（`SLOWSQL_RUNNING`）**：上面那张表只有**已完成**的执行，一条跑了二十分钟还没完的 SQL 不在表里。这一项来自 `pg_stat_activity`：客户端会话 active、不在等锁、已执行超过阈值。证据里带 pid 与 SQL 摘要 —— 看它此刻的计划用 `/gaussdb-explain --pid <pid>`，深调用 `/gaussdb-sqltune`。在等锁的长语句只在标题里点数（「另有 N 条…在等锁」），去 Locks 维找根阻塞，**不要**导去调 SQL。同一会话通常也有一条 `XACT_LONG`：那条说它持着快照多久（影响 vacuum），这条说语句还没跑完（影响响应），两条都要处理，不是重复。
- **据此分流**：`SLOWSQL_LOW_CPU` 命中（cpu_s≈0，或 EXPLAIN cost 极低却 avg 极高）→ 去 **Locks / Xact / Wait Events** 维查锁与等待，必要时看临时文件/排序，**别盲目导流 `/gaussdb-sqltune` 建索引**；只有 `cpu_s` 占比高（真 CPU 消耗）的慢 SQL 才适合 `/gaussdb-sqltune` 深调。

### Long & Idle Transactions（长事务与空闲事务）
- 只看真实客户端会话（`client_port` 非空，已排除 WLM 等 GaussDB 内部后台线程；TCP 与本地 socket 客户端都纳入，不论驱动填不填 `connection_info`）。
- 活动长事务（XACT_LONG）：长时间 active 的事务持有快照，阻止 vacuum 回收旧版本 → 关联 Dead Tuples & Bloat。
- 空闲事务（XACT_IDLE，idle in transaction）：开了事务不提交，**既持锁又卡 vacuum**，是阻塞链与膨胀的常见根因 → 关联 Locks 与 Bloat。多由应用连接池泄漏 / 漏 commit / `idle_in_transaction_session_timeout` 未设导致。

### Dead Tuples & Bloat（死元组与膨胀）
- `pg_stat_user_tables`，dead_ratio 高且 n_dead_tup 大（> 10 万）才计，避免小表噪声。
- Astore（默认堆）靠 vacuum 回收死元组；Ustore（in-place 更新）膨胀语义不同，回滚段/旧版本压力点不一样。
- **先看证据里的 `autovacuum=on/off`（采集器已在发现证据与表里标出）**：
  - `autovacuum=off` → 膨胀真因是该表 autovacuum 被关闭（或全局关闭），**与长事务无关**；建议开启 autovacuum 或人工 `VACUUM`，**不要**归因于"长事务卡 vacuum"。
  - `autovacuum=on` 且 dead 高 + last_autovacuum 久未跑 → 才考虑是否被长事务/IIT 的旧快照卡住：看 Overview 的"最老事务"年龄，足够大且死元组晚于其快照时该归因才成立；先处置那个事务，膨胀常自愈一部分。

### Lightweight Locks (LWLock)（轻量锁）
- `pg_thread_wait_status` 里 lwlock 类等待。同一 lwlock ≥ 5 会话持续等待为热点。
- WALWriteLock 热 → WAL 写瓶颈（结合 checkpoint）；buffer mapping / clog 热 → 缓冲区或事务状态争用。

### Transaction Locks & Blocking Chains（事务锁与阻塞链）
- `pg_locks` 自连接递归构链（GaussDB 无 `pg_blocking_pids()`）。看**根阻塞者**、链深、被阻会话数、阻塞时长。
- 根阻塞者若是 `idle in transaction` → 严重度升级：这是最典型、最该先处置的场景（解一个根 IIT 往往解开整条链）。
- 处置建议：业务确认根阻塞 pid 可中断后由人工 `SELECT pg_terminate_backend(<pid>);`；并排查应用事务边界。**gaussdb-health 绝不自动 kill。**

### Connections（连接）
- `pg_stat_activity` 按 state 分布，只数 `sessionid <> 0` 的会话（开线程池时 `sessionid = 0` 的是空闲工作线程条目，不是会话）。此维为上下文证据，本身不产严重度发现（连接使用率的严重度在 Overview，长/空闲事务在 Xact）。
- idle 堆积 → 连接池过大或未回收；IIT 计数高 → 与 Xact/Locks 维交叉印证。

### Checkpoint / WAL / Archiving（日志类纯 SQL 信号）
- `pg_stat_bgwriter`：checkpoints_req 占比高（> 30%/50%）= checkpoint 被频繁强制触发（WAL 涨太快 / `checkpoint_segments` 偏小），关注 IO 抖动。
- `archive_mode`：来自 `pg_settings`。注：PG 9.2 内核的 GaussDB 无 `pg_stat_archiver`，归档失败计数不可得，本维只报开关与 checkpoint 压力。

### Replication / Standby（复制 / 主备）
- `pg_stat_replication`（主库视角）：每个 walsender 的 state、sync_state、replay 延迟字节（sent vs replay）。
- `REPL_NOT_STREAMING`：备库状态非 Streaming（Catchup/Startup/Down）→ 复制未跟上或断连。
- `REPL_LAG`：replay 延迟字节超阈值 → 备库落后，故障切换有丢数据风险。
- 检测到备库才出；单机或本节点是备库时自降级为"无下游备库"。备库角色已在 Overview 的 `in_recovery=true` 体现。

### Schema / Objects（对象 / Schema 健康）
- `INDEX_UNUSED`：`idx_scan=0` 且体积大的索引——白占空间、拖慢写入；删前**确认不是低频/周期性查询所用**。（已排除主键/唯一约束索引——它们 idx_scan=0 正常但不可删）。
- `INDEX_INVALID`：`indisvalid=false`——建索引失败遗留，需重建或删除。
- `STALE_STATS`：`pg_stats` 里没有统计列（从没被 ANALYZE 覆盖）、冻结页数 `relpages` 为 0（没有基准），或冻结页数与实时页数偏离超过 10%（表在上次 ANALYZE 之后长大了）→ 执行计划可能劣化，建议 ANALYZE。判据只用不会被 `pg_stat_reset()` 清掉的信号，`last_analyze` 只作参考。候选表先排「没统计 / 没基准」的，再按偏离比例、体积排，从没 ANALYZE 过的大表不会被截掉。已排除 GaussDB 内部 schema。

### Transactions / Concurrency（事务 / 并发）
- `DEADLOCKS`：`pg_stat_database.deadlocks` 累计>0——曾发生死锁，查应用加锁顺序。
- `ROLLBACK_HIGH`：回滚率 rollback/(commit+rollback) 超阈值——大量事务失败/异常，查错误日志。
- `PREPARED_XACT`：悬挂的两阶段事务（`pg_prepared_xacts`）——未提交/回滚的 2PC 长期持锁卡 vacuum，需处置。

### 关于 XID 回卷（GaussDB 专属：基本无此风险）
- **GaussDB/openGauss 用 64 位事务 ID**（`xid` 8 字节），不存在 PG 32 位 XID 回卷导致数据库强制只读/停机的灾难，故 gaussdb-health **不做"XID 回卷"采集器**——那是 generic-PG 的检查，套在 GaussDB 上是误导。freeze/vacuum 健康通过膨胀维度与 autovacuum 状态间接体现。

## 跨维度关联（健康检查的价值所在）

- **IIT → 阻塞链 + 膨胀**：一个 `idle in transaction` 会话同时出现在 Xact（XACT_IDLE）、Locks（根阻塞）、并可能间接抬高 Bloat（旧快照卡 vacuum）。**膨胀这条关联有前提守卫**：仅当目标表 `autovacuum=on` 且 Overview 的"最老事务"年龄足够大时，膨胀才归因于该 IIT；若 `autovacuum=off`，膨胀是**独立问题**（autovacuum 关了），不要硬串成一条因果。满足前提时把阻塞+膨胀点成**单一根因**，而不是孤立问题。
- **WALWriteLock 热 + checkpoint_req 高**：指向 WAL/落盘压力，而非单纯锁问题。
- **缓存命中率低 + Slow SQL 大表扫**：互相印证 buffer 不足或缺索引，导流 `/sqltune`。

## 证据锚定校验（复述要点）

报告里每条结论必须能在 `## Deterministic Findings` 找到对应 Code 或在原始小节找到对应数字；WARN/CRITICAL 发现一条都不能漏；严重度以脚本确定性带为准；总体状态 = 最重发现，不得下调。
