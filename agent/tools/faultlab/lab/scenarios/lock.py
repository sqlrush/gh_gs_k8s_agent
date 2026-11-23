"""锁等待(K1–K10):十种现场最常见的锁冲突。

**十种锁类型分别落在哪:** transactionid(等某笔事务结束)、tuple(等行锁队列)、
relation(表级锁:DDL / LOCK TABLE / 建索引)、extend(表扩展)、advisory(应用层咨询锁),
以及两阶段事务留下的「锁还在、持锁的会话却不在了」。

**共同的做法:持锁者先确认拿到锁,再放等待者进来。** 不这么做的话,偶尔会变成等待者
先到、持锁者后到,链条方向就反了 —— 而终端上看不出任何异常(tools/probe_lock_matrix.py
在审查里被打回的就是这个问题)。所以每个场景都用 wait_for 显式确认,不靠 sleep 赌。
"""
from __future__ import annotations

import threading
from typing import List, Tuple

from .. import config as C
from ..db import Conn
from ..session import sleep_until, until, wait_for
from .base import Base, register

HOLD_POLL_S = 0.5


def _hold_rows(c: Conn, stop: threading.Event, ids: List[int], ready: threading.Event) -> None:
    """开一笔事务,改几行,然后**一直不提交** —— 现场最常见的根因:
    应用拿了连接改了数据,之后去等别的系统(或者干脆忘了提交)。"""
    c.exec("BEGIN")
    for i in ids:
        c.exec("UPDATE %s.acct SET bal = bal + 1 WHERE id = %%s" % C.SCHEMA, (i,))
    ready.set()
    while not stop.is_set():
        stop.wait(HOLD_POLL_S)
    c.exec("ROLLBACK")


@register("K1")
class RowChain(Base):
    """四层行锁阻塞链:root 持 1 号行 → mid1 持 2 号等 1 号 → mid2 持 3 号等 2 号 → 3 个等待者等 3 号。

    **为什么要四层:** 只有两层的话,「谁是真凶」太好猜。四层能逼出一个真问题:
    杀掉链条中间那个解不开,必须杀最根上那个 —— lockwait 的 chain.py 就是为这件事存在的。
    """

    group = "lock"

    def start(self) -> None:
        root_ready, m1_ready, m2_ready = (threading.Event() for _ in range(3))
        self.worker("root", lambda c, stop: _hold_rows(c, stop, [1], root_ready))
        if not wait_for(root_ready.is_set, 20):
            self.log("root 没能在 20 秒内拿到行锁,链条可能搭不起来")
        self.worker("mid1", lambda c, stop: self._mid(c, stop, 2, 1, m1_ready))
        wait_for(m1_ready.is_set, 20)
        self.worker("mid2", lambda c, stop: self._mid(c, stop, 3, 2, m2_ready))
        wait_for(m2_ready.is_set, 20)
        for n in range(3):
            self.worker("wait%d" % n, lambda c, stop: self._tail(c, stop))
        self.log("链条已搭:root(1) ← mid1(2) ← mid2(3) ← 3 个等待者")

    def _mid(self, c: Conn, stop: threading.Event, hold_id: int, wait_id: int,
             ready: threading.Event) -> None:
        """先占住自己那一行(这一步会成功),再去改上一层占着的行(这一步会挂住)。"""
        c.exec("BEGIN")
        c.exec("UPDATE %s.acct SET bal = bal + 1 WHERE id = %%s" % C.SCHEMA, (hold_id,))
        ready.set()
        c.exec("UPDATE %s.acct SET bal = bal + 1 WHERE id = %%s" % C.SCHEMA, (wait_id,))  # 挂在这里
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)
        c.exec("ROLLBACK")

    def _tail(self, c: Conn, stop: threading.Event) -> None:
        c.exec("UPDATE %s.acct SET bal = bal + 1 WHERE id = 3" % C.SCHEMA)   # 一直等
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)

    def probe(self, admin: Conn) -> str:
        depth = admin.one(
            "SELECT count(*) FROM pg_thread_wait_status w JOIN pg_stat_activity a "
            "  ON a.sessionid = w.sessionid "
            " WHERE a.application_name LIKE %s AND w.block_sessionid IS NOT NULL "
            "   AND w.block_sessionid <> w.sessionid", (C.APP_PREFIX + self.code + "%",))
        return "等锁会话 %d 个,记到阻塞关系的 %s 条" % (self.waiting(admin), depth)


