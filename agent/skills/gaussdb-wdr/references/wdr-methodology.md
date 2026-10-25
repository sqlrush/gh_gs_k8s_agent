# WDR 解读方法论（OpenGauss/GaussDB）

## 总则
WDR = 两个快照之间的工作负载差值。先看 `## Report Window` 确认窗口时长合理（过短噪声大、过长稀释峰值）。`## Deterministic Findings` 是 ground truth，逐条锚定。

## 逐维度
- **Load Profile**：DB time = 窗口内所有 SQL elapsed（墙钟耗时）之和。**务必同看 CPU time 与"CPU占DBtime%"**：CPU% 高 → 真在算（CPU/排序）；CPU% 极低（如 0%）→ DB time 几乎全是**等待/锁/IO/睡眠**，"忙"是假象，别把高 DB time 当成高资源消耗。物理读占比高 → 缓存/索引问题。
- **Database Stat**：回滚率高 → 应用异常/约束冲突；死锁 ≥1 必查；临时文件溢出 → work_mem 不足或缺索引致大排序/hash。
- **Top SQL（多维度）**：表带 `elapsed_s`/`cpu_s`/`spill_MB`/`物理读`/`calls` 多列，且有 **「各维度元凶」行** —— **每类资源的冠军 SQL 往往不是同一条**（典型：DB time/CPU/溢出冠军是大排序查询，而**物理读冠军常是另一条按 elapsed 排名靠后、单维榜单上看不到的 SQL**）。归因时**按问题类型去对应冠军找元凶**，别只盯 elapsed 第一名。占 DB time 最高者也**未必**是资源元凶——**先看它的 `cpu_s`（是否真在算）与 `spill_MB`（是否它在制造临时溢出）**。⚠ **DB time 陷阱（必读）**：`elapsed` 按墙钟计，`pg_sleep`/被锁阻塞/空等的语句会**以巨大 elapsed 霸占 DB time 却消耗 ~0 CPU**，而真正烧 CPU/盘的查询在资源被打满时反被饿死、计入的 elapsed 反而小。若某 SQL 触发 `WDR_TOPSQL_LOW_CPU`（CPU 占其耗时 <10%），它是**阻塞/睡眠症状不是元凶**：根因转去 **CPU time 榜 + 等待事件（LOCK/IO） + 临时溢出/死锁**等维度，**不要**对它建索引或当作压垮库的原因。带 sql_id 的索引/改写建议**必须经 gaussdb-sqltune 实证后**才给（睡眠/阻塞类查询通常无法靠执行计划优化）。GaussDB 自己的系统 SQL（`dbe_perf.*` 监控查询、WDR 快照、诊断工具查系统视图）**已从 Top SQL 与各维度元凶里排除**，标题行末尾写着「已排除 N 条内核系统 SQL（…合计 X 秒）」：要转告；合计秒数占窗口 DB time 很高时，说明监控 / 诊断查询本身是负载的一部分，可以点出来，但不要把它们当应用元凶。
- **Wait Events / Classes**：等待类倾斜指向瓶颈子系统——IO（缺索引/慢盘）、LOCK（锁竞争）、LWLOCK（缓冲/WAL 争用）。与 Top SQL、Cache 交叉印证。
  「主要事件」列给出每类里耗时最多的几个事件，结论**落到事件名**：LWLOCK 里是 `WALFlushWait` / `WALInsertLock` 就是 WAL（提交太频繁 / 日志盘慢），
  是 `BufferContentLock` 就是热点页，是 `LockMgrLock` 就是锁管理器争用（如大量分区）——不要只说「轻量锁高」。
- **Checkpoint/Redo**：强制 checkpoint 占比高 → WAL/shared_buffers 配置或写入风暴。
- **Cache/Memory**：物理读 Top 对象 = 索引/缓存候选；命中率本身在 Database Stat。
- **File IO**：物理读写 Top 文件（以 db/spc/filenum 标识），定位热点表空间/盘。

