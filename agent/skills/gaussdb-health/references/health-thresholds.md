# 健康检查阈值（默认值与取值理由）

这些是 `health.py` 的确定性发现阈值默认值（`thresholds.py` 常量，OLTP 取向，`--top` 可调列表条数）。**严重度由脚本按这些阈值判定，LLM 不得更改。**

| 维度 / 指标 | NOTICE 🟡关注 | WARN 🟠告警 | CRITICAL 🔴严重 | Code |
|---|---|---|---|---|
| 连接使用率（占 max_connections） | > 80% | > 90% | — | `CONN_HIGH` |
| 缓存命中率 | < 99% | < 95% | — | `CACHE_LOW` |
| 长事务（活动，client 会话） | > 5min | > 30min | > 2h | `XACT_LONG` |
| 空闲事务 IIT | > 5min | > 10min | > 30min | `XACT_IDLE` |
| 死元组 dead_ratio（且 n_dead_tup > 10 万） | > 20% | > 40% | — | `BLOAT_DEAD_RATIO` |
| 阻塞时长 | > 5s | > 30s | > 2min | `LOCK_BLOCKING_CHAIN` |
| 阻塞链深 | — | > 2 | 根阻塞为 IIT 时升一档 | `LOCK_BLOCKING_CHAIN` |
| 等待集中度（单一非空闲等待占比，且总等待 ≥ 5） | > 50% | > 75% | — | `WAIT_CONCENTRATION` |
| LWLock 同锁等待会话数 | ≥ 5 | — | — | `LWLOCK_HOT` |
| checkpoint_req 占比 | > 30% | > 50% | — | `CKPT_PRESSURE` |
| 慢 SQL avg | > 500ms（列入并 Top） | — | — | `SLOWSQL_TOP` |
| 慢 SQL Top1 的 CPU 占比（cpu_s/total_s） | < 10% → 标"非 CPU 消耗主导" | — | — | `SLOWSQL_LOW_CPU` |
| 执行中、还没结束的 SQL（client 会话 active 且不在等锁） | > 5min | > 30min | > 2h | `SLOWSQL_RUNNING` |
| 活跃会话数 | > 20 | > 50 | — | `ACTIVE_HIGH` |
| 活跃集中度（top SQL 占真实客户端活跃，且 ≥5） | > 60% | — | — | `ACTIVE_SQL_HOT` |
| 复制 replay 延迟 | > 16MB | > 64MB | — | `REPL_LAG` |
| 备库状态非 Streaming | — | 非 Streaming | — | `REPL_NOT_STREAMING` |
| 无用索引体积（idx_scan=0） | > 10MB | — | — | `INDEX_UNUSED` |
| 失效索引 | — | > 0 | — | `INDEX_INVALID` |
| 统计陈旧（pg_stats 无统计列 / 冻结页数为 0 / 冻结与实时页数偏离 > 10%；候选：冻结行数 > 1 万或实时页数 > 128 页） | 命中 | — | — | `STALE_STATS` |
| 死锁累计 | > 0 | — | — | `DEADLOCKS` |
| 事务回滚率（且总事务 > 1000） | > 10% | — | — | `ROLLBACK_HIGH` |
| 悬挂 2PC | — | > 0 | — | `PREPARED_XACT` |

## 取值理由

- **缓存命中率 99%/95%**：OLTP 工作集通常应几乎全在 buffer 命中；< 99% 值得关注，< 95% 多有热数据放不下或大批顺序扫。
- **长事务 5/30min/2h**：> 5min 已可能影响 vacuum 与锁；> 2h 在 OLTP 几乎一定是异常（卡住的事务）。
- **空闲事务 IIT 5/10/30min**：IIT 既持锁又卡 vacuum，比同时长的活动事务更危险，故阈值更紧，且阻塞链里根阻塞为 IIT 时严重度升级。
- **死元组 20%/40% + 绝对量 > 10 万**：比例阈值配绝对量下限，避免把几十行的小表（高比例但无影响）报成问题。
- **阻塞链深 > 2**：两级以上的等待链说明争用在扩散，即使单点时长不长也升 WARN。

## 已知口径与边界

- **长/空闲事务、最老事务、活跃集中度、执行中的长 SQL 只统计真实客户端会话**（`client_port` 非空）。GaussDB 内部后台线程（WLM 的 workload、WDRSnapshot、Asp、JobScheduler 等，有的长期 active 且带事务开始时间）的 `client_port` 为 NULL，据此排除（否则 WLM 会被误报为 35h 长事务）；真实客户端 TCP 是端口号、本地 Unix socket 是 -1，都纳入检查。**不再按 `connection_info` 非空认客户端**：只有 openGauss 自带的 libpq（gsql 等）会填这一列，psycopg2 这类社区 libpq 系驱动、不少中间件不填——2026-10-24 faultlab 实测，这类客户端的长事务 / 空闲事务因此整类看不见。
- **执行中的长 SQL（`SLOWSQL_RUNNING`）**：慢 SQL 表读的 `dbe_perf.statement` 只记已完成的执行，还在跑的语句不在里面；这一项从 `pg_stat_activity` 看此刻 active 的语句已执行多久。**在等锁的不算**（`waiting=true`，耗时花在锁上，看锁阻塞链），只在标题里点数。
- **连接状态分布只数 `sessionid <> 0` 的会话**：开线程池时 `pg_stat_activity` 里有 `sessionid = 0` 的空闲工作线程条目，不是会话。
- **归档失败计数在 PG 9.2 内核 GaussDB 不可得**（无 `pg_stat_archiver`）；该维仅报 `archive_mode` 与 checkpoint 压力。
- 阈值为默认值，不同业务（批处理 vs 在线）可调；调整 `thresholds.py` 时连同本文件一起更新，保持"非拍脑袋"。
