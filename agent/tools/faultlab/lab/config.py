"""faultlab 的全部常量与场景目录 —— 时间、并发、阈值、库对象名都只在这里出现一次。

**为什么单独一个文件:** 这套东西要反复跑、还要被人按自己的机器改(窗口想短一点、
并发想小一点)。常量散在各场景里的话,改一处就得翻五个文件,漏改一处就是「这次跑出来
和上次不一样」。

**时间口径:** 所有秒数都是「从故障窗口开始算起的偏移」,不是绝对时间。runner 按它排期。
"""
from __future__ import annotations

import pathlib
from typing import Dict, NamedTuple, Optional, Tuple

# ---------------------------------------------------------------- 连接与库对象

ROOT = pathlib.Path(__file__).resolve().parents[1]      # tools/faultlab
SQL_DIR = ROOT / "sql"
STATE_DIR = pathlib.Path.home() / ".faultlab"           # 口令、残留清单、时间线
CONN_FILE = STATE_DIR / "conn.json"
STATE_FILE = STATE_DIR / "state.json"

SCHEMA = "fl"                   # 所有库对象只在这个模式下
LAB_USER = "fl_app"             # 专用用户,purge 时删掉
APP_PREFIX = "fl:"              # application_name 前缀,status / cleanup 按它认人

# 工具**自己**的连接名(不造故障,只编排和观测)。cleanup 杀会话时必须跳过它们 ——
# 第一版没跳,结果收尾时把编排器自己的连接也杀了,紧接着那个「恢复后」的 WDR 快照
# 就报 server closed the connection unexpectedly(2026-10-17 全量跑时实测)。
INFRA_APPS = ("runner", "cleanup", "status", "check", "prepare", "calibrate", "explain", "fix", "stop")

# og5 的默认连法(容器内 5432 发布到 Mac 5433)。换机器用 ~/.faultlab/conn.json 覆盖。
DEFAULT_CONN = {
    "host": "127.0.0.1",
    "port": 5433,
    "dbname": "postgres",
    "container": "og5",         # 建用户 / 删用户要 omm 权限,经容器执行
}

# 实例指纹:prepare / run 前核对,对不上就拒绝动手。
# 防的是「以为连的是 og5、其实连到行方库上」—— 这套脚本会关 autovacuum、建复制槽、
# 做 VACUUM FULL,跑错库是事故。
EXPECT_FINGERPRINT = {
    "port": 5432,               # 容器内端口
    "version_like": "openGauss",
}

# ---------------------------------------------------------------- 时间与并发

WINDOW_S = 20 * 60              # 故障窗口:所有场景加起来 20 分钟(user 2026-10-17 定)
SNAPSHOT_EVERY_S = 4 * 60       # 窗口内每 4 分钟一个 WDR 快照,与轻量锁换段**严格对齐**
BASELINE_QUIET_S = 60           # 开始前的静默基线
SETTLE_S = 60                   # 清理后的恢复观察

# 并发上限。og5 是 4 核的容器,并发再高就全是 CPU 排队 —— 那时每个场景都「慢」,
# 反而分不出是哪一项造成的。12 条忙会话 ≈ 3 倍超配:库明显卡,但没被压垮。
MAX_CPU_SESSIONS = 12           # 真吃 CPU 的会话总数上限(等锁的会话不算在内)

# 每条会话自带的兜底超时(窗口 + 余量):编排器被 kill 了,故障也会自己到期。
GUARD_MARGIN_S = 10 * 60
STATEMENT_TIMEOUT_S = WINDOW_S + GUARD_MARGIN_S
IDLE_XACT_TIMEOUT_S = WINDOW_S + GUARD_MARGIN_S

# 等锁的兜底按**场景自己的时段**算,不按整个窗口(见 lock_guard_s)。2026-10-24 全量跑时
# K3 按排期 +700 秒停下,它那条排在长查询后面的 ALTER 却一直挂在 fl.payments 上 ——
# 停止位只能让 Python 循环退出,叫不醒一条正在等锁的 SQL;那时它的等锁上限还是「窗口 + 10 分钟」,
# 于是 S5、status、explain、health 跟着全被堵在 payments 上。
LOCK_GUARD_MARGIN_S = 60

