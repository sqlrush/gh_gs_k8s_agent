#!/usr/bin/env python3
"""生成示例知识库 sample-kb/:27 份案例 + 12 条规范条款 + 六类类型化条目 + 三元组 + 别名表 + 黄金查询集。

这是「导入前 / 导入后 / 删库回退」三态对照的语料,也是客户试用时的样板。全部虚构,
但现象、code、对象名对准各诊断 skill 真会报出来的东西(INDEX_UNUSED / CACHE_LOW /
WAIT_LWLOCK_HEAVY / DBTIME_CPU_HEAVY / DEADLOCKS / STALE_STATS / LOCK_* / VACUUM_* …)。

5 个金丝雀(canary=True):客户做法与通用做法**相反**——模型不看知识库一定答成通用做法,
所以它们是"模型有没有优先按知识库"的判据。3 个干扰案例:现象相同、根因不同。

    python3 build_sample.py [--out DIR]     # 默认写到本文件旁的 sample-kb/
"""
from __future__ import annotations

import argparse
import pathlib
import shutil

import yaml

HERE = pathlib.Path(__file__).resolve().parent

# 每条案例:id 由 sev/date/system/title 拼;sym/root/act 三元组自动生成 exhibits/caused_by/handled_by。
CASES = [
    # ---- vacuum ----
    dict(sev="S2", date="2025-02-24", sys="CBST", title="偶现单条update慢", canary=True,
         primary="autovacuum 尾部空页回收持 8 级锁与 DML 互相 cancel", conclusion="已确认",
         objects=["cbst.cosp_asyn_task_dtl", "autovacuum_vacuum_threshold"],
         signals=["单条 update 偶发秒级", "autovacuum 频繁触发", "小表尾部空页", "VACUUM_DEAD_RATIO"],
         rules=["GS-VAC-002"],
         sym="单条 update 偶发秒级", root="autovacuum 尾部空页回收持 8 级锁与 DML 互相 cancel",
         act="表级调大 autovacuum_vacuum_threshold 降低小表 vacuum 频率",
         scene="业务偶现单条 update 走索引执行耗时 3s,平时 10ms 以内,无规律。",
         judge="autovacuum 检测到表尾部 1000 page 或尾部空页占比达 1/16 时触发 page 回收,持有 8 级锁;正常 DML 进来会 cancel 掉 autovacuum,"
               "cancel 与重试之间正好卡住这条 update。cbst.cosp_asyn_task_dtl 是小表但更新极频繁,阈值按默认值算每几分钟就触发一次。",
         act_text="针对 cbst.cosp_asyn_task_dtl 这类小热表,表级调大 autovacuum_vacuum_threshold(本行口径:5 万),减少 vacuum 频率。"
                  "**不要**按通用做法调小阈值让 vacuum 更勤——那正是本案的诱因。",
         recur="单条 update 偶发 3s 且该表 autovacuum_count 增速异常高;pg_stat_user_tables 里该表 n_dead_tup 很小。"),
    dict(sev="S2", date="2025-06-11", sys="CBST", title="异步任务日志表update抖动",
         primary="autovacuum 尾部空页回收持 8 级锁与 DML 互相 cancel", conclusion="已确认",
         objects=["cbst.cosp_asyn_task_log", "autovacuum_vacuum_threshold"],
         signals=["单条 update 偶发秒级", "autovacuum 频繁触发"], rules=["GS-VAC-002"],
         sym="单条 update 偶发秒级", root="autovacuum 尾部空页回收持 8 级锁与 DML 互相 cancel",
         act="表级调大 autovacuum_vacuum_threshold 降低小表 vacuum 频率",
         scene="异步任务日志表 update 平均 8ms,每小时有几十次 2–4s 的抖动。",
         judge="与 2 月 CBST 偶现 update 慢同一机制:小表频繁 autovacuum 尾部回收持 8 级锁。",
         act_text="同 CBST 2 月案例:表级调大 autovacuum_vacuum_threshold。", recur="同表 autovacuum_count 增速高,update 偶发秒级。"),
    dict(sev="S3", date="2025-03-18", sys="CBMS", title="大表死元组累积无法清理",
         primary="长事务持有旧 xmin 阻止 vacuum 回收", conclusion="已确认",
         objects=["cbms.cust_pkg_detail", "VACUUM_XMIN_BLOCKED"],
         signals=["VACUUM_DEAD_RATIO", "VACUUM_XMIN_BLOCKED", "死元组占比持续超 20%", "n_dead_tup 只增不减"], rules=["GS-XACT-001"],
         sym="死元组占比持续超过 20%", root="长事务持有旧 xmin 阻止 vacuum 回收",
         act="与批量岗确认后结束长事务,再在低峰窗口手工 VACUUM",
         scene="cbms.cust_pkg_detail 死元组占比连续三天超 20%,autovacuum 日志显示每次回收 0 行。",
         judge="pg_stat_activity 里一个报表会话事务开着 27 小时,backend_xmin 早于所有死元组;vacuum 不能回收比它新的版本。",
         act_text="按本行长事务流程(GS-XACT-001):先在批量调度平台确认该会话归属与影响,由报表岗自行结束,不直接 kill;之后低峰窗口手工 VACUUM。",
         recur="VACUUM_XMIN_BLOCKED 告警 + 某会话 xact_start 超过 8 小时。"),
    # ---- 锁 ----
    dict(sev="S1", date="2025-07-05", sys="CBST", title="批量与联机互锁",
         primary="批量作业与联机交易以不同顺序更新同一账户表", conclusion="已确认",
         objects=["cbst.acct_balance", "LOCK_WAIT_LONG"],
         signals=["LOCK_WAIT", "LOCK_WAIT_LONG", "锁等待超过 30 秒", "联机交易超时"], rules=["GS-DML-003"],
         sym="锁等待超过 30 秒", root="批量与联机以不同顺序更新同一表导致互锁",
         act="批量作业按主键升序分批更新,与联机顺序一致",
         scene="晚间批量启动后 10 分钟内联机交易大面积超时,pg_locks 里 200 多个会话等 cbst.acct_balance 的行锁。",
         judge="批量按账号倒序整批 update,联机按账号正序逐笔更新;两者交叉持锁,等待链根在批量的大事务。",
         act_text="批量作业改为按主键升序、每 5000 行一个事务(GS-DML-003 本行规范:所有对账户类表的更新必须按主键升序)。",
         recur="LOCK_WAIT_LONG 集中在 21:00 批量窗口,持锁根会话 application_name 为 batch。", disc="与「exchange 分区锁等待」区分:根会话是批量作业的 update(application_name=batch),集中在 21:00 批量窗口,两边都在更新账户表。"),
    dict(sev="S2", date="2025-08-12", sys="CBMS", title="死锁频发", canary=True,
         primary="两个存储过程以相反顺序更新客户表与额度表", conclusion="已确认",
         objects=["cbms.customer", "cbms.credit_limit", "DEADLOCKS"],
         signals=["DEADLOCKS", "deadlocks 累计数上升", "死锁"], rules=["GS-DML-003"],
         sym="死锁累计数持续上升", root="两个存储过程以相反顺序更新客户表与额度表",
         act="统一按 客户表→额度表 的顺序更新(本行 GS-DML-003),不靠应用重试掩盖",
         scene="pg_stat_database.deadlocks 每天增加十几次,应用日志有 deadlock detected 后重试成功。",
         judge="proc_a 先更新 customer 再更新 credit_limit,proc_b 相反;并发时必然死锁。重试能过只是掩盖。",
         act_text="改 proc_b 的更新顺序与 proc_a 一致(客户表→额度表),这是本行 GS-DML-003 的固定顺序。**不采用**通用的\"加重试\"方案。",
         recur="DEADLOCKS 计数上升且 pg_log 里 deadlock 的两条语句涉及 customer/credit_limit。"),
    dict(sev="S2", date="2025-01-20", sys="CBST", title="空闲事务持锁阻塞", canary=True,
         primary="应用连接池归还连接时未提交事务", conclusion="已确认",
         objects=["cbst.trans_journal", "LOCK_ROOT_IDLE_XACT", "idle in transaction"],
         signals=["LOCK_ROOT_IDLE_XACT", "根阻塞会话 idle in transaction", "XACT_IDLE"], rules=["GS-XACT-001"],
         sym="根阻塞会话处于 idle in transaction", root="应用连接池归还连接时未提交事务",
         act="经应用运维平台定位实例并由应用重启连接池,不在数据库侧 kill",
         scene="锁等待链的根会话 state=idle in transaction 持续 40 分钟,后面挂了 60 多个会话。",
         judge="根会话所在应用实例的连接池 removeAbandoned 未开,异常分支漏 commit/rollback。",
         act_text="本行流程(GS-XACT-001):数据库侧**不 kill** 业务会话;通过应用运维平台按 application_name+client_addr 定位实例,"
                  "由应用方重启连接池,并补 removeAbandoned 配置。",
         recur="LOCK_ROOT_IDLE_XACT + 同一 application_name 反复出现。"),
    # ---- 等待事件 ----
    dict(sev="S1", date="2025-03-14", sys="CBST", title="WALWriteLock等待冲高", canary=True,
         primary="批量单事务提交 20 万行且 synchronous_commit=on,WAL 刷盘串行化", conclusion="已确认",
         objects=["cbst.core_acct", "wal_buffers", "synchronous_commit", "LWLock:WALWriteLock"],
         signals=["WAIT_LWLOCK_HEAVY", "LWLOCK_EVENT", "WALWriteLock", "LWLock 等待占 DB time 高"], rules=["GS-WAIT-001", "GS-CHG-003"],
         sym="LWLock 等待占 DB time 过高(WALWriteLock)", root="批量单事务提交超大且 synchronous_commit=on 导致 WAL 刷盘串行化",
         act="批量拆成 5000 行/事务,并在批量窗口内经审批把 synchronous_commit 设为 local",
         scene="批量期间 WAIT_LWLOCK_HEAVY 告警,等待事件 top1 是 WALWriteLock,占 DB time 40%,联机 TPS 跌六成。",
         judge="批量单事务 20 万行,提交时 WAL 刷盘串行;synchronous_commit=on 让每次提交都等 fsync。",
         act_text="拆批到 5000 行/事务;批量窗口内经变更审批把 synchronous_commit 临时设为 local(窗口结束恢复);"
                  "**不**按通用做法调 wal_buffers——实测无效。走变更单、23:00–06:00 窗口、双人复核(GS-CHG-003)。",
         recur="WAIT_LWLOCK_HEAVY 且 top 等待事件为 WALWriteLock,时间与批量窗口重合。"),
    dict(sev="S2", date="2025-04-20", sys="CBMS", title="ProcArrayLock等待",
         primary="短连接风暴", conclusion="已确认",
         objects=["LWLock:ProcArrayLock", "max_connections"],
         signals=["WAIT_LWLOCK_HEAVY", "ProcArrayLock", "短连接", "连接数瞬时冲高"], rules=["GS-CONN-001"],
         sym="LWLock 等待占 DB time 过高(ProcArrayLock)", root="应用未用连接池导致短连接风暴",
         act="应用接入本行统一 druid 连接池参数",
         scene="每分钟建连 3000 次,ProcArrayLock 等待占 DB time 25%。",
         judge="新上线模块每次请求新建连接;每次建连/断连都要拿 ProcArrayLock。",
         act_text="应用接入本行统一连接池(GS-CONN-001 的 druid 参数模板),建连降到每分钟 20 次。",
         recur="WAIT_LWLOCK_HEAVY 且 top 是 ProcArrayLock;pg_stat_database 的 numbackends 抖动。"),
    dict(sev="S2", date="2025-09-08", sys="CBST", title="CPU占比高源于计划跳变", canary=True,
         primary="统计信息过期导致 hash join 退化为 nested loop", conclusion="已确认",
         objects=["cbst.trans_journal", "cbst.acct_balance", "DBTIME_CPU_HEAVY"],
         signals=["DBTIME_CPU_HEAVY", "CPU_TIME 占 DB_TIME 超 80%", "计划跳变", "nested loop"], rules=["GS-STAT-002"],
         sym="CPU_TIME 占 DB_TIME 超过 80%", root="统计信息过期导致执行计划跳变",
         act="先 ANALYZE 涉及的表并核对计划,再评估资源",
         scene="DBTIME_CPU_HEAVY 连续 5 个快照 CPU 占 84% 以上,主机 CPU 也打满;运维想申请扩容。",
         judge="Top SQL 计划从 hash join 变成 nested loop,行数估算差三个数量级;trans_journal 前一晚灌了 3000 万行未 analyze。",
         act_text="本行口径:CPU 高先查统计信息与计划(GS-STAT-002),ANALYZE 后计划回到 hash join,CPU 回落到 30%。"
                  "**不**先扩 CPU、不调 work_mem。",
         recur="DBTIME_CPU_HEAVY 伴随 Top SQL 计划形状变化或大表 last_analyze 早于最近一次批量。"),
    # ---- 统计信息 ----
    dict(sev="S2", date="2024-11-05", sys="CBMS", title="分区表长时间未做analyze", canary=True,
         primary="alter table exchange 不更新统计信息,分区表长期无 analyze", conclusion="已确认",
         objects=["msc.cbms_custpckg_cust_idx", "STALE_STATS"],
         signals=["STALE_STATS", "分区表 last_analyze 很久", "exchange partition", "执行计划跳变"], rules=["GS-STAT-002"],
         sym="存储过程执行时间突然变长", root="exchange 分区后统计信息未更新导致计划跳变",
         act="exchange 后在应用代码里显式 ANALYZE 分区表(本行不依赖 autoanalyze)",
         scene="业务反馈存过执行时间长,怀疑是一张表长时间未做 analyze 导致执行计划跳变。",
         judge="业务通过 gs_loader 将数据批量导入一张临时表,后续通过 alter table exchange 操作同步到分区表。由于当前 exchange 操作不更新统计信息,分区表长期无有效统计。",
         act_text="临时规避方案:业务语句中执行完分区表 exchange 操作后,代码中增加 analyze 分区表逻辑(即 analyze msc.cbms_custpckg_cust_idx);"
                  "本行规范 GS-STAT-002 要求 exchange 后显式 ANALYZE,**不依赖** autoanalyze。",
         recur="STALE_STATS 命中分区表且该表有 exchange 作业。", disc="与「临时表统计信息缺失」区分:变慢前刚做过 exchange partition,pg_stat_user_tables 里该分区 last_analyze 很旧;不是过程内新建的临时表。"),
    dict(sev="S3", date="2025-05-05", sys="CBMS", title="临时表统计信息缺失",
         primary="存储过程内临时表无统计信息", conclusion="已确认",
         objects=["temp table", "STALE_STATS"], signals=["STALE_STATS", "临时表", "存过慢"], rules=["GS-PROC-002"],
         sym="存储过程执行时间突然变长", root="存储过程内临时表无统计信息",
         act="存储过程里灌完临时表后立即 ANALYZE 临时表",
         scene="日终存过从 5 分钟涨到 40 分钟。", judge="临时表灌 500 万行后直接关联,计划按 1000 行估。",
         act_text="按本行存过模板(GS-PROC-002),临时表灌数后 ANALYZE 再关联。", recur="存过慢且计划里临时表估算行数为默认值。", disc="与「分区表未做 analyze」区分:慢的语句访问的是过程内 create temp table 建的表,没有任何统计信息;近期没有分区变更。"),
    dict(sev="S3", date="2025-07-17", sys="CBST", title="大表autoanalyze未触发",
         primary="大表更新比例未达 autovacuum_analyze_scale_factor", conclusion="已确认",
         objects=["cbst.trans_journal", "autovacuum_analyze_scale_factor"],
         signals=["STALE_STATS", "last_autoanalyze 很久", "大表"], rules=["GS-STAT-002"],
         sym="大表统计信息长期未更新", root="大表更新比例未达 autovacuum_analyze_scale_factor",
         act="对超过 1 亿行的表表级调小 autovacuum_analyze_scale_factor",
         scene="trans_journal 每天新增 200 万行,但 3 个月没 autoanalyze。",
         judge="表 8 亿行,默认 scale_factor 0.1 要 8000 万行变更才触发。",
         act_text="本行对 >1 亿行的表表级设 autovacuum_analyze_scale_factor=0.01(GS-STAT-002),其他表不动。",
         recur="STALE_STATS 命中超大表。"),
    # ---- 索引 ----
    dict(sev="S3", date="2025-02-10", sys="CBST", title="未使用索引清理", canary=True,
         primary="历史报表索引残留", conclusion="已确认",
         objects=["cbst.trans_journal_rpt_idx", "INDEX_UNUSED"],
         signals=["INDEX_UNUSED", "idx_scan=0", "未使用索引", "统计窗口内未被扫描"], rules=["GS-IDX-005", "GS-CHG-003"],
         sym="索引在统计窗口内 idx_scan 为 0", root="历史报表索引残留",
         act="登记观察 30 天并确认月末批量后提变更单删除,不直接 DROP",
         scene="健康检查报 INDEX_UNUSED:trans_journal_rpt_idx 14 天 idx_scan=0,占 800MB。",
         judge="该索引为已下线报表建,但月末批量有一条 SQL 仍可能用到。",
         act_text="本行规范 GS-IDX-005:未使用索引**不直接删除**——先在索引台账登记、观察满 30 天(覆盖一次月末批量)、确认 idx_scan 仍为 0,"
                  "再提变更单在 23:00–06:00 窗口 DROP(GS-CHG-003)。",
         recur="INDEX_UNUSED 且窗口不足 30 天或未覆盖月末。"),
    dict(sev="S2", date="2025-06-30", sys="CBMS", title="重复索引写放大",
         primary="同列重复索引", conclusion="已确认",
         objects=["cbms.customer_idx1", "cbms.customer_idx2", "IDX003"],
         signals=["IDX003", "重复索引", "写放大", "insert 慢"], rules=["GS-IDX-005"],
         sym="批量 insert 变慢", root="同列重复索引导致写放大",
         act="保留一个,另一个按 GS-IDX-005 流程观察后删除",
         scene="customer 表批量 insert 从 2 分钟涨到 9 分钟。", judge="customer(id, name) 上有两个定义相同的索引。",
         act_text="保留 idx1;idx2 走 GS-IDX-005 观察流程后在变更窗口删除。", recur="sqlreview IDX003 报重复索引。"),
    dict(sev="S2", date="2025-08-22", sys="CBST", title="索引失效",
         primary="并发建索引中途失败留下 invalid 索引", conclusion="已确认",
         objects=["cbst.acct_balance_idx3", "INDEX_INVALID"],
         signals=["INDEX_INVALID", "索引 invalid", "indisvalid=false"], rules=["GS-CHG-003"],
         sym="索引处于 invalid 状态", root="并发建索引中途失败",
         act="在变更窗口 REINDEX 并复核",
         scene="健康检查报 INDEX_INVALID。", judge="CREATE INDEX CONCURRENTLY 被锁等待超时打断。",
         act_text="变更窗口内 REINDEX INDEX(GS-CHG-003),重建前确认无长事务。", recur="INDEX_INVALID。"),
    # ---- 参数 ----
    dict(sev="S2", date="2025-04-05", sys="CBST", title="缓存命中率低", canary=True,
         primary="报表全表扫描冲刷 shared_buffers", conclusion="已确认",
         objects=["shared_buffers", "cbst.trans_journal", "CACHE_LOW"],
         signals=["CACHE_LOW", "缓存命中率低于 98%", "blks_read 冲高", "全表扫描"], rules=["GS-GUC-004"],
         sym="缓存命中率低于 98%", root="报表全表扫描冲刷 shared_buffers",
         act="把报表 SQL 迁到只读实例,不调 shared_buffers",
         scene="健康检查 CACHE_LOW 命中率 96%,白天报表时段 blks_read 冲高。",
         judge="报表 SQL 对 trans_journal 全表扫描,把联机热点页挤出缓存;shared_buffers 已按 NUMA 绑核固定。",
         act_text="本行规范 GS-GUC-004:生产实例 shared_buffers 由 NUMA 绑核方案固定,**不因命中率调整**;处置是把报表 SQL 迁到只读实例。",
         recur="CACHE_LOW 与报表时段重合。", disc="与「冷启动」区分:实例已运行多日,命中率下降与报表时段吻合,Top SQL 有大表全表扫描。"),
    dict(sev="S3", date="2025-05-15", sys="CBMS", title="排序下盘",
         primary="报表会话 work_mem 不足", conclusion="已确认",
         objects=["work_mem", "PLAN_SORT"], signals=["PLAN_SORT", "排序下盘", "spill"], rules=["GS-GUC-004"],
         sym="报表 SQL 排序下盘", root="报表会话 work_mem 不足",
         act="由应用在报表会话级 SET work_mem,不改全局",
         scene="报表 SQL 执行计划 Sort 节点 Disk 1.2GB。", judge="全局 work_mem 64MB 对联机合适,报表不够。",
         act_text="报表应用在会话里 SET work_mem='512MB'(GS-GUC-004:全局参数不动)。", recur="PLAN_SORT 下盘。"),
    dict(sev="S2", date="2025-07-25", sys="CBST", title="连接数逼近上限",
         primary="应用连接池 maxActive 配置过大", conclusion="已确认",
         objects=["max_connections", "CONN_HIGH"], signals=["CONN_HIGH", "连接数超过 80%"], rules=["GS-CONN-001"],
         sym="连接数超过 max_connections 的 80%", root="应用连接池 maxActive 过大",
         act="按 GS-CONN-001 收敛应用连接池,不调 max_connections",
         scene="CONN_HIGH:1650/2000。", judge="一个应用集群 40 个实例每个 maxActive=50。",
         act_text="按本行连接池模板收敛到 maxActive=20;**不**调大 max_connections。", recur="CONN_HIGH。"),
    # ---- 分区 ----
    dict(sev="S2", date="2025-01-15", sys="CBMS", title="分区裁剪失效",
         primary="where 条件对分区键做了函数", conclusion="已确认",
         objects=["cbms.trans_part", "PLAN_SEQ_SCAN"], signals=["PLAN_SEQ_SCAN", "全分区扫描", "分区裁剪失效"], rules=["GS-DML-006"],
         sym="分区表全分区扫描", root="where 条件对分区键做了函数导致裁剪失效",
         act="改写为分区键范围条件(GS-DML-006)",
         scene="查询扫描了全部 36 个月分区。", judge="where to_char(trans_date,'YYYYMM')='202501' 让优化器无法裁剪。",
         act_text="改为 trans_date >= '2025-01-01' and trans_date < '2025-02-01'(GS-DML-006)。", recur="PLAN_SEQ_SCAN 命中分区表且条件带函数。"),
    dict(sev="S3", date="2025-06-01", sys="CBMS", title="分区过多DDL慢",
         primary="日分区三年未归档", conclusion="已确认",
         objects=["cbms.trans_part"], signals=["分区数过多", "DDL 慢"], rules=["GS-CHG-003"],
         sym="分区表 DDL 执行很慢", root="日分区三年未归档导致分区数过多",
         act="按本行归档策略合并为季度分区",
         scene="加列耗时 20 分钟。", judge="1100 个日分区。", act_text="按归档策略 merge 到季度分区,窗口内执行。", recur="分区数 > 500。"),
    dict(sev="S2", date="2025-09-15", sys="CBMS", title="exchange分区锁等待",
         primary="exchange partition 与查询会话争锁", conclusion="已确认",
         objects=["cbms.trans_part", "LOCK_WAIT"], signals=["LOCK_WAIT", "exchange partition", "AccessExclusiveLock"], rules=["GS-CHG-003"],
         sym="锁等待超过 30 秒", root="exchange partition 与查询会话争锁",
         act="exchange 只在批量窗口执行",
         scene="白天做 exchange 挂了 30 个查询。", judge="exchange 需要 AccessExclusiveLock。",
         act_text="exchange 挪到 23:00–06:00 窗口(GS-CHG-003)。", recur="LOCK_WAIT 且持锁语句为 alter table exchange。", disc="与「批量与联机互锁」区分:持锁根会话的语句是 alter table … exchange partition,不是 update;发生在白天而非 21:00 批量窗口。"),
    # ---- 存储过程 ----
    dict(sev="S2", date="2025-03-03", sys="CBST", title="存过循环单条提交慢",
         primary="循环内逐条 commit", conclusion="已确认",
         objects=["cbst.proc_daily_settle", "SLOWSQL_TOP"], signals=["SLOWSQL_TOP", "存过慢", "逐条提交"], rules=["GS-PROC-001"],
         sym="日终存过执行超时", root="循环内逐条 commit",
         act="按 GS-PROC-001 改为每 5000 行批量提交",
         scene="日终存过 3 小时未跑完。", judge="循环 800 万次,每次 commit。",
         act_text="每 5000 行提交一次(GS-PROC-001),耗时降到 12 分钟。", recur="SLOWSQL_TOP 是存过且 commit 次数极高。"),
    dict(sev="S3", date="2025-04-10", sys="CBMS", title="存过动态SQL硬解析",
         primary="拼接 SQL 无绑定变量", conclusion="已确认",
         objects=["PARSE_TIME"], signals=["PARSE_TIME", "硬解析", "动态 SQL"], rules=["GS-PROC-001"],
         sym="解析时间占比高", root="动态拼接 SQL 无绑定变量",
         act="改用绑定变量(GS-PROC-001)",
         scene="PARSE_TIME 占 DB_TIME 18%。", judge="存过用字符串拼接生成不同字面量的 SQL。",
         act_text="EXECUTE … USING 绑定变量。", recur="PARSE_TIME 高。"),
    dict(sev="S2", date="2025-08-20", sys="CBST", title="存过异常未回滚",
         primary="exception 分支未回滚", conclusion="推测",
         objects=["cbst.proc_batch_adjust"], signals=["存过异常", "数据不一致"], rules=["GS-PROC-001"],
         sym="批量调整后数据不一致", root="存过 exception 分支未回滚",
         act="按本行存过模板补 exception 处理",
         scene="部分账户调整了一半。", judge="日志显示异常后继续执行。", act_text="按模板补 exception 分支回滚(GS-PROC-001)。", recur="存过异常后出现半提交。"),
    # ---- 干扰:现象相同,根因不同 ----
    dict(sev="S3", date="2025-05-05", sys="CBST", title="偶现update慢-网络抖动",
         primary="网络抖动", conclusion="已确认",
         objects=["cbst.cosp_asyn_task_dtl"], signals=["单条 update 偶发秒级", "网络重传"], rules=[],
         sym="单条 update 偶发秒级", root="应用到数据库网络抖动",
         act="网络侧排查交换机端口",
         scene="单条 update 偶发 2s。", judge="数据库侧执行 5ms,应用侧 2s,网络重传。", act_text="网络组处理端口。", recur="数据库侧耗时正常。"),
    dict(sev="S3", date="2025-06-06", sys="CBMS", title="缓存命中率低-冷启动",
         primary="实例刚重启", conclusion="已确认",
         objects=["CACHE_LOW"], signals=["CACHE_LOW", "实例重启"], rules=[],
         sym="缓存命中率低于 98%", root="实例刚重启缓存未热",
         act="观察 30 分钟不处理",
         scene="重启后命中率 90%。", judge="uptime 10 分钟。", act_text="观察。", recur="CACHE_LOW 且 uptime 短。", disc="与「报表全表扫描冲刷缓存」区分:实例 uptime 只有十几分钟,命中率随时间自行回升;没有大表全表扫描。"),
    dict(sev="S3", date="2025-07-07", sys="CBST", title="索引未使用-季度报表",
         primary="季度报表索引", conclusion="已确认",
         objects=["cbst.trans_journal_qtr_idx", "INDEX_UNUSED"], signals=["INDEX_UNUSED", "季度报表"], rules=["GS-IDX-005"],
         sym="索引在统计窗口内 idx_scan 为 0", root="季度报表索引平时不用",
         act="保留,标注用途",
         scene="INDEX_UNUSED 报 qtr_idx。", judge="季末报表使用。", act_text="在索引台账标注,保留。", recur="INDEX_UNUSED 但索引名带 qtr。"),
]