@register("K10")
class OrphanPrepared(Base):
    """孤儿两阶段事务:PREPARE 之后会话就走了,锁留在库里。

    **为什么单列一项:** 这是唯一一种「杀会话也解不开」的锁 —— pg_stat_activity 里根本
    没有持锁人,只有 pg_prepared_xacts 里有一条。现场遇到过有人反复 kill 等待者,
    怎么 kill 都好不了。解法只有 COMMIT PREPARED / ROLLBACK PREPARED。
    """

    group = "lock"
    GID = "fl_orphan_k10"

    def start(self) -> None:
        self.worker("prep", self._prepare)

    def _prepare(self, c: Conn, stop: threading.Event) -> None:
        from .. import state
        c.exec("BEGIN")
        c.exec("UPDATE %s.acct SET bal = bal + 1 WHERE id = 100" % C.SCHEMA)
        c.exec("PREPARE TRANSACTION '%s'" % self.GID)
        # **登记进 state:** 这笔事务不会随会话结束而消失,cleanup 必须显式回滚它。
        state.add_residue("prepared_xact", self.GID)
        self.log("已 PREPARE %s(会话随即退出,锁留在库里)" % self.GID)

    def probe(self, admin: Conn) -> str:
        n = admin.one("SELECT count(*) FROM pg_prepared_xacts WHERE gid = %s", (self.GID,))
        return "两阶段事务在库里:%s 条(持锁者不在 pg_stat_activity 里)" % n


@register("K4")
class TableLockCycles(Base):
    """月结任务:LOCK TABLE … IN EXCLUSIVE MODE,锁 2 分钟、放 1 分钟,循环。

    造的是**间歇性**表级锁:业务侧表现为「每小时总有几分钟特别卡」,而排查的人赶到时
    往往已经放开了。lockwait / health 要能在锁着的那几分钟里说清是谁锁的。
    """

    group = "lock"
    HOLD_S = 120
    FREE_S = 60

    def start(self) -> None:
        self.worker("monthend", self._cycle)
        for n in range(2):
            self.worker("biz%d" % n, self._biz, expect_errors=True)

    def _cycle(self, c: Conn, stop: threading.Event) -> None:
        def one_round() -> None:
            c.exec("BEGIN")
            c.exec("LOCK TABLE %s.acct_child IN EXCLUSIVE MODE" % C.SCHEMA)
            self.log("已锁住 acct_child(%d 秒)" % self.HOLD_S)
            sleep_until(stop, self.HOLD_S)
            c.exec("ROLLBACK")
            self.log("放开 acct_child(%d 秒)" % self.FREE_S)
            sleep_until(stop, self.FREE_S)
        until(stop, one_round)

    def _biz(self, c: Conn, stop: threading.Event) -> None:
        """业务侧:锁着的时候它就挂在那里等。"""
        until(stop, lambda: c.exec(
            "UPDATE %s.acct_child SET amount = amount + 1 WHERE id = 5" % C.SCHEMA), period_s=5)

    def probe(self, admin: Conn) -> str:
        held = admin.one(
            "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            " WHERE a.application_name = %s AND l.mode = 'ExclusiveLock' AND l.granted",
            (C.APP_PREFIX + "K4/monthend",))
        return "表级排他锁 %s 把,业务侧等锁 %d 个" % (held, self.waiting(admin))