# 观测类连接(status、编排器自己的 admin 连接、停场景用的连接)的超时:宁可这一项查不出来,
# 也不能让观测者自己排进别人的锁队列 —— K3 期间 status 的 pg_total_relation_size 就这样挂住过。
OBSERVER_STATEMENT_S = 30
OBSERVER_LOCKWAIT_S = 3
# 清理连接:VACUUM FULL 可能要跑很久,不限语句时长;等锁最多等一分钟(会话都已经杀过了)。
MAINT_LOCKWAIT_S = 60

# 停场景的三步:置停止位 → 等 STOP_GRACE_S 让会话自己收 → 还在跑的取消当前语句(pg_cancel_session)
# → 再等 CANCEL_GRACE_S → 还在的终止会话(pg_terminate_session)。只按 application_name 认自己的会话。
STOP_GRACE_S = 2.0
CANCEL_GRACE_S = 3.0

# 复制槽场景的 WAL 守卫:超过这个量立刻删槽(og5 盘是共享的,不能让它涨下去)
WAL_GUARD_BYTES = 2 * 1024 * 1024 * 1024

# ---------------------------------------------------------------- 数据规模

class Table(NamedTuple):
    """一张要造的表:行数与它存在的理由(手册直接引用 why)。"""
    name: str
    rows: int
    why: str


TABLES: Tuple[Table, ...] = (
    Table("customers", 200_000, "行方维表,其中 1 个大行方占 30% 订单 —— 数据倾斜的来源"),
    Table("products", 50_000, "商品维表"),
    Table("orders", 1_000_000, "订单主表,慢 SQL 的驱动表"),
    Table("order_items", 3_000_000, "订单明细,慢 SQL 里最大的那张表"),
    Table("payments", 1_000_000, "支付表"),
    Table("shipments", 1_000_000, "物流表"),
    Table("regions", 500, "地区维表(小表,专门用来让优化器选嵌套循环)"),
    Table("channels", 200, "渠道维表"),
    Table("promos", 2_000, "促销维表"),
    Table("warehouses", 300, "仓库维表"),
    Table("couriers", 800, "承运商维表"),
    Table("bloat_hot", 300_000, "V1:关掉 autovacuum 后持续更新,膨胀到 3 倍以上"),
    Table("bloat_slow", 200_000, "V5:autovacuum 被限速,追不上更新速度"),
    Table("xmin_hold", 100_000, "V2/V3:回收水位被长事务 / 两阶段事务卡住"),
    Table("acct", 2_000, "锁与死锁用的小表(余额表)"),
    Table("acct_child", 2_000, "外键子表:K6 / D5 的父子锁冲突"),
    Table("hotpage", 100, "L2:100 行挤在同一个页上,制造页内容锁争用(能用 ustore 就用,见 prepare.runtime_ddl)"),
    Table("wal_log", 0, "L1:提交风暴的落点,运行时写入、cleanup 清空"),
    Table("doc_blob", 0, "W1:大字段批量写入的落点(几 KB 随机正文进 TOAST),运行时写入、cleanup 清空"),
    Table("part_log", 100_000, "L4:1000 个分区,制造锁管理器争用"),
    Table("import_stage", 0, "V7:导入 50 万行后不做 ANALYZE,统计信息过期"),
    Table("xact_tick", 1_000, "L3:每条会话各改自己那一行的极短写事务(行分在不同页上)"),
    Table("subx_log", 0, "L5:每个子事务写一行,运行时写入、cleanup 清空"),
    Table("maint_hist", 200_000, "V6:VACUUM FULL 的对象(unlogged,带一条慢表达式索引,重建要两分钟)"),
    Table("dl_child", 100, "D3/D5:死锁场景自己的外键子表(不和 K4 锁着的 acct_child 抢)"),
)