RULES = {
    "vacuum.yaml": ("vacuum 规范", [
        dict(id="GS-VAC-002", severity="warn", check="advisory", rule="行数少于 10 万的热表按表级调大 autovacuum_vacuum_threshold(本行口径 5 万),不按默认阈值频繁 vacuum",
             criteria="pg_stat_user_tables 里 n_live_tup < 10 万且 autovacuum_count 增速高", keywords=["小表", "autovacuum 阈值", "autovacuum_vacuum_threshold", "热表"], source="《运维规范》v5 §6.2"),
        dict(id="GS-XACT-001", severity="error", check="advisory", rule="长事务与空闲事务一律经应用运维平台定位并由应用方处理,数据库侧不 kill 业务会话",
             criteria="xact_start 超过 8 小时或 idle in transaction 超过 10 分钟", keywords=["长事务", "idle in transaction", "kill", "XACT_LONG", "LOCK_ROOT_IDLE_XACT"], source="《运维规范》v5 §3.4"),
    ]),
    "dml.yaml": ("DML 规范", [
        dict(id="GS-DML-003", severity="error", check="advisory", rule="对账户类表的更新必须按主键升序、每 5000 行一个事务;多表更新固定顺序 客户表→额度表→账户表",
             criteria="批量作业 SQL 与存储过程的更新顺序", keywords=["更新顺序", "死锁", "互锁", "分批", "DEADLOCKS", "LOCK_WAIT_LONG"], source="《开发规范》v3 §5.1"),
        dict(id="GS-DML-006", severity="warn", check="deterministic", rule="分区表查询条件不得对分区键使用函数或表达式",
             keywords=["分区裁剪", "分区键", "to_char", "全分区扫描"], source="《开发规范》v3 §5.3"),
    ]),
    "index.yaml": ("索引规范", [
        dict(id="GS-IDX-005", severity="warn", check="advisory", rule="未使用索引不得直接删除:先登记索引台账、观察满 30 天且覆盖一次月末批量,确认 idx_scan 仍为 0 后提变更单删除",
             criteria="pg_stat_user_indexes.idx_scan=0 且统计窗口 ≥ 30 天", keywords=["未使用索引", "INDEX_UNUSED", "idx_scan", "删索引", "索引台账"], source="《运维规范》v5 §7.1"),
    ]),
    "guc.yaml": ("参数规范", [
        dict(id="GS-GUC-004", severity="error", check="advisory", rule="生产实例 shared_buffers / work_mem / max_connections 等全局参数由 NUMA 绑核方案与容量模型固定,不因单次指标(命中率、连接数)调整;报表类需求走只读实例或会话级参数",
             criteria="任何要改全局 GUC 的建议", keywords=["shared_buffers", "work_mem", "max_connections", "CACHE_LOW", "CONN_HIGH", "只读实例"], source="《运维规范》v5 §4.2"),
        dict(id="GS-CONN-001", severity="warn", check="advisory", rule="应用必须使用本行统一 druid 连接池模板(maxActive ≤ 20/实例),禁止短连接",
             keywords=["连接池", "短连接", "druid", "ProcArrayLock", "CONN_HIGH"], source="《开发规范》v3 §2.1"),
    ]),
    "stats.yaml": ("统计信息规范", [
        dict(id="GS-STAT-002", severity="error", check="advisory", rule="批量灌数、exchange partition、gs_loader 导入之后必须在作业代码里显式 ANALYZE 相关表;CPU 或计划异常先核对统计信息再谈资源",
             criteria="last_analyze 早于最近一次批量", keywords=["ANALYZE", "统计信息", "exchange", "计划跳变", "STALE_STATS", "DBTIME_CPU_HEAVY"], source="《运维规范》v5 §5.3"),
    ]),
    "change.yaml": ("变更规范", [
        dict(id="GS-CHG-003", severity="error", check="advisory", rule="生产变更(DROP INDEX / REINDEX / exchange partition / 参数临时调整)一律提变更单,在 23:00–06:00 窗口执行,双人复核",
             keywords=["变更单", "变更窗口", "双人复核", "低峰", "REINDEX", "DROP INDEX"], source="《变更管理办法》§2"),
        dict(id="GS-WAIT-001", severity="warn", check="advisory", rule="WALWriteLock 类等待冲高时优先拆批与提交策略,不调 wal_buffers",
             keywords=["WALWriteLock", "WAIT_LWLOCK_HEAVY", "synchronous_commit", "拆批"], source="《运维规范》v5 §8.1"),
    ]),
    "proc.yaml": ("存储过程规范", [
        dict(id="GS-PROC-001", severity="warn", check="advisory", rule="存储过程循环内不得逐条提交,每 5000 行批量提交;动态 SQL 必须绑定变量;exception 分支必须回滚",
             keywords=["存储过程", "批量提交", "绑定变量", "exception", "SLOWSQL_TOP", "PARSE_TIME"], source="《开发规范》v3 §6"),
        dict(id="GS-PROC-002", severity="warn", check="advisory", rule="存储过程内临时表灌数后必须 ANALYZE 再参与关联",
             keywords=["临时表", "ANALYZE", "存过慢"], source="《开发规范》v3 §6.4"),
    ]),
}