## 跨维关联（典型链）
- Top SQL 占 DB time 高 + Cache 物理读 Top 同一对象 + Wait 类 IO 倾斜 → **缺索引致全表扫**，三者互证，根因单一。
- 临时文件溢出 + Top SQL 大排序 → 缺支持 ORDER BY/GROUP BY 的索引或 work_mem 偏小。
- **`WDR_TOPSQL_LOW_CPU`（pg_sleep/阻塞霸占 DB time）+ LOCK 等待类 + 死锁** → 行锁竞争/连接持锁，根因在并发与事务边界（谁在持锁/睡眠），**不在那条 SQL 的计划**；同窗口的 temp 溢出/IO 是被它旁边真在跑的大查询造成的，二者元凶不同，别混为一谈。

## 问题归因纪律（每条发现 → 引发请求 → 如何优化）
**报告不能只列问题。** 每条发现必须闭环到「哪些请求引发 + 这些请求怎么优化」，否则只是诊断、不可落地。按问题类型定位元凶请求：

| 问题（发现） | 元凶请求从哪定位（证据包列） | 怎么优化该请求 |
|---|---|---|
| `WDR_TEMP_SPILL` 临时溢出 | Top SQL **`spill_MB` 列**最高者（确定性：谁溢出多少一目了然） | 经 sqltune 实证：为 ORDER BY/GROUP BY 建索引免大排序、改写减少 hash/sort、或评估 work_mem |
| `WDR_TOPSQL_DBTIME` 吃 DB time | Top SQL `elapsed_s`/`cpu_s` 最高者 | CPU 高且可优化 → sqltune 索引/改写；CPU 低（LOW_CPU）→ 见下"阻塞"行 |
| IO 等待倾斜（`WDR_WAIT_CLASS_SKEW`=IO） | Top SQL 的 **`物理读`冠军**（见「各维度元凶」行，常非 elapsed 第一名）+ Cache/File IO 的热点对象/文件 | 缺索引致全表扫 → sqltune 建索引；非 sargable 谓词(模运算/函数包列)→ 改写而非建索引；热表/盘 → 评估表空间/缓存配置 |
| LOCK 等待倾斜 + `WDR_DEADLOCK` | 锁受害者：Top SQL 里 **`cpu_s`≈0 但 `elapsed` 高**的被阻塞语句。死锁对：**WDR 取不到，只能从同表 DML 反推候选，必须标明是候选**（实测有三方环被反推成「两条在抢」）；权威来源是数据库日志里的 `deadlock detected` 段 | **不是 SQL 计划问题**：缩短事务、统一加锁顺序、加重试、收敛持锁/睡眠时间（持锁者即 LOW_CPU 那条） |
| `WDR_ROLLBACK_RATIO` 回滚率 | 触发死锁/约束冲突的 DML | 应用侧处理冲突、避开热点行竞争 |

要点：① temp/IO/锁三类问题的**元凶请求往往不是同一条**（如高并发压测：temp 溢出=大排序聚合查询、锁等待=被阻塞的 UPDATE、CPU=同一聚合查询），**别一锅烩归给同一条 SQL**；② 每条带 sql_id 的优化建议**先用 sqltune 实证**（`python3 {baseDir}/../sqltune/scripts/sqltune.py -c <conn> <sql_id>`）再写进报告；睡眠/阻塞类请求通常无法靠执行计划优化，给事务/并发层建议。

## GaussDB/WDR 专属
- scope/node：单机用 node + `pgxc_node_name`；cluster scope 与分布式 CN/DN 在 lite 上未验。
- 计数器重置：若窗口内统计被 reset，delta 失真——该维度会降级标注，勿臆测。
- lite 版可能缺部分快照视图——降级是正确行为，如实说明。
- 快照视图列名随版本而异（5.0.3 上数据列统一带 `snap_` 前缀；checkpoint 计数在 `snap_global_bgwriter_stat`）。