@register("K6")
class ForeignKeyWait(Base):
    """外键等待:父行被 `FOR UPDATE` 锁着,子表插入引用这一行的数据就插不进去。

    子表插入要确认父行存在,会在父行上拿**键级共享锁**;父行正被 FOR UPDATE 握着,
    就只能等。现实里的样子:「主数据维护」(锁住某个行方/账户做核对)和业务写入同时跑,
    两边都不觉得自己在加锁。

    **这里有一条反直觉的事实,别改回去:** 一开始父行用的是 `UPDATE acct SET bal = …`
    (只改非键列),子表插入**根本不会被挡住** —— 2026-10-17 两个会话实测:非键列更新
    0.0 秒就插进去了,换成 `FOR UPDATE` 才被挡住(等到 94 秒后被清理杀掉)。
    原因是外键检查拿的是**键级**共享锁,它只和「改键列 / 删父行 / FOR UPDATE」冲突,
    和「只改非键列」的行锁是兼容的。想造外键锁等待,就必须动键级锁。
    (同一个坑让死锁场景 D5 连着两版都撞不出死锁,见 deadlock.py。)
    """

    group = "lock"
    PARENT = 200

    def start(self) -> None:
        ready = threading.Event()
        self.worker("parent", lambda c, stop: self._hold_parent(c, stop, ready))
        wait_for(ready.is_set, 20)
        self.worker("child", self._child, expect_errors=True)

    def _hold_parent(self, c: Conn, stop: threading.Event, ready: threading.Event) -> None:
        c.exec("BEGIN")
        c.query("SELECT bal FROM %s.acct WHERE id = %%s FOR UPDATE" % C.SCHEMA, (self.PARENT,))
        ready.set()
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)
        c.exec("ROLLBACK")

    def _child(self, c: Conn, stop: threading.Event) -> None:
        """插引用父行的子行。**id 必须每次都不一样,而且插失败要接着试。**

        这里踩过一个很隐蔽的坑:原来 id 从固定的 9000001 开始往上加,而 acct_child 里
        上一次运行插进去的行**不会被清掉** —— 于是第二次跑这个场景时第一条插入就撞主键,
        会话直接结束(它声明了 expect_errors,报错被当成预期的吞掉了),
        于是「子表插入等父行」永远是 0 个,看起来像场景没生效。
        现在 id 用随机值,并且插失败也继续 —— 同时 cleanup 会把这些行删掉(见 cleanup.py)。
        """
        def insert() -> None:
            c.exec("INSERT INTO %s.acct_child(id, acct_id, amount) "
                   "VALUES ((random() * 1000000)::int + 7000000, %%s, 1)" % C.SCHEMA,
                   (self.PARENT,))
        until(stop, insert, period_s=3,
              on_error=lambda exc: self.ctx.bump("K6_insert_errors"))

    def probe(self, admin: Conn) -> str:
        holding = admin.one(
            "SELECT count(*) FROM pg_stat_activity "
            " WHERE application_name = %s AND sessionid <> 0", (C.APP_PREFIX + "K6/parent",))
        return "父行持锁会话 %s 个,子表插入等父行的会话 %d 个" % (holding, self.waiting(admin))


@register("K7")
class UniqueKeyRace(Base):
    """多个会话插同一个主键:第一个没提交,后来的只能等它的结果(成功就报冲突,回滚就能插进去)。

    等的是 transactionid —— 这类等待在 pg_locks 里**看不到行锁**,只看到在等某笔事务,
    是「为什么插入会卡住」这类工单里最难解释的一种。
    """

    group = "lock"
    KEY = 777

    def start(self) -> None:
        ready = threading.Event()
        self.worker("first", lambda c, stop: self._first(c, stop, ready))
        wait_for(ready.is_set, 20)
        for n in range(2):
            self.worker("race%d" % n, self._race, expect_errors=True)

    def _first(self, c: Conn, stop: threading.Event, ready: threading.Event) -> None:
        c.exec("BEGIN")
        c.exec("DELETE FROM %s.uniq_race WHERE k = %%s" % C.SCHEMA, (self.KEY,))
        c.exec("INSERT INTO %s.uniq_race(k, who) VALUES (%%s, 'first')" % C.SCHEMA, (self.KEY,))
        ready.set()
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)
        c.exec("ROLLBACK")

    def _race(self, c: Conn, stop: threading.Event) -> None:
        until(stop, lambda: c.exec(
            "INSERT INTO %s.uniq_race(k, who) VALUES (%%s, 'race')" % C.SCHEMA, (self.KEY,)),
            period_s=5, on_error=lambda exc: self.ctx.bump("K7_conflict"))

    def probe(self, admin: Conn) -> str:
        return "等主键冲突结果的会话 %d 个" % self.waiting(admin)