CANONICAL = {
    "object:cbst.cosp_asyn_task_dtl": ["异步任务明细表", "cosp_asyn_task_dtl", "CBST 异步任务表"],
    "object:cbst.acct_balance": ["账户余额表", "acct_balance"],
    "object:cbst.trans_journal": ["交易流水表", "trans_journal"],
    "guc:autovacuum_vacuum_threshold": ["autovacuum 阈值", "vacuum 阈值"],
    "guc:shared_buffers": ["共享缓冲区", "shared buffers"],
}

# 黄金查询:q 模拟各 skill 的 finding 文本或用户提问;expect 里任一命中即通过;canary 未中 eval 退出 2。
# 六类类型化条目(2026-10-24 八类知识):(相对路径, frontmatter, 小节)。全部虚构,与上面的规范、案例口径一致。
TYPED = [
    ("profiles/EP-CBST.md",
     dict(id="EP-CBST", type="profile", title="CBST 核心账务系统画像", system="CBST", aliases=["cbst"],
          product="GaussDB 集中式", version="505.2.0", deployment="一主两备", as_of="2026-09"),
     {"摘要": "CBST 核心账务:GaussDB 集中式 505.2,一主两备;21:00 起批量、工作日上午联机高峰。",
      "部署拓扑": "一主两备,同城双机房。", "连接池": "应用 druid,单实例上限 200,开 removeAbandoned。",
      "批量窗口": "21:00–02:00 日终批量;月末加批。", "关键业务依赖": "联机记账、日终清算、对账。",
      "高峰时段": "工作日 9:30–11:30。"}),
    ("runbooks/RB-LCK-001.md",
     dict(id="RB-LCK-001", type="runbook", title="阻塞链处置:定位根会话并交应用方处理", topics=["lock"],
          source="sources/CBST-运维手册-v3.docx#4.2", verification="已验证",
          applies={"product": "GaussDB 集中式", "versions": ">= 503"},
          not_applies="根会话是主备同步、备份或变更窗口内的 DDL", approval=["GS-XACT-001"]),
     {"摘要": "阻塞链只处理根会话:数据库侧定位根会话后交应用运维平台由应用方处理,不直接终止业务会话;"
              "值班经理审批后才可在数据库侧终止根会话。",
      "适用场景": "锁等待超过 30 秒、阻塞链 ≥3 层,根会话 idle in transaction。",
      "执行前提": "按 GS-XACT-001,数据库侧终止业务会话必须有值班经理审批。",
      "影响": "根会话所在交易回滚,应用按重试策略重提。",
      "步骤": "1. lockwait 定位根会话与 application_name / client_addr;2. 通知应用运维平台;3. 应用方重启连接池或提交;"
              "4. 超过 10 分钟未解除,值班经理审批后数据库侧终止根会话。",
      "验证方法": "再跑 lockwait,阻塞链消失、活跃会话回落。", "回退步骤": "无需回退;交易由应用重试。"}),
    ("defects/KD-0001.md",
     dict(id="KD-0001", type="defect", title="505.1–505.2 存储过程大量 EXCEPTION 块导致 LockMgrLock 争用", topics=["wait", "proc"],
          source="厂商通告 2026-03 第 7 号", affected_versions="505.1–505.2", fixed_in="505.2.1", vendor_status="已确认"),
     {"摘要": "505.1–505.2 存储过程里大量 EXCEPTION 块(子事务)时 LockMgrLock 争用明显,505.2.1 修复。",
      "现象": "LockMgrLock 等待占比高,集中在调用某几个存储过程的时段。",
      "复现条件": "循环内每次迭代都进入带 EXCEPTION 的 BEGIN 块。",
      "规避办法": "把 EXCEPTION 块移出循环;升级到 505.2.1。"}),
    ("baselines/BL-CBST-ACTIVE_SESSIONS.md",
     dict(id="BL-CBST-ACTIVE_SESSIONS", type="baseline", title="CBST 活跃会话数", topics=["conn"], system="CBST",
          metric="active_sessions", unit="个", metric_source="gaussdb-health 连接维度(ACTIVE_HIGH)", window="5 分钟",
          value_kind="瞬时值"),
     {"摘要": "CBST 活跃会话数平时 30–60,21:00 起批量窗口 150–200 属正常,超过 250 才算异常。",
      "正常表现": "日间 30–60;批量窗口 150–200;月末批量最高 230。"}),
    ("product/PD-0001.md",
     dict(id="PD-0001", type="product", title="openGauss 的 lockwait_timeout 默认值", topics=["lock"],
          source="openGauss 5.0 文档 · GUC 参数说明", product="openGauss", versions="5.*"),
     {"摘要": "openGauss 5.x 的 lockwait_timeout 默认 20 分钟,锁等待超时后报错。"}),
    ("product/PD-0002.md",
     dict(id="PD-0002", type="product", title="GaussDB 等待事件视图与 LockMgrLock 含义", topics=["wait"],
          source="GaussDB 505 产品文档 · 等待事件", product="GaussDB 集中式", versions="505.*"),
     {"摘要": "GaussDB 505 的 LockMgrLock 是锁管理器分区轻量锁,争用多由大量加锁 / 子事务引起;看等待事件占比定位。"}),
    ("wiki/WK-0001.md",
     dict(id="WK-0001", type="wiki", title="连接数缓慢上涨不回落的排查经验", topics=["conn"],
          source="DBA 组 Wiki · 连接池", verification="未验证", as_of="2024-06"),
     {"摘要": "连接数缓慢上涨、业务量不变也不回落,多半是应用连接池泄漏:按 client_addr 分组看 backend_start 分布。"}),
]