# 运行期对象(runtime_ddl 建,不在 schema.sql 里):这几张表与 fl_slow_key 函数。
RUNTIME_TABLES = ("xact_tick", "subx_log", "maint_hist", "dl_child", "doc_blob")
DL_CHILD_ROWS = next(t.rows for t in TABLES if t.name == "dl_child")

BIG_CUSTOMER_ID = 1            # 占 30% 订单的那个大行方
PARTITIONS = 1000              # part_log 的分区数
IMPORT_ROWS = 500_000          # V7 一次导入的行数

# V7 / S5:import_stage 表级的 analyze 触发线拉到永远够不着。
# **光 autovacuum_enabled=false 挡不住自动 ANALYZE:** og5(openGauss 5.0.3,autovacuum_mode=mix)
# 上实测,关了 autovacuum 的表照样被 autovacuum 做 ANALYZE —— 2026-10-24 那轮 import_stage 导入
# 80 秒后就有了统计信息(last_autoanalyze=09:47:28),bloat_hot 在关着 autovacuum 的 20 分钟里被自动分析了
# 101 次。所以再加两条**表级**参数(实例级一个不动):触发线 = 阈值 + 比例 × 行数,永远到不了。
IMPORT_ANALYZE_THRESHOLD = 2_000_000_000
IMPORT_ANALYZE_SCALE = 100

# K5 / V6 用的「慢表达式」每行做几轮 md5(fl_slow_key 的第二个参数)。
# 目的是让建索引 / VACUUM FULL 重建索引**持续整个时段**,而不是 7 秒、10 秒就完事
# (2026-10-24 那轮实测:300 万行建普通索引 7 秒,VACUUM FULL 500 MB 的表 10 秒)。
# og5 上实测(2026-10-24,单线程、空闲时)每轮约 0.25 微秒,忙时更慢:
# K5 300 万行 × 200 轮 ≈ 150 秒(时段 130 秒);V6 20 万行 × 2400 轮 ≈ 120 秒一次。
# 跑不完没关系 —— 时段一到,停场景时会把那条 DDL 取消掉;太快才是问题(建完就不挡了)。
K5_SLOW_ITERS = 200
V6_SLOW_ITERS = 2400
MAINT_ROWS = next(t.rows for t in TABLES if t.name == "maint_hist")

# ---------------------------------------------------------------- 场景目录

class Scenario(NamedTuple):
    """一个场景。

    code     手册与 expected.yaml 里的编号(S1 / K1 / D1 / V1 / L1)
    group    分类,--only 按它过滤
    title    一句话说清造的是什么问题
    sessions 要开几条会话(含持锁者与等待者)
    cpu      这些会话里有几条是真吃 CPU 的 —— 用来守住 MAX_CPU_SESSIONS
    start_s  窗口内第几秒开始
    stop_s   第几秒结束(None = 跟窗口一起结束)
    """
    code: str
    group: str
    title: str
    sessions: int
    cpu: int
    start_s: int
    stop_s: int = 0             # 0 表示跟窗口一起结束


GROUPS = ("slow", "lock", "deadlock", "vacuum", "lwlock", "writes")