@register("K8")
class AdvisoryLock(Base):
    """应用层咨询锁:持有者「卡住」不放,后面三个排队。

    **为什么要专门造它:** 咨询锁和任何表、任何行都没有关系 —— 在 pg_locks 里是
    locktype='advisory'。只会看表锁的排查思路到这里会完全断线:「没有任何表被锁,
    可是业务就是过不去」。
    """

    group = "lock"
    LOCK_ID = 880_001

    def start(self) -> None:
        ready = threading.Event()
        self.worker("holder", lambda c, stop: self._holder(c, stop, ready))
        wait_for(ready.is_set, 20)
        for n in range(3):
            self.worker("wait%d" % n, self._waiter)

    def _holder(self, c: Conn, stop: threading.Event, ready: threading.Event) -> None:
        c.exec("SELECT pg_advisory_lock(%s)", (self.LOCK_ID,))
        ready.set()
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)
        c.exec("SELECT pg_advisory_unlock(%s)", (self.LOCK_ID,))

    def _waiter(self, c: Conn, stop: threading.Event) -> None:
        c.exec("SELECT pg_advisory_lock(%s)", (self.LOCK_ID,))      # 一直等
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)

    def probe(self, admin: Conn) -> str:
        granted = admin.one("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted")
        waiting = admin.one("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        return "咨询锁:持有 %s 把、排队 %s 个" % (granted, waiting)


@register("K2")
class BatchForUpdate(Base):
    """批处理分批 SELECT … FOR UPDATE,在线更新间歇性等锁(抖动型,不是一直堵死)。

    **抖动比堵死更难查:** 每批只锁几千行、几百毫秒就放,在线交易偶尔卡一下。
    等排查的人连上去看,往往什么都看不到 —— 这一项就是用来检验「抖动型锁等待」
    能不能被抓到(要靠按时间段统计,不是看某一个瞬间)。
    """

    group = "lock"
    BATCH = 400
    # **只用 acct 的 1001~2000 号行。** 原来批处理从 1 号开始扫、在线更新也满表乱跳,
    # 一上来就撞上 K1 根会话锁着的 1~3 号行:批处理在 1 号行上一等就是 18 分钟,
    # 抖动型变成了持续阻塞(2026-10-24 全量跑)。别的场景用到的行都在 1~200 号以内
    # (K1 1~3、D 10~51、K10 100、K6 200、L3 已改用 xact_tick),这一段谁也不碰。
    LO, HI = 1001, 2000

    def start(self) -> None:
        self.worker("batch", self._batch)
        for n in range(2):
            self.worker("online%d" % n, self._online, expect_errors=True)

    @classmethod
    def batch_range(cls, n: int) -> Tuple[int, int]:
        """第 n 批锁哪一段:在 [LO, HI] 里循环,每批 BATCH 行。"""
        span = cls.HI - cls.LO + 1
        lo = cls.LO + (n * cls.BATCH) % span
        return lo, min(lo + cls.BATCH - 1, cls.HI)

    @classmethod
    def online_id(cls, n: int) -> int:
        """在线更新第 n 次改哪一行:同样只在 [LO, HI] 里跳。"""
        return cls.LO + (n * 137) % (cls.HI - cls.LO + 1)

    def _batch(self, c: Conn, stop: threading.Event) -> None:
        pos = [0]

        def one_batch() -> None:
            lo, hi = self.batch_range(pos[0])
            pos[0] += 1
            c.exec("BEGIN")
            c.exec("SELECT id FROM %s.acct WHERE id BETWEEN %%s AND %%s ORDER BY id FOR UPDATE"
                   % C.SCHEMA, (lo, hi))
            sleep_until(stop, 2.0)              # 批内处理时间
            c.exec("COMMIT")

        # 批处理和在线更新本来就会偶尔撞死锁(实测撞到过)—— 那是这个场景的真实后果之一,
        # 不是工具坏了。回滚掉接着跑下一批。
        def recover(exc: BaseException) -> None:
            c.rollback()
            self.ctx.bump("K2_batch_errors")
        until(stop, one_batch, period_s=4.0, on_error=recover)

    def _online(self, c: Conn, stop: threading.Event) -> None:
        pos = [0]

        def touch() -> None:
            pos[0] += 1
            c.exec("UPDATE %s.acct SET bal = bal + 0.01 WHERE id = %%s" % C.SCHEMA, (self.online_id(pos[0]),))
        until(stop, touch, period_s=0.3)

    def probe(self, admin: Conn) -> str:
        return "此刻等锁 %d 个(抖动型:为 0 也正常,要看一段时间内的统计)" % self.waiting(admin)


@register("K5")
class IndexBuild(Base):
    """不带 CONCURRENTLY 建索引:300 万行的表,建索引期间所有写入都被挡住。

    现实里的样子:白天「顺手加个索引」。建索引本身要几十秒到几分钟,这段时间里
    这张表只能读不能写,业务侧看到的是「插入全部超时」。

    **建的是一条慢表达式索引,而且时段内只建一次、不删。** 原来是普通索引循环「建 → 等 20 秒 → 删」:
    建只要 7 秒,写入被挡的时间不到三成;更糟的是 DROP INDEX 要整表排他锁,排在 S2/S3 读
    order_items 的长查询后面,写入又排在 DROP 后面 —— 挡住写入的其实是 DROP 加长查询,
    看起来像 K3 而不是 K5(2026-10-24 全量跑)。现在 CREATE INDEX 本身就要两分多钟
    (每行算一次 fl_slow_key),整个时段都在建;时段一到,停场景时把它取消,不留索引、
    也不在窗口里做 DROP。万一机器快、提前建完了,索引留着,由 cleanup 删(登记过)。
    """

    group = "lock"
    IDX = "idx_fl_k5_items_note"

    def start(self) -> None:
        self.worker("ddl", self._build, expect_errors=True)
        for n in range(2):
            self.worker("writer%d" % n, self._writer, expect_errors=True)

    @classmethod
    def index_sql(cls) -> str:
        from ..prepare import slow_key_expr
        return "CREATE INDEX %s ON %s.order_items ((%s))" % (
            cls.IDX, C.SCHEMA, slow_key_expr("note", C.K5_SLOW_ITERS))

    def _build(self, c: Conn, stop: threading.Event) -> None:
        from .. import state
        c.set("query_dop", "1")
        state.add_residue("index", "%s.%s" % (C.SCHEMA, self.IDX))   # 先登记:被 kill 也有人收
        # **窗口里不做 DROP INDEX:** 它要整表排他锁,会排在 S2 读 order_items 的长查询后面。
        # 上一次的索引还在,说明上次没清干净 —— 如实说,不在这里补删。
        if c.one("SELECT count(*) FROM pg_class WHERE relname = %s", (self.IDX,)):
            self.log("上一次的 %s 还在(上次清理没做完),这次不建 —— 先跑 cleanup" % self.IDX)
            return
        self.log("开始建索引(慢表达式,预计要建满整个时段;期间 order_items 写不进)")
        c.exec(self.index_sql())                                      # 挂在这里,时段到了被取消
        self.log("索引提前建完了 —— 写入从此不再被挡;索引留到 cleanup 再删")
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)

    def _writer(self, c: Conn, stop: threading.Event) -> None:
        """往大表里写。id 用随机值,原因同 K6._child(固定 id 第二次跑就撞主键)。"""
        def insert() -> None:
            c.exec("INSERT INTO %s.order_items(id, order_id, product_id, qty, amount, note) "
                   "VALUES ((random() * 100000000)::bigint + 8000000, 1, 1, 1, 1.0, 'k5')"
                   % C.SCHEMA)
        until(stop, insert, period_s=1.0,
              on_error=lambda exc: self.ctx.bump("K5_insert_errors"))

    def probe(self, admin: Conn) -> str:
        rows = admin.query(
            "SELECT extract(epoch FROM now() - query_start)::int, substr(query, 1, 12) "
            "  FROM pg_stat_activity WHERE application_name = %s AND state = 'active' AND sessionid <> 0",
            (C.APP_PREFIX + "K5/ddl",))
        building = "建索引已 %s 秒" % rows[0][0] if rows and "CREATE" in str(rows[0][1]).upper() else "没在建索引"
        return "%s,写入侧等锁 %d 个" % (building, self.waiting(admin))