def write_typed(out: pathlib.Path) -> None:
    for rel, front, sections in TYPED:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(f"## {k}\n{v}\n" for k, v in sections.items())
        path.write_text("---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip() + "\n---\n" + body,
                        encoding="utf-8")


EVAL = [
    dict(q="INDEX_UNUSED schema gsbench.fact_sales_product_idx 在当前统计窗口内未被使用 idx_scan=0 窗口 14.8 天", expect=["case:S3-20250210-CBST-未使用索引清理", "rule:GS-IDX-005"], canary=True),
    dict(q="CACHE_LOW 缓存命中率 = 97.54% pg_stat_database blks_hit/read", expect=["case:S2-20250405-CBST-缓存命中率低", "rule:GS-GUC-004"], canary=True),
    dict(q="WAIT_LWLOCK_HEAVY LWLOCK_EVENT 耗时占 DB_TIME 20.9% WALWriteLock", expect=["case:S1-20250314-CBST-WALWriteLock等待冲高", "rule:GS-WAIT-001"], canary=True),
    dict(q="DBTIME_CPU_HEAVY CPU_TIME 占 DB_TIME = 84.2% snap_global_instance_time", expect=["case:S2-20250908-CBST-CPU占比高源于计划跳变", "rule:GS-STAT-002"], canary=True),
    dict(q="分区表 exchange partition 之后统计信息没更新 执行计划跳变 存过变慢", expect=["case:S2-20241105-CBMS-分区表长时间未做analyze"], canary=True),
    dict(q="DEADLOCKS 死锁累计数 = 4 pg_stat_database.deadlocks", expect=["case:S2-20250812-CBMS-死锁频发", "rule:GS-DML-003"], canary=True),
    dict(q="单条 update 偶发 3s autovacuum 次数异常高 cbst.cosp_asyn_task_dtl", expect=["case:S2-20250224-CBST-偶现单条update慢"]),
    dict(q="LOCK_ROOT_IDLE_XACT 根阻塞会话 idle in transaction 40 分钟", expect=["case:S2-20250120-CBST-空闲事务持锁阻塞", "rule:GS-XACT-001"]),
    dict(q="CONN_HIGH 连接数 1650/2000 超过 80%", expect=["case:S2-20250725-CBST-连接数逼近上限", "rule:GS-GUC-004", "rule:GS-CONN-001"]),
    dict(q="SLOWSQL_TOP 慢 SQL 存储过程 proc_daily_settle 逐条 commit", expect=["case:S2-20250303-CBST-存过循环单条提交慢"]),
    dict(q="空闲事务持锁阻塞了很多会话怎么办", expect=["case:S2-20250120-CBST-空闲事务持锁阻塞"]),
    dict(q="有没有分区裁剪失效的案例", expect=["case:S2-20250115-CBMS-分区裁剪失效"]),
    dict(q="存过循环里单条提交很慢", expect=["case:S2-20250303-CBST-存过循环单条提交慢"]),
    # 八类知识(2026-10-24):手册 / 基线 / 缺陷也要能被问到
    dict(q="阻塞链根会话要不要直接终止 需要谁审批", expect=["runbook:RB-LCK-001"]),
    dict(q="CBST 活跃会话数平时多少算正常", expect=["baseline:BL-CBST-ACTIVE_SESSIONS"]),
    dict(q="存储过程 EXCEPTION 块 LockMgrLock 争用 是不是已知缺陷", expect=["defect:KD-0001"]),
    dict(q="WAL 归档失败 archive_command 报错怎么办", expect=[]),
    dict(q="备机复制延迟 REPL_LAG 30 秒", expect=[]),
]


