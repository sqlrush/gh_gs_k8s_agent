"""死锁(D1–D6 六种形态轮流):约 30 秒一次,20 分钟大约 35 次。

**六种形态为什么都要:** 它们在日志里长得不一样,给用户的建议也不一样 ——
行序颠倒要改代码里的加锁顺序;锁升级要改成一开始就 FOR UPDATE;外键死锁要调整
父子表的写入顺序;唯一索引死锁要靠按序插入。只造一种的话,「死锁」这件事看起来
永远是同一个样子。

**做法:** 每一轮开两三条线程,让它们按设计好的相反顺序加锁,然后一起撞上去。
数据库自己会选一个牺牲者回滚 —— 牺牲者收到的报错是**预期**的,所以这些会话都声明了
expect_errors;真正的失败(连不上、SQL 写错)仍然会被记下来。

**为什么不是一直撞:** 死锁检测要等 deadlock_timeout(og5 上 1 秒),撞得太密集会让
整个库都在做死锁检测,反而盖住别的场景。30 秒一次足够让计数稳定上涨。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Tuple

from .. import config as C
from ..db import Conn
from ..session import sleep_until
from .base import Base, register

STEP_SLEEP_S = 1.5          # 两步之间的间隔:要让对方也拿到自己的第一把锁
ROUND_TIMEOUT_S = 20
STATS_LAG_S = 2.0       # 等统计收集器把这一轮的死锁刷进 pg_stat_database


def _tx(c: Conn, steps: List[str]) -> None:
    """一笔事务按顺序跑几条语句,最后提交。中间被判死锁的话,异常由调用方接。"""
    c.exec("BEGIN")
    for i, sql in enumerate(steps):
        c.exec(sql)
        if i == 0:
            time.sleep(STEP_SLEEP_S)        # 故意停一下,等对方也拿到第一把锁
    c.exec("COMMIT")


@register("D")
class Deadlocks(Base):
    """一个驱动会话,轮流演六种形态。"""

    group = "deadlock"

    def start(self) -> None:
        self.worker("driver", self._drive, expect_errors=True)

    def _drive(self, c: Conn, stop: threading.Event) -> None:
        """驱动本身不加锁 —— 每一轮另开短命连接去撞,撞完就关。

        这样每一轮都是干净的新事务,不会因为上一轮的牺牲者把本轮的状态搅乱。
        """
        forms = [(code, title, getattr(self, "_form_" + code.lower()))
                 for code, title in C.DEADLOCK_FORMS]
        i = 0
        while not stop.is_set():
            code, title, fn = forms[i % len(forms)]
            i += 1
            before = self._deadlock_count(c)
            try:
                fn(stop)
            except Exception as exc:        # noqa: BLE001 —— 撞死锁本来就会报错
                self.ctx.bump("deadlock_victim_errors")
            # **等一下再读计数。** pg_stat_database 的计数由统计收集器异步刷新,撞完立刻读
            # 往往还没算上这一轮,于是这一轮显示 +0、下一轮显示 +2(2026-10-17 全量跑时
            # D1/D3/D5 都出现过 +0,而总数是对的)。等一拍再读,日志才对得上。
            sleep_until(stop, STATS_LAG_S)
            after = self._deadlock_count(c)
            got = after - before
            self.ctx.bump("deadlocks_seen", max(0, got))
            self.ctx.bump("%s_rounds" % code)
            self.log("%s %s → 库里死锁计数 +%d" % (code, title, got))
            sleep_until(stop, C.DEADLOCK_PERIOD_S)

    def _deadlock_count(self, c: Conn) -> int:
        return int(c.one("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()") or 0)

    # -- 六种形态。每个都开自己的连接,撞完即关 ------------------------------

    def _race(self, stop: threading.Event, plans: List[List[str]]) -> None:
        """并发跑几笔事务,各自按 plans 里给的顺序加锁。"""
        errs: List[BaseException] = []

        def one(steps: List[str]) -> None:
            c = None
            try:
                # 撞车用的短命连接最多等锁 ROUND_TIMEOUT_S:万一撞上别的场景锁着的行,
                # 这一轮作废就是了,不能挂到窗口结束(它们不归 Worker 管,停场景时不会被取消)。
                c = Conn(self.code + "/x", lockwait_s=ROUND_TIMEOUT_S)
                _tx(c, steps)
            except BaseException as exc:    # noqa: BLE001
                errs.append(exc)
                if c is not None:
                    c.rollback()
            finally:
                if c is not None:
                    c.close()

        threads = [threading.Thread(target=one, args=(p,), daemon=True) for p in plans]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=ROUND_TIMEOUT_S)

    # D3 / D5 的子表。**不用 acct_child:** K4 每 3 分钟有 2 分钟用 LOCK TABLE 锁着它,
    # 2026-10-24 第二轮 D5 三轮都卡在那把表锁上(死锁 +0),D3 也只撞出 2 次。
    CHILD = "dl_child"

    def _upd(self, table: str, where_id: int) -> str:
        return "UPDATE %s.%s SET %s WHERE id = %d" % (
            C.SCHEMA, table, "bal = bal + 1" if table == "acct" else "amount = amount + 1", where_id)

    def _form_d1(self, stop: threading.Event) -> None:
        """两会话交叉更新同表两行:A 先 10 后 11,B 先 11 后 10。"""
        self._race(stop, [[self._upd("acct", 10), self._upd("acct", 11)],
                          [self._upd("acct", 11), self._upd("acct", 10)]])

    def _form_d2(self, stop: threading.Event) -> None:
        """三会话成环:A(20→21)、B(21→22)、C(22→20)。"""
        self._race(stop, [[self._upd("acct", 20), self._upd("acct", 21)],
                          [self._upd("acct", 21), self._upd("acct", 22)],
                          [self._upd("acct", 22), self._upd("acct", 20)]])

    def _form_d3(self, stop: threading.Event) -> None:
        """跨表顺序相反:A 先 acct 后子表,B 反过来。"""
        self._race(stop, [[self._upd("acct", 30), self._upd(self.CHILD, 30)],
                          [self._upd(self.CHILD, 30), self._upd("acct", 30)]])

    def _form_d4(self, stop: threading.Event) -> None:
        """锁升级:两边都先对同一行加共享锁(FOR SHARE),再都想改它。

        这一种最隐蔽:两笔事务一开始都「只是读」,各自都拿到了共享锁、都以为没事,
        升级成排他锁时必然互相等 —— 代码里根本看不到交叉加锁的痕迹。
        """
        share = "SELECT bal FROM %s.acct WHERE id = 40 FOR SHARE" % C.SCHEMA
        self._race(stop, [[share, self._upd("acct", 40)], [share, self._upd("acct", 40)]])

    def _form_d5(self, stop: threading.Event) -> None:
        """外键死锁:父行的**键级锁**与 `FOR UPDATE` 交叉。

        A 先把父行 50 锁住(FOR UPDATE),再插一条引用父行 51 的子行;
        B 先把父行 51 锁住,再插一条引用父行 50 的子行。插子行时外键检查要在父行上
        拿键级共享锁,而对方手里正握着那一行的 FOR UPDATE —— 两边互相等,成环。

        **为什么不能用「改父行」来撞(前两版都失败了,别再改回去):**
        实测 openGauss 的外键检查在父表上只拿 RowShareLock(表级),行级拿的是
        **键级**共享锁 —— 它和「只改非键列的 UPDATE」根本不冲突。所以
        「一边插子行、一边 UPDATE 父行的 bal」怎么撞都撞不出死锁:
        第一版用同一个父行、第二版用两个父行,死锁计数都一直是 +0。
        要撞就必须动键级锁:`FOR UPDATE`(或者更新键列、删父行)才与它冲突。
        """
        def lock_parent(parent: int) -> str:
            return "SELECT bal FROM %s.acct WHERE id = %d FOR UPDATE" % (C.SCHEMA, parent)

        def ins(parent: int) -> str:
            return ("INSERT INTO %s.%s(id, acct_id, amount) "
                    "VALUES ((random() * 1000000)::int + 7000000, %d, 1)" % (C.SCHEMA, self.CHILD, parent))
        self._race(stop, [[lock_parent(50), ins(51)],
                          [lock_parent(51), ins(50)]])

    def _form_d6(self, stop: threading.Event) -> None:
        """唯一索引死锁:两边插同样的两个主键,顺序相反。"""
        a, b = 6001, 6002
        ins_a = "INSERT INTO %s.uniq_race(k, who) VALUES (%d, 'd6a')" % (C.SCHEMA, a)
        ins_b = "INSERT INTO %s.uniq_race(k, who) VALUES (%d, 'd6b')" % (C.SCHEMA, b)
        clean = "DELETE FROM %s.uniq_race WHERE k IN (%d, %d)" % (C.SCHEMA, a, b)
        self._race(stop, [[clean], []])                 # 先清掉上一轮留下的
        self._race(stop, [[ins_a, ins_b], [ins_b, ins_a]])

    def probe(self, admin: Conn) -> str:
        cnt = self.ctx.snapshot_counters()
        total = int(admin.one(
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()") or 0)
        rounds = sum(v for k, v in cnt.items() if k.endswith("_rounds"))
        return "已撞 %d 轮,本库死锁累计计数 %d(这个计数**不会**自己清零)" % (rounds, total)