@register("K3")
class DdlQueue(Base):
    """DDL 锁队列放大:长查询挡住 ALTER TABLE,而排在 DDL 后面的普通读写也全被挡住。

    **这是最反直觉的一种:** 一个只读的长查询,本来谁也不挡;可是一旦有人想加字段,
    ALTER 要等它,而 ALTER 已经在队列里排上了表级排他锁,**后面所有读写都得排在 ALTER 之后**。
    于是「一个读查询 + 一个加字段」把整张表的读写全冻住了。现场运维不敢白天做 DDL,就是因为这个。
    """

    group = "lock"
    COL = "fl_k3_tmp"

    def start(self) -> None:
        ready = threading.Event()
        self.worker("longread", lambda c, stop: self._long_read(c, stop, ready))
        wait_for(ready.is_set, 20)
        self.worker("ddl", self._ddl, expect_errors=True)
        sleep_until(self.ctx.stop, 2)
        for n in range(2):
            self.worker("reader%d" % n, self._reader, expect_errors=True)

    def _long_read(self, c: Conn, stop: threading.Event, ready: threading.Event) -> None:
        """开一笔事务、读一次 payments(拿到表级共享锁),然后一直不提交。"""
        c.exec("BEGIN")
        c.query("SELECT count(*) FROM %s.payments WHERE id < 1000" % C.SCHEMA)
        ready.set()
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)
        c.exec("ROLLBACK")

    def _ddl(self, c: Conn, stop: threading.Event) -> None:
        from .. import state
        state.add_residue("column", "%s.payments.%s" % (C.SCHEMA, self.COL))
        c.exec("ALTER TABLE %s.payments ADD COLUMN %s int" % (C.SCHEMA, self.COL))   # 挂在这里
        while not stop.is_set():
            stop.wait(HOLD_POLL_S)

    def _reader(self, c: Conn, stop: threading.Event) -> None:
        until(stop, lambda: c.query("SELECT count(*) FROM %s.payments WHERE id < 10" % C.SCHEMA),
              period_s=5)

    def probe(self, admin: Conn) -> str:
        return "DDL 队列里等锁 %d 个(其中既有 ALTER,也有排在它后面的普通读)" % self.waiting(admin)


@register("K9")
class ExtendLock(Base):
    """多会话往同一张表批量插入,抢表扩展锁(locktype='extend')。

    表要长大时,只能有一个会话在扩展文件。并发插入一多,等的就是这把锁 ——
    现场表现为「批量导入越加并发越不快」。
    """

    group = "lock"

    def start(self) -> None:
        for n in range(2):
            self.worker("ins%d" % n, self._insert, expect_errors=True)

    def _insert(self, c: Conn, stop: threading.Event) -> None:
        until(stop, lambda: c.exec(
            "INSERT INTO %s.wal_log(who, payload) "
            "SELECT 'k9', repeat('e', 180) FROM generate_series(1, 2000)" % C.SCHEMA), period_s=0.0)

    def probe(self, admin: Conn) -> str:
        n = admin.one("SELECT count(*) FROM pg_locks WHERE locktype = 'extend'")
        return "此刻 extend 锁 %s 把(瞬时锁,抓不到也正常,看 WDR 的等待事件)" % n