def case_id(c: dict) -> str:
    return f"{c['sev']}-{c['date'].replace('-', '')}-{c['sys']}-{c['title']}"


def write_case(out: pathlib.Path, c: dict) -> str:
    cid = case_id(c)
    front = {
        "id": cid, "title": c["title"], "system": c["sys"], "instance": "未知", "occurred_at": c["date"],
        "engine": "gaussdb", "severity": c["sev"], "primary_factor": c["primary"], "secondary_factors": [],
        "conclusion": c["conclusion"], "source": f"sources/{c['sys']}-问题分析报告-{c['date']}.docx#前言",
        "entered_by": "sample", "entered_at": "2026-09-02",
        "objects": c["objects"], "signals": c["signals"], "rules": c["rules"],
    }
    body = "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip() + "\n---\n"
    body += f"## 现场\n{c['scene']}\n## 判断\n{c['judge']}\n## 处置\n{c['act_text']}\n## 复发标志\n{c['recur']}\n"
    if c.get("disc"):                     # 看起来相同、根因不同:区分证据(2026-10-24)
        body += f"## 区分证据\n{c['disc']}\n"
    (out / "cases" / f"{cid}.md").write_text(body, encoding="utf-8")
    return cid


def triples_for(c: dict, cid: str) -> list:
    conf = {"已确认": 1.0, "推测": 0.6, "待验证": 0.3}[c["conclusion"]]
    return [
        {"src": {"kind": "case", "name": c["title"], "canonical": f"case:{cid}"}, "rel": "exhibits",
         "dst": {"kind": "symptom", "name": c["sym"]}, "confidence": conf, "status": "accepted",
         "source": f"cases/{cid}.md#现场", "case": cid, "valid_from": c["date"]},
        {"src": {"kind": "symptom", "name": c["sym"]}, "rel": "caused_by",
         "dst": {"kind": "rootcause", "name": c["root"]}, "confidence": conf, "status": "accepted",
         "source": f"cases/{cid}.md#判断", "case": cid, "valid_from": c["date"]},
        {"src": {"kind": "rootcause", "name": c["root"]}, "rel": "handled_by",
         "dst": {"kind": "action", "name": c["act"]}, "confidence": conf, "status": "accepted",
         "source": f"cases/{cid}.md#处置", "case": cid, "valid_from": c["date"]},
    ]