# 排期的三条规矩(改 start_s / stop_s 前先看这里):
#   ① 同时吃 CPU 的会话不超过 MAX_CPU_SESSIONS —— cpu_budget() 会算,runner 启动前检查;
#   ② **轻量锁五段严格按 WDR 快照边界切**(0/240/480/720/960/1200),这样每个快照窗口里
#      只有一种轻量锁争用,waitevent 的多窗口分解才看得出「这段是谁在争」;
#   ③ 吃 CPU 的锁 / VACUUM 场景彼此不重叠,一次只有一个 —— 否则两个重场景一起上,
#      两边都「慢」,分不清是谁造成的。
SLOW: Tuple[Scenario, ...] = (
    # 两条**跑到窗口结束都跑不完**的:它们负责「现在有一条 SQL 已经跑了十几分钟」
    # 这个现象(health / lockwait / waitevent 看的是正在执行的会话)。
    Scenario("S1", "slow", "相关子查询逐行执行(6 表;payments.order_id 没索引,每行扫一遍百万行表)", 1, 1, 20),
    Scenario("S2", "slow", "多层派生表 + LIKE 前缀关联(7 表 3 层,索引用不上、哈希聚合在 4MB work_mem 下落盘,跑不完)", 1, 1, 30),
    # 四条**能在窗口内跑完**的:它们负责「执行历史里有几分钟到十几分钟的 SQL」
    # (topsql / slowsql / wdr 读 dbe_perf 的统计,只统计**已完成**的执行 —— 一直没跑完的
    # S1 / S2 在那里一行都没有)。分两条车道依次上,同时最多两条在跑。
    #   车道 A(长,目标 8 分钟):S3 → S5
    #   车道 B(短,目标 3 分钟):S4 → S6
    Scenario("S3", "slow", "NOT IN 碰上可空列(5 表,无法改写成反连接,退化成逐行子计划)", 1, 1, 60, 660),
    Scenario("S5", "slow", "统计信息过期 → 选错嵌套循环(5 表,估几千行实际 50 万行)", 1, 1, 660, 1200),
    Scenario("S4", "slow", "列上套类型转换让索引失效(6 表,customers.code::bigint 每行全表扫)", 1, 1, 240, 480),
    Scenario("S6", "slow", "窗口函数 + 大排序在 1MB work_mem 下落盘(5 表)", 1, 1, 720, 960),
    # 一条高频的中等慢 SQL:让 Top SQL 的「总耗时 / 调用次数」榜首与「单条最慢」分开。
    # 实测单次只有零点几秒(orders 全表扫,数据都在缓存里)。它的意义**不在单次耗时**,
    # 而在次数:20 分钟能跑几千次,把「总耗时榜首」和「单条最慢」拉开。
    Scenario("S7", "slow", "高频点查(cust_code 无索引,每次全表扫,单次零点几秒、不停地跑)", 1, 1, 40),
)

# 锁等待:等待者只是挂在那里等,不吃 CPU(cpu=0);真干活的那几项彼此不重叠。
LOCK: Tuple[Scenario, ...] = (
    Scenario("K1", "lock", "行锁阻塞链:根会话改完行后空闲不提交,4 层链、末端挂 3 个等待者", 6, 0, 15),
    Scenario("K10", "lock", "孤儿两阶段事务:PREPARE 后会话退出,锁还在、杀会话也杀不掉", 1, 0, 20),
    Scenario("K4", "lock", "月结任务 LOCK TABLE IN EXCLUSIVE MODE,锁 2 分钟放 1 分钟", 3, 0, 180),
    Scenario("K6", "lock", "外键父子表互相等锁", 2, 0, 200),
    Scenario("K7", "lock", "多个会话插入同一个主键,等第一个事务的结果", 3, 0, 220),
    Scenario("K8", "lock", "应用层咨询锁(advisory):持有者卡住,后面排队", 4, 0, 240),
    Scenario("K2", "lock", "批处理 SELECT … FOR UPDATE 分批锁行(acct 1001~2000,与别的场景不重叠),在线更新间歇性等锁(抖动型)", 3, 1, 120, 420),
    Scenario("K5", "lock", "不带 CONCURRENTLY 建索引(慢表达式,整个时段都在建),300 万行表的写入全被挡住", 3, 1, 430, 560),
    Scenario("K3", "lock", "DDL 锁队列放大:长查询挡住 ALTER TABLE,DDL 又挡住后面所有读写", 4, 1, 570, 700),
    Scenario("K9", "lock", "多会话往同一张表批量插入,抢表扩展锁(extend)", 2, 2, 1060),
)

