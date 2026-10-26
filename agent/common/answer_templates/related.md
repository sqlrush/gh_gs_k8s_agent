### 关联现象清单

| 用户报的现象 | 必查的关联现象 | 用哪个技能 / 维度取 |
|---|---|---|
| 交易超时、接口变慢、整体变慢 | 数据库时间花在哪(CPU / IO / 锁 / 其它等待);锁等待与阻塞链;长事务、空闲事务;Top SQL 有没有新上榜或变慢;连接数与活跃会话 | waitevent、lockwait、health(xact、conn)、topsql |
| 锁等待多、被阻塞 | 根会话在干什么(执行中 / 事务里空闲);长事务;DDL 排在锁队列里;死锁次数;被卡住的 vacuum(死元组涨) | lockwait、health(xact)、vacuum |
| 死锁 | 死锁次数与时间分布;同时段的锁等待;涉及哪些语句(明细在数据库日志里,向 DBA 要日志段) | health / wdr、lockwait |
| 某条 SQL 变慢 | 转 SQL 优化(gaussdb-sqltune);另看这条 SQL 是被锁住还是真在干活(等待占比)、执行计划变没变、统计信息是否陈旧 | sqltune、topsql |
| CPU 高 | 按 CPU 排的 Top SQL;数据库时间里 CPU 的占比;活跃会话数;有没有计划变差的语句 | topsql(按 CPU)、waitevent |
| IO 高、临时文件落盘 | 落盘最多的语句;大排序 / 哈希;checkpoint 与 WAL;表膨胀 | wdr、topsql、health(logs)、vacuum |
| 连接数满、连不上 | 事务里空闲的会话;会话来源分布;锁等待导致会话堆积;长事务 | health(conn、xact)、lockwait |
| 表膨胀、空间涨得快 | 长事务与 xmin 持有;复制槽;未提交的两阶段事务;autovacuum 跟不跟得上 | vacuum、health(xact、repl、storage) |
| 内存高 | 内存上下文分布;会话数;大排序 / 哈希;存储过程 | memanalyze、health(conn) |
| WAL 多、主备延迟 | WAL 生成量;大事务;checkpoint;复制槽 | health(logs、repl) |
