"""VACUUM 相关(V1–V7):死元组、膨胀、回收水位被卡、限速追不上、维护堵业务、统计信息过期。

**一条贯穿的主线:死元组清不掉的原因不止一个,而解法完全不同:**
  · V1 autovacuum 被关掉 —— 开回去就好;
  · V2 长事务持着旧快照 —— 要去找那笔事务,改 autovacuum 没用;
  · V3 两阶段事务 —— 连会话都没有,只能 ROLLBACK PREPARED;
  · V4 复制槽没人消费 —— 要删槽或让备机跟上;
  · V5 限速太狠 —— 调 cost_delay / cost_limit。
  诊断工具的价值就在于**分清是哪一种**:报一句「有死元组」等于没说。

**V2/V3/V4 都是「状态型」的:** 建好之后一直成立、几乎不耗 CPU。它们留下的东西不会随
会话结束消失,所以全部登记进 state,由 cleanup 收。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .. import config as C
from .. import state
from ..db import Conn
from ..session import sleep_until, until
from .base import Base, register

HOLD_POLL_S = 0.5


def _dead_ratio(admin: Conn, table: str) -> str:
    row = admin.query(
        "SELECT n_live_tup, n_dead_tup, "
        "       CASE WHEN n_live_tup + n_dead_tup > 0 "
        "            THEN round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 1) ELSE 0 END, "
        "       pg_size_pretty(pg_total_relation_size(%s)) "
        "  FROM pg_stat_user_tables WHERE schemaname = %s AND relname = %s",
        ("%s.%s" % (C.SCHEMA, table), C.SCHEMA, table))
    if not row:
        return "查不到 %s 的统计" % table
    live, dead, pct, size = row[0]
    return "存活 %s、死元组 %s(%s%%)、占用 %s" % (live, dead, pct, size)


class _Updater(Base):
    """反复更新同一张表的同一批行 —— 每次更新都留下一个死元组。"""

    table = ""
    rows = 0

    def _update(self, c: Conn, stop: threading.Event) -> None:
        pos = [0]

        def step() -> None:
            pos[0] = (pos[0] + 1) % max(1, self.rows)
            c.exec("UPDATE %s.%s SET cnt = cnt + 1, touched_at = now() "
                   "  WHERE id BETWEEN %%s AND %%s" % (C.SCHEMA, self.table),
                   (pos[0] + 1, pos[0] + 200))

        # **更新失败要接着干,不能停。** V6 对同一张表做 VACUUM FULL 时会把表文件整个重写,
        # 正在更新它的会话会收到 `could not seek to end of file …`(2026-10-17 全量跑时实测)。
        # 这在现场就是真实后果 —— 业务 SQL 当场报错 —— 但它不该让造膨胀这件事停下来,
        # 否则 VACUUM FULL 之后死元组就不再增长,V1 的现象也跟着消失了。
        until(stop, step, period_s=0.05,
              on_error=lambda exc: self.ctx.bump("%s_update_errors" % self.code))


@register("V1")
class BloatNoAutovacuum(_Updater):
    """关掉表级 autovacuum,然后一直更新 → 死元组过半、表膨胀数倍。

    现实里的样子:有人为了「别让 vacuum 影响业务」把某张热表的 autovacuum 关了,
    然后忘了。几周后这张表的扫描慢了好几倍,查询计划也开始走偏。
    """

    group = "vacuum"
    table = "bloat_hot"
    rows = 300_000

    def start(self) -> None:
        c = Conn(self.code + "/setup")
        try:
            c.exec("ALTER TABLE %s.%s SET (autovacuum_enabled = false)" % (C.SCHEMA, self.table))
            state.add_residue("table_option", "%s.%s:autovacuum_enabled" % (C.SCHEMA, self.table))
            before = c.one("SELECT pg_total_relation_size('%s.%s')" % (C.SCHEMA, self.table))
            self.log("已关掉 %s 的 autovacuum,起始大小 %s 字节" % (self.table, before))
        finally:
            c.close()
        self.worker("upd", self._update)

    def probe(self, admin: Conn) -> str:
        return _dead_ratio(admin, self.table)


@register("V5")
class BloatThrottled(_Updater):
    """autovacuum 开着,但被表级参数限速 → 它一轮轮在跑,却追不上更新速度。

    **和 V1 的区别就是这套工具要分清的事:** 这里 autovacuum 是**开着**的,
    `last_autovacuum` 一直在更新,但死元组照样涨。只看「autovacuum 开没开」会误判成正常。

    **限速不能狠到一轮都跑不完。** 原来是 cost_delay=100ms / cost_limit=10:每秒只有 100 点配额,
    一轮要扫几千个页,3 分钟里一轮也没跑完 —— `last_autovacuum` 只在一轮**结束**时更新,
    于是一直是「从未运行」,看起来和 V1 关掉了一样(2026-10-24 全量跑,autovacuum_count 始终 0)。
    现在给默认速度的三成(cost_delay=20ms / cost_limit=60):一轮几十秒,时段内能跑完好几轮。
    (十分之一也试过:og5 上单跑 V5 时,表在 3 分钟里胀到 112 MB,第一轮要 170 秒才跑完,
    时段内只更新了一次 last_autovacuum。)
    死元组照样清不掉 —— 回收水位被 V2/V3/K10 卡着,这本来也是现场「限速 + 长事务」叠在一起的样子。
    """

    group = "vacuum"
    table = "bloat_slow"
    rows = 200_000
    COST_DELAY_MS = 20
    COST_LIMIT = 60

    def start(self) -> None:
        c = Conn(self.code + "/setup")
        try:
            c.exec("ALTER TABLE %s.%s SET (autovacuum_enabled = true, "
                   "  autovacuum_vacuum_cost_delay = %d, autovacuum_vacuum_cost_limit = %d, "
                   "  autovacuum_vacuum_scale_factor = 0.001)"
                   % (C.SCHEMA, self.table, self.COST_DELAY_MS, self.COST_LIMIT))
            state.add_residue("table_option", "%s.%s:autovacuum_cost" % (C.SCHEMA, self.table))
            self._base = int(c.one("SELECT coalesce(autovacuum_count, 0) FROM pg_stat_user_tables "
                                   " WHERE schemaname = %s AND relname = %s", (C.SCHEMA, self.table)) or 0)
            self.log("已把 %s 的 autovacuum 限速到 cost_delay=%dms / cost_limit=%d"
                     % (self.table, self.COST_DELAY_MS, self.COST_LIMIT))
        finally:
            c.close()
        self.worker("upd", self._update)

    def probe(self, admin: Conn) -> str:
        row = admin.query(
            "SELECT to_char(last_autovacuum, 'HH24:MI:SS'), coalesce(autovacuum_count, 0) "
            "  FROM pg_stat_user_tables WHERE schemaname = %s AND relname = %s", (C.SCHEMA, self.table))
        last, count = (row[0] if row else (None, 0))
        rounds = int(count or 0) - getattr(self, "_base", 0)
        verdict = "开着、跑了 %d 轮还追不上" % rounds if rounds > 0 else "这段时间 autovacuum 还没跑完一轮"
        return "%s;最近一次 autovacuum %s(%s)" % (_dead_ratio(admin, self.table), last or "从未", verdict)


@register("V2")
class LongTransactionHold(Base):
    """长事务持着可重复读快照 → 它开始之后产生的死元组一个都清不掉。

    autovacuum 会照常跑完、`last_autovacuum` 照常更新,但死元组纹丝不动 ——
    因为那笔老事务还可能要看到旧版本。日志里的说法是「… cannot be removed yet」。
    """

    group = "vacuum"
    table = "xmin_hold"

    def start(self) -> None:
        self.worker("holder", self._hold)
        self.worker("upd", self._churn)

    def _hold(self, c: Conn, stop: threading.Event) -> None:
        c.exec("START TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # **核对隔离级别真的生效了。** 可重复读的快照是这个场景成立的唯一前提:
        # 退化成读已提交的话,每条语句都取新快照,回收水位压根不住,而死元组照样会涨
        # (autovacuum 还没来得及跑)—— 看起来和成功一模一样。
        level = str(c.one("SELECT current_setting('transaction_isolation')") or "")
        if "repeatable" not in level.lower():
            raise RuntimeError("隔离级别是 %s,不是可重复读 —— V2 不成立,不往下演" % level)
        c.query("SELECT count(*) FROM %s.%s" % (C.SCHEMA, self.table))   # 快照在这里定下来
        self.log("长事务已持住快照(%s),之后的死元组都清不掉" % level)
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)
        c.exec("ROLLBACK")

    def _churn(self, c: Conn, stop: threading.Event) -> None:
        pos = [0]

        def step() -> None:
            pos[0] = (pos[0] + 1) % 100_000
            c.exec("UPDATE %s.%s SET cnt = cnt + 1 WHERE id BETWEEN %%s AND %%s"
                   % (C.SCHEMA, self.table), (pos[0] + 1, pos[0] + 100))
        until(stop, step, period_s=0.2)

    def probe(self, admin: Conn) -> str:
        age = admin.one(
            "SELECT extract(epoch FROM now() - xact_start)::int FROM pg_stat_activity "
            " WHERE application_name = %s AND sessionid <> 0", (C.APP_PREFIX + "V2/holder",))
        return "%s;长事务已持续 %s 秒" % (_dead_ratio(admin, self.table), age)


@register("V3")
class PreparedXactHold(Base):
    """两阶段事务卡住回收水位:连会话都没有,只有 pg_prepared_xacts 里一条记录。

    和 V2 的差别:V2 至少能在 pg_stat_activity 里找到那笔长事务;这里什么会话都找不到,
    只能靠 pg_prepared_xacts。**现场遇到过「找不到长事务却清不掉死元组」的工单就是这种。**
    """

    group = "vacuum"
    GID = "fl_xmin_v3"
    table = "xmin_hold"

    def start(self) -> None:
        self.worker("prep", self._prepare)

    def _prepare(self, c: Conn, stop: threading.Event) -> None:
        c.exec("BEGIN")
        c.exec("UPDATE %s.%s SET cnt = cnt + 1 WHERE id = 1" % (C.SCHEMA, self.table))
        c.exec("PREPARE TRANSACTION '%s'" % self.GID)
        state.add_residue("prepared_xact", self.GID)
        self.log("已 PREPARE %s —— 回收水位从此卡住" % self.GID)

    def probe(self, admin: Conn) -> str:
        n = admin.one("SELECT count(*) FROM pg_prepared_xacts WHERE gid = %s", (self.GID,))
        return "两阶段事务 %s 条(查 pg_stat_activity 是找不到它的)" % n


@register("V4")
class ReplicationSlotHold(Base):
    """建一个没人消费的复制槽 → WAL 只增不删,回收水位(catalog_xmin)也被它压住。

    **带守卫:** 槽一旦留着,WAL 会一直涨。og5 的盘是和别的测试共用的,所以这里起一条
    看门线程,每分钟看一次 WAL 目录大小,超过 WAL_GUARD_BYTES 就立刻删槽并记一行日志。
    宁可让这个场景提前结束,也不能把别人的测试环境搞坏。

    **为什么改成逻辑槽:** 原来建的是物理槽,没有备机连上来,它的 restart_lsn、xmin 一直是空的 ——
    既不留 WAL 也不钉水位,等于什么都没造(2026-10-24 全量跑,V4 自证一直是 restart_lsn=None)。
    逻辑槽一建好就有 restart_lsn 与 catalog_xmin(og5 的 wal_level=logical)。
    逻辑槽建立时要等当时所有已分配事务号的事务结束,所以 V4 排在最前(+2 秒),
    并给建槽语句 30 秒上限;建不成就退回「物理槽 + 推进到当前位置」,再不成如实说没造出来。
    """

    group = "vacuum"
    SLOT = "fl_slot_v4"
    PLUGIN = "mppdb_decoding"            # openGauss 自带的逻辑解码插件
    CREATE_TIMEOUT_S = 30

    def start(self) -> None:
        c = Conn(self.code + "/setup")
        try:
            c.set("statement_timeout", "%ds" % self.CREATE_TIMEOUT_S)
            c.exec("SELECT pg_drop_replication_slot(%s) WHERE EXISTS "
                   "  (SELECT 1 FROM pg_replication_slots WHERE slot_name = %s)",
                   (self.SLOT, self.SLOT))
            state.add_residue("slot", self.SLOT)       # 先登记:建到一半被打断也有人收
            self.log("已建复制槽 %s(%s,无人消费)" % (self.SLOT, self._create(c)))
        finally:
            c.close()
        self.worker("guard", self._guard)

    def _create(self, c: Conn) -> str:
        """先建逻辑槽;不行就物理槽 + 推进到当前 WAL 位置(让它有 restart_lsn)。返回建成了哪种。"""
        try:
            c.exec("SELECT * FROM pg_create_logical_replication_slot(%s, %s)", (self.SLOT, self.PLUGIN))
            return "逻辑槽,插件 %s" % self.PLUGIN
        except Exception as exc:                 # noqa: BLE001
            why = str(exc).splitlines()[0][:80]
            c.rollback()
        # openGauss 的物理槽要两个参数(第二个是「是否 dummy standby」)
        c.exec("SELECT pg_create_physical_replication_slot(%s, false)", (self.SLOT,))
        try:
            c.exec("SELECT pg_replication_slot_advance(%s, pg_current_xlog_location()::text)", (self.SLOT,))
            return "逻辑槽建不成(%s),退回物理槽并推进到当前位置" % why
        except Exception as exc:                 # noqa: BLE001
            c.rollback()
            return ("逻辑槽建不成(%s),物理槽也推进不了(%s)—— 槽在但不留 WAL、不钉水位,V4 这次没造出来"
                    % (why, str(exc).splitlines()[0][:60]))

    def _guard(self, c: Conn, stop: threading.Event) -> None:
        # **看增量,不看总量。** 第一版比的是 pg_ls_waldir() 的总和,而 og5 上本来就
        # 存着 8.8 GB 的 WAL —— 守卫在建槽后几十秒就触发,把场景自己掐了
        # (2026-10-17 全量跑时 +95 秒就被删了)。要防的是「这个槽让 WAL 涨多少」,
        # 所以记下基线,只盯增量。
        base = c.one("SELECT sum(size) FROM pg_ls_waldir()") if self._has_waldir(c) else None
        if base is None:
            self.log("这个内核没有 pg_ls_waldir(),WAL 守卫无法工作 —— 槽会留到清理时才删")
            return
        base = int(base)

        def check() -> None:
            used = c.one("SELECT sum(size) FROM pg_ls_waldir()")
            if used is None:
                return
            grown = int(used) - base
            if grown > C.WAL_GUARD_BYTES:
                c.exec("SELECT pg_drop_replication_slot(%s)", (self.SLOT,))
                state.drop_residue("slot", self.SLOT)
                self.log("槽建立以来 WAL 涨了 %s 字节,超过守卫阈值 —— 已主动删掉复制槽" % grown)
                stop.set()
        until(stop, check, period_s=60)

    @staticmethod
    def _has_waldir(c: Conn) -> bool:
        return bool(c.one("SELECT count(*) FROM pg_proc WHERE proname = 'pg_ls_waldir'"))

    def probe(self, admin: Conn) -> str:
        row = admin.query(
            "SELECT slot_type, active, restart_lsn, xmin, catalog_xmin, "
            "       CASE WHEN restart_lsn IS NULL THEN NULL "
            "            ELSE pg_xlog_location_diff(pg_current_xlog_location(), restart_lsn)::bigint END "
            "  FROM pg_replication_slots WHERE slot_name = %s", (self.SLOT,))
        if not row:
            return "槽已不在(可能被守卫删了,见时间线)"
        kind, active, lsn, xmin, cxmin, held = row[0]
        if lsn is None and xmin is None and cxmin is None:
            return "槽 %s 在(%s、active=%s),但 restart_lsn / xmin / catalog_xmin 全空 —— 不留 WAL、不钉水位,没造出来" % (
                self.SLOT, kind, active)
        # 这个数是 restart_lsn 之后产生的 WAL 量 —— 槽在,这些就都不能回收;
        # 不等于 pg_xlog 比平时多出来的量(og5 的 checkpoint_segments=256,平时就留着好几 GB)。
        return "槽 %s(%s、active=%s)钉着 WAL:restart_lsn 之后已产生 %s MB,xmin=%s、catalog_xmin=%s" % (
            self.SLOT, kind, active, (int(held) // 1048576) if held is not None else "?", xmin, cxmin)


@register("V7")
class ImportNoAnalyze(Base):
    """批量导入 50 万行、**导完不收集统计信息** —— S5 的成因。

    这一项本身耗时很短(实测不到 1 秒),它造的是一个**状态**:统计信息与实际数据
    差几十倍。表级 autovacuum 关着、analyze 触发线拉到了永远够不着(prepare.import_stage_options_sql;
    光关 autovacuum 挡不住自动 ANALYZE),所以这个状态不会被自动修正。
    """

    group = "vacuum"

    def start(self) -> None:
        self._imported_at: Optional[str] = None
        self.worker("import", self._import)

    def _import(self, c: Conn, stop: threading.Event) -> None:
        from ..prepare import import_batch
        import_batch(c)
        self._imported_at = str(c.one("SELECT to_char(now(), 'YYYY-MM-DD HH24:MI:SS.US')"))
        self.log("已导入 %d 行,**没有** ANALYZE" % C.IMPORT_ROWS)

    def probe(self, admin: Conn) -> str:
        # 只算导入**之后**的自动 ANALYZE —— 之前那些(上一轮留下的时间戳)与这一轮无关
        row = admin.query(
            "SELECT c.reltuples::bigint, "
            "       CASE WHEN s.last_autoanalyze > %s::timestamptz "
            "            THEN to_char(s.last_autoanalyze, 'HH24:MI:SS') END "
            "  FROM pg_class c JOIN pg_stat_user_tables s ON s.relid = c.oid "
            " WHERE s.schemaname = %s AND s.relname = 'import_stage'",
            (getattr(self, "_imported_at", None) or "infinity", C.SCHEMA))
        real = admin.one("SELECT count(*) FROM %s.import_stage" % C.SCHEMA)
        est, auto = row[0] if row else (None, None)
        # 自动 ANALYZE 抢先过(上次就是这样)的话要如实说:那时统计已经准了,S5 的错估不会出现
        note = ";**已被自动 ANALYZE(%s)—— 统计不再过期,S5 不成立**" % auto if auto else ""
        return "统计信息里 %s 行、实际 %s 行%s" % (est, real, note)


@register("V6")
class VacuumFullBlocks(Base):
    """业务高峰做维护:VACUUM FULL 一张表,这张表的读写全被挡住。

    VACUUM FULL 要整表排他锁、还要重写整张表 —— 空间是收回来了,代价是这段时间里
    谁也别想碰它。现实里的样子:发现表膨胀,白天就手工 VACUUM FULL,业务当场中断。

    **对象换成了 maint_hist,而且整个时段反复做。** 原来对 bloat_hot 做一次,10 秒就完
    (2026-10-24 全量跑),排查的人基本赶不上;想让它变长只能让表更大,而 bloat_hot 被回收水位
    卡着,重写一次就是 500 多 MB 的 WAL(V4 的槽正钉着 WAL)。maint_hist 是 unlogged 表、
    带一条慢表达式索引:重写不产生 WAL,重建那条索引要两分钟上下。时段一到,停场景时把
    正在做的那次取消。代价是不再对 bloat_hot 做前后对比 —— 那本来也做不成:回收水位被
    V2/V3/K10 卡着,VACUUM FULL 也收不回那些死元组(上次实测做完反而更大)。
    """

    group = "vacuum"
    table = "maint_hist"

    def start(self) -> None:
        self.worker("maint", self._maint, expect_errors=True)
        self.worker("reader", self._read, expect_errors=True)
        self.worker("writer", self._write, expect_errors=True)

    def _maint(self, c: Conn, stop: threading.Event) -> None:
        c.set("query_dop", "1")

        def once() -> None:
            t0 = time.time()
            self.log("开始 VACUUM FULL %s(期间这张表读写都进不去)" % self.table)
            c.exec("VACUUM FULL %s.%s" % (C.SCHEMA, self.table))
            self.log("VACUUM FULL 做完一次,%.0f 秒;接着再做" % (time.time() - t0))
        until(stop, once, period_s=1.0)

    def _read(self, c: Conn, stop: threading.Event) -> None:
        until(stop, lambda: c.query("SELECT count(*) FROM %s.%s WHERE id < 100"
                                    % (C.SCHEMA, self.table)), period_s=3)

    def _write(self, c: Conn, stop: threading.Event) -> None:
        pos = [0]

        def touch() -> None:
            pos[0] = pos[0] % C.MAINT_ROWS + 1
            c.exec("UPDATE %s.%s SET cnt = cnt + 1 WHERE id = %%s" % (C.SCHEMA, self.table), (pos[0],))
        until(stop, touch, period_s=2)

    def probe(self, admin: Conn) -> str:
        # 不量表大小:VACUUM FULL 拿着整表排他锁,pg_total_relation_size 会排在它后面
        rows = admin.query(
            "SELECT extract(epoch FROM now() - query_start)::int FROM pg_stat_activity "
            " WHERE application_name = %s AND state = 'active' AND sessionid <> 0 "
            "   AND query LIKE 'VACUUM FULL%%'", (C.APP_PREFIX + "V6/maint",))
        doing = "VACUUM FULL 这一次已做 %s 秒" % rows[0][0] if rows else "此刻没在做 VACUUM FULL"
        return "%s,业务侧等锁 %d 个" % (doing, self.waiting(admin))