def build(out: pathlib.Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    for sub in ("cases", "graph", "rules", "guides", "errata", "archive", "sources", "inbox", "eval"):
        (out / sub).mkdir(parents=True)
    triples = []
    for c in CASES:
        cid = write_case(out, c)
        triples += triples_for(c, cid)
    (out / "graph" / "sample.yaml").write_text(yaml.safe_dump(triples, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (out / "graph" / "canonical.yaml").write_text(yaml.safe_dump(CANONICAL, allow_unicode=True, sort_keys=True), encoding="utf-8")
    for fname, (comment, entries) in RULES.items():
        (out / "rules" / fname).write_text(f"# {comment}\n" + yaml.safe_dump(entries, allow_unicode=True, sort_keys=False), encoding="utf-8")
    write_typed(out)
    (out / "eval" / "queries.yaml").write_text(yaml.safe_dump(EVAL, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (out / "VERSION").write_text("2026.09\n", encoding="utf-8")
    (out / "README.md").write_text(
        "# 示例知识库(全部虚构)\n\n由 build_sample.py 生成:27 案例(含 5 个金丝雀、3 组现象相同根因不同的反例,带区分证据)、"
        "12 条规范条款、六类类型化条目各一两条(画像 / 手册 / 缺陷 / 基线 / 产品文档 / Wiki)、三元组与别名表、黄金查询集。\n\n用法:复制本目录为 `<kb>/`,按需写 `kb.yaml`(embeddings、可选的 Neo4j),"
        "然后 `kb.py setup && kb.py index && kb.py eval`。\n", encoding="utf-8")
    canaries = [case_id(c) for c in CASES if c.get("canary")]
    print(f"已生成 {out}:案例 {len(CASES)}(金丝雀 {len(canaries)}) · 三元组 {len(triples)} · "
          f"条款 {sum(len(v[1]) for v in RULES.values())} · 黄金查询 {len(EVAL)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(HERE / "sample-kb"))
    args = ap.parse_args(argv)
    build(pathlib.Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