# 死锁:一个驱动会话轮流演 6 种形态(DEADLOCK_FORMS),约 30 秒一次。
# **为什么只算一个排期单元:** 每种形态只活几秒钟,按 6 项分别排期会把 CPU 预算算虚高。
DEADLOCK: Tuple[Scenario, ...] = (
    Scenario("D", "deadlock", "六种死锁形态轮流制造,约 30 秒一次(20 分钟约 35 次)", 3, 1, 60),
)
DEADLOCK_FORMS: Tuple[Tuple[str, str], ...] = (
    ("D1", "两会话交叉更新同表两行"),
    ("D2", "三会话成环:A 等 B、B 等 C、C 等 A"),
    ("D3", "跨表加锁顺序相反"),
    ("D4", "锁升级:两会话都先 FOR SHARE 同一行,再都去 UPDATE"),
    ("D5", "外键引起:子表插入锁父行,与父行更新交叉"),
    ("D6", "唯一索引按相反顺序插入"),
)
DEADLOCK_PERIOD_S = 30

# VACUUM:前四项是「状态型」(建好就一直成立,不吃 CPU),后三项要干活、依次上。
VACUUM: Tuple[Scenario, ...] = (
    # V4 必须**最先**起:逻辑复制槽建立时要等当时所有已分配事务号的事务结束,
    # 排在 K1/K10/V2/V3 后面的话,孤儿两阶段事务永远不结束,建槽就永远挂着。
    Scenario("V4", "vacuum", "逻辑复制槽无人消费,钉住 catalog_xmin、WAL 只增不删(带 WAL 守卫)", 1, 0, 2),
    # V7 的导入只要 1 秒,CPU 不计;排到窗口结束是为了每分钟自证一次「统计信息还没被自动收集」。
    Scenario("V7", "vacuum", "批量导入 50 万行后不做 ANALYZE,统计信息停在 0 行(S5 的成因)", 1, 0, 5),
    Scenario("V1", "vacuum", "关掉 autovacuum 的表持续更新 → 死元组过半、膨胀数倍", 1, 1, 10),
    Scenario("V2", "vacuum", "长事务(可重复读快照)卡住回收水位,autovacuum 跑了也清不掉", 1, 0, 25),
    Scenario("V3", "vacuum", "孤儿两阶段事务卡住另一张表的回收水位", 1, 0, 45),
    Scenario("V5", "vacuum", "autovacuum 被表级限速:开着、一轮轮在跑,却追不上更新速度", 1, 1, 710, 900),
    Scenario("V6", "vacuum", "业务高峰做维护:VACUUM FULL 一张带慢表达式索引的表,整个时段堵住它的读写", 3, 1, 910, 1050),
)

# 轻量锁:五段,边界与 WDR 快照严格对齐(见上面规矩 ②)。每段 3 条会话。
LWLOCK: Tuple[Scenario, ...] = (
    Scenario("L1", "lwlock", "提交风暴:3 会话单行插入 + 每条即提交 → WAL 相关轻量锁", 3, 3, 30, 240),
    Scenario("L2", "lwlock", "热点页:3 会话异步提交、各改同一页上的不同行(ustore 原地更新)→ 页内容锁争用", 3, 3, 240, 480),
    Scenario("L3", "lwlock", "极短写事务高频提交(异步提交,能在服务端循环就在服务端循环)→ 事务号 / 快照相关", 3, 3, 480, 720),
    Scenario("L4", "lwlock", "1000 分区表:每条语句锁 200 个分区、语句级提交 → 锁管理器争用", 3, 3, 720, 960),
    Scenario("L5", "lwlock", "2 会话每事务数百个写子事务 + 1 会话并发读 → 子事务 / CLog 相关", 3, 3, 960, 1200),
)

# 写入:行方原话里的例子「大量大字段写入导致 xlog 膨胀迅速」(2026-11-05 加)。排在 WAL 那一段(L1 提交风暴,30~240 秒)里:
# 两者都是 WAL 主题,不搅乱别的轻量锁段;一条会话。体量有上限(W1_MAX_BYTES),到了就停写,不把共用的测试库写满。
WRITES: Tuple[Scenario, ...] = (
    Scenario("W1", "writes", "大字段批量写入:几 KB 随机正文一批批插入、每批即提交 → xlog 涨得快、checkpoint 被频繁强制触发", 1, 1, 60, 200),
)
W1_BATCH_ROWS = 300                       # 每批行数(每行约 5 KB 随机文本)
# doc_blob 长到这么大就停写(连 TOAST)。不能大:V4 的复制槽整个窗口都钉着 WAL,槽落后超过 WAL_GUARD_BYTES(2 GiB)
# 守卫就会删槽,V4 那一项就看不到了;300 MB 的大字段约产生 0.5 GB WAL,加上别的场景仍在守卫线以内。
W1_MAX_BYTES = 300 << 20

ALL_SCENARIOS: Tuple[Scenario, ...] = SLOW + LOCK + DEADLOCK + VACUUM + LWLOCK + WRITES


def by_group(groups: Tuple[str, ...]) -> Tuple[Scenario, ...]:
    """按分类过滤场景。空 groups = 全要。"""
    if not groups:
        return ALL_SCENARIOS
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise ValueError("不认识的分类 %s;可选:%s" % (", ".join(unknown), ", ".join(GROUPS)))
    return tuple(s for s in ALL_SCENARIOS if s.group in groups)


def by_codes(codes: Tuple[str, ...]) -> Tuple[Scenario, ...]:
    """按编号挑场景(`run --codes K5,V6`),保持排期表里的顺序。不认识的编号直接报错。"""
    known = {s.code for s in ALL_SCENARIOS}
    unknown = [c for c in codes if c not in known]
    if unknown:
        raise ValueError("不认识的场景编号 %s;可选:%s" % (", ".join(unknown), ", ".join(sorted(known))))
    return tuple(s for s in ALL_SCENARIOS if s.code in codes)


def rebase(scenarios: Tuple[Scenario, ...], lead_s: int = 5) -> Tuple[Scenario, ...]:
    """把挑出来的几项整体前移,让最早的那项在窗口第 lead_s 秒起跑,时长与先后不变。

    **为什么要它:** 在共用的 og5 上验证某一项能不能造出来时,不该为了等 V6(排在 +910 秒)
    陪跑十几分钟。stop_s=0(跟窗口一起结束)保持 0。
    """
    if not scenarios:
        return scenarios
    shift = min(s.start_s for s in scenarios) - lead_s
    return tuple(s._replace(start_s=s.start_s - shift,
                            stop_s=(s.stop_s - shift) if s.stop_s else 0) for s in scenarios)


def lock_guard_s(meta: Optional[Scenario], window_s: int = WINDOW_S) -> int:
    """这个场景的会话最多等一把锁多久:场景自己的时段 + 一点余量。

    停场景时本来就会取消挂着的语句(session.stop_workers);这是那一步失灵时的第二道保险 ——
    最迟在时段结束后 LOCK_GUARD_MARGIN_S 秒,数据库自己把这条等锁的语句掐掉。
    """
    if meta is None:
        return STATEMENT_TIMEOUT_S
    end = min(meta.stop_s or window_s, window_s)
    return max(0, end - meta.start_s) + LOCK_GUARD_MARGIN_S


def cpu_budget(scenarios: Tuple[Scenario, ...]) -> Dict[str, int]:
    """任意时刻同时在跑的吃 CPU 会话数的峰值,与上限一起返回。

    排期表是手写的,改了 start_s / stop_s 很容易不知不觉把 og5 压死。
    这个函数让它变成一条可断言的数 —— runner 启动前检查,单测也钉它。
    """
    marks = sorted({s.start_s for s in scenarios} | {(s.stop_s or WINDOW_S) for s in scenarios})
    peak = 0
    for t in marks:
        live = sum(s.cpu for s in scenarios if s.start_s <= t < (s.stop_s or WINDOW_S))
        peak = max(peak, live)
    return {"peak": peak, "limit": MAX_CPU_SESSIONS}
