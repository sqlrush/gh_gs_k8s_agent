"""轻量锁(L1–L5):五段,每段 3 条会话,边界与 WDR 快照严格对齐。

**轻量锁和普通锁完全是两回事:** 普通锁(表锁、行锁)保护的是**数据**,能在 pg_locks 里
看到谁锁了哪张表、等了多久;轻量锁保护的是内核自己的**共享内存结构**(WAL 缓冲区、
缓冲池映射表、事务号分配器、锁管理器的分区),持有时间以微秒计,在 pg_locks 里一行都没有。
唯一能看到它的地方是等待事件统计(`pg_thread_wait_status` / WDR 的等待事件段)。

**所以这五段是专门用来检验 waitevent / wdr 的:** 如果一个工具只会查 pg_locks,
这五段在它眼里全是「没有锁等待,库很正常」,而实际上库已经在打转了。

**为什么边界要和快照对齐:** 每个快照窗口里只有一种轻量锁争用,WDR 的多窗口分解才能
给出「这 4 分钟是谁在争、下 4 分钟换成了谁」。五段混在一起的话,等待事件榜上永远是
同一锅粥,分不出是哪种操作引起的。

**不能保证每种都一定出现:** 轻量锁争用取决于内核版本、线程池配置、机器核数。
og5 只有 4 个核、3 条会话,有些段可能只是轻微争用。probe 会如实报出当下排名靠前的
等待事件,**没争起来就说没争起来**,不去替它圆场 —— 手册里也写明了这一点。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List

from .. import config as C
from ..db import Conn
from ..session import until
from .base import Base, register

SESSIONS = 3

# pg_thread_wait_status 里这两种状态**不是在等锁**:none 是正在 CPU 上跑,wait cmd 是会话
# 空着等客户端发下一条命令。2026-10-24 那轮 L3 的自证把 wait cmd 也算成了「在等」,
# 一段根本没争起来的轻量锁看上去「在等的 35 次」。
IDLE_STATUSES = ("none", "wait cmd")


def summarize(tally: Dict[str, int], samples: int) -> str:
    """把连采的结果汇总成一行(纯函数,单测钉住)。tally 的键是 `状态/事件`。"""
    if not tally:
        return "连采 %d 次都没有本场景的等待记录(会话可能刚起或已停)" % samples
    top = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    busy = sum(v for k, v in tally.items() if k.split("/", 1)[0] not in IDLE_STATUSES)
    lw = sum(v for k, v in tally.items() if k.startswith("acquire lwlock/"))
    return "%d 次采样:%s(真在等的 %d 次,其中轻量锁 %d 次)" % (
        samples, "、".join("%s×%d" % (k, v) for k, v in top), busy, lw)


_COMMIT_IN_DO: Dict[str, bool] = {}


def commit_in_do_supported(c: Conn) -> bool:
    """这个内核的匿名块里能不能 COMMIT(能的话,一次往返就能在服务端连提几百个事务)。

    探测一次、整个进程记住。探测本身只是一个空事务的 COMMIT,不写任何数据。
    """
    if "v" not in _COMMIT_IN_DO:
        try:
            c.exec("DO $$ BEGIN COMMIT; END $$;")
            _COMMIT_IN_DO["v"] = True
        except Exception:                        # noqa: BLE001
            c.rollback()
            _COMMIT_IN_DO["v"] = False
    return _COMMIT_IN_DO["v"]


class _Churn(Base):
    """五段的共同骨架:开 SESSIONS 条会话,每条反复跑自己那一小段 SQL。"""

    group = "lwlock"
    role = "w"

    def start(self) -> None:
        for n in range(SESSIONS):
            self.worker("%s%d" % (self.role_of(n), n),
                        lambda c, stop, i=n: self._loop(c, stop, i), expect_errors=True)

    def role_of(self, idx: int) -> str:
        return self.role

    def _loop(self, c: Conn, stop: threading.Event, idx: int) -> None:
        self.setup(c, idx)
        until(stop, lambda: self.step(c, idx), period_s=0.0)

    def setup(self, c: Conn, idx: int) -> None:
        """每条会话开始前做一次的设置(默认什么都不做)。"""

    def step(self, c: Conn, idx: int) -> None:
        raise NotImplementedError

    @staticmethod
    def async_commit(c: Conn) -> None:
        """会话级关掉同步提交:提交不再等 WAL 刷盘,争用才落到想争的那把锁上。

        不关的话,每条即提交的写都在等刷盘 —— 2026-10-24 那轮 L2 的自证里全是 WALFlushWait,
        一次 BufferContentLock 都没有,热点页根本没争起来,争起来的是 L1 那种提交风暴。
        """
        c.set("synchronous_commit", "off")

    SAMPLES = 20
    SAMPLE_GAP_S = 0.1

    def probe(self, admin: Conn) -> str:
        """这个场景的会话在等什么 —— 连采 20 次(约 2 秒)再汇总。

        **为什么不能只看一眼:** 轻量锁的持有时间以**微秒**计。单次采样几乎总是落在
        「没在等」的那一刻,于是 probe 一直报 none —— 看起来像场景没生效,实际是采样方式不对
        (2026-10-17 全量跑时 L1/L2 一直显示 none/none,而同一时刻全局等待事件里
        明明有 WALFlushWait)。多采几次就能把这种瞬时争用抓出来。

        这同时也是给 skill 的一条提醒:**轻量锁要看累计统计(WDR 的等待事件段),
        不能靠查一次 pg_thread_wait_status。**
        """
        tally: Dict[str, int] = {}
        for _ in range(self.SAMPLES):
            rows = admin.query(
                "SELECT w.wait_status, coalesce(w.wait_event, '-') "
                "  FROM pg_thread_wait_status w JOIN pg_stat_activity a ON a.sessionid = w.sessionid "
                " WHERE a.application_name LIKE %s AND a.sessionid <> 0",
                (C.APP_PREFIX + self.code + "%",))
            for status, event in rows:
                key = "%s/%s" % (status, event)
                tally[key] = tally.get(key, 0) + 1
            time.sleep(self.SAMPLE_GAP_S)
        return summarize(tally, self.SAMPLES)


@register("L1")
class CommitStorm(_Churn):
    """提交风暴:每条会话单行插入、每条都立即提交。

    每次提交都要把 WAL 写进缓冲区并刷盘 → 抢 WAL 插入锁、WAL 写锁;
    `synchronous_commit=on`(og5 的默认)时还要等刷盘完成。
    现实里的样子:应用把批量插入拆成了一行一个事务,吞吐上不去、CPU 也不高,全在等提交。
    """

    role = "ins"

    def step(self, c: Conn, idx: int) -> None:
        c.exec("INSERT INTO %s.wal_log(who, payload) VALUES (%%s, %%s)" % C.SCHEMA,
               ("L1-%d" % idx, "x" * 160))


@register("L2")
class HotPage(_Churn):
    """热点页:三条会话反复更新同一张 100 行的小表 —— 所有行都挤在同一个数据页上。

    每次更新都要拿这个页的内容锁,并发一上来就全在等同一个缓冲区。
    现实里的样子:全局计数器表、序列号表、状态标志表 —— 行数极少、更新极频繁。

    **2026-10-24 那轮没争起来,两个原因,分别改了:**
      · 每条更新即提交、同步刷盘 → 时间全花在等 WAL 刷盘(WALFlushWait)上。现在会话级异步提交;
      · 回收水位被 V2/V3/K10 卡着,普通表每次更新留一个旧版本清不掉,100 行很快散到几百个页上,
        「热点页」不复存在(hotpage 胀到 4 MB)。现在 hotpage 是 ustore 表(原地更新,页不胀),
        见 prepare.runtime_ddl;实例不支持 ustore 时退回普通表,建表时会写明。
    三条会话各改自己那几行(id 模 3 分开),不在同一行上排行锁 —— 要的是页内容锁,不是行锁。
    """

    role = "hot"

    def setup(self, c: Conn, idx: int) -> None:
        self.async_commit(c)

    @staticmethod
    def row_for(idx: int, n: int) -> int:
        """第 idx 条会话第 n 次改哪一行:1~100 里 id 模 3 余 idx 的那些,轮着改。"""
        mine = [i for i in range(1, 101) if i % SESSIONS == idx % SESSIONS]
        return mine[n % len(mine)]

    def _loop(self, c: Conn, stop: threading.Event, idx: int) -> None:
        self.setup(c, idx)
        n = [0]

        def one() -> None:
            n[0] += 1
            c.exec("UPDATE %s.hotpage SET cnt = cnt + 1 WHERE id = %%s" % C.SCHEMA, (self.row_for(idx, n[0]),))
        until(stop, one, period_s=0.0)


@register("L3")
class ShortTransactions(_Churn):
    """极短写事务高频提交:每笔事务改一行就提交,不停地开、不停地提。

    每笔写事务都要分配事务号(XidGenLock)、提交时从活跃事务数组里摘掉自己(ProcArrayLock)——
    争的是事务号分配器与活跃事务数组。现实里的样子:连接池里几百个连接,每个都在跑「改一行就提交」的小事务。

    **2026-10-24 那轮没争起来:** 原来每笔事务只读一行(只读事务不分配事务号),而且
    BEGIN / SELECT / COMMIT 三次往返,时间全耗在客户端往返上(自证里全是 wait cmd)。
    现在:写事务 + 会话级异步提交;内核支持的话在匿名块里循环 COMMIT,一次往返连提 LOOPS 个事务。
    每条会话只改 xact_tick 里属于自己的那一行(1 / 401 / 801,分在不同页上),不排行锁、不抢页。
    """

    role = "tx"
    LOOPS = 200

    def setup(self, c: Conn, idx: int) -> None:
        self.async_commit(c)
        self._batch = commit_in_do_supported(c)
        if idx == 0:
            self.log("提交方式:%s" % ("匿名块里循环 COMMIT,一次往返 %d 个事务" % self.LOOPS
                                    if self._batch else "内核不支持块内 COMMIT,一条语句一个事务"))

    def step(self, c: Conn, idx: int) -> None:
        row = 1 + idx * 400
        if getattr(self, "_batch", False):
            c.exec("DO $$ BEGIN FOR i IN 1..%d LOOP "
                   "  UPDATE %s.xact_tick SET n = n + 1 WHERE id = %d; COMMIT; "
                   "END LOOP; END $$;" % (self.LOOPS, C.SCHEMA, row))
        else:
            c.exec("UPDATE %s.xact_tick SET n = n + 1 WHERE id = %%s" % C.SCHEMA, (row,))


@register("L4")
class PartitionLockMgr(_Churn):
    """1000 个分区的表,每条语句扫 200 个分区 —— 一次要拿两百把分区锁,语句结束就放。

    锁管理器的共享结构是按分区分段的,一次性申请上百把锁就会在那些分段上排队。
    现实里的样子:按天分区保留两三年的大表,查询没带分区键,一扫就是上千个分区。

    **2026-10-24 那轮很轻(LockMgrLock 只采到 2 次):** 原来每轮 BEGIN / SELECT / COMMIT
    三次往返,锁申请的频率被往返拖住了。现在一条语句自成一个事务(语句结束就放锁);
    内核支持的话在匿名块里循环「扫 200 个分区 → COMMIT」,每个 COMMIT 都把锁全放掉、
    下一轮重新申请,一次往返就申请几千把锁。
    """

    role = "part"
    LOOPS = 20

    def setup(self, c: Conn, idx: int) -> None:
        # 一笔事务里同时持上千把分区锁,默认的 max_locks_per_transaction=256 不够;
        # 这是**会话级**改不了的实例参数,所以一次只扫 200 个分区。
        # (不去动实例参数 —— og5 上还跑着别人的测试。)
        if self._has_guc(c):
            c.set("enable_partition_opfusion", "off")
        self._batch = commit_in_do_supported(c)

    @staticmethod
    def _has_guc(c: Conn) -> bool:
        return bool(c.one("SELECT count(*) FROM pg_settings WHERE name = 'enable_partition_opfusion'"))

    def step(self, c: Conn, idx: int) -> None:
        # LIMIT 1:200 个分区的锁在执行器启动时就全拿了,扫到第一行就收手 —— 时间花在申请锁上,
        # 不花在扫行上。原来 count(*) 要扫完 2 万行,og5 上分组验证时 CPU 占了 DB time 的 99%,
        # LockMgrLock 虽然排第一,却只占 0.4%。
        lo = (idx * 200) % 800
        if getattr(self, "_batch", False):
            c.exec("DO $$ BEGIN FOR i IN 1..%d LOOP "
                   "  PERFORM 1 FROM %s.part_log WHERE bucket >= %d AND bucket < %d LIMIT 1; COMMIT; "
                   "END LOOP; END $$;" % (self.LOOPS, C.SCHEMA, lo, lo + 200))
        else:
            c.query("SELECT 1 FROM %s.part_log WHERE bucket >= %%s AND bucket < %%s LIMIT 1"
                    % C.SCHEMA, (lo, lo + 200))


@register("L5")
class SubTransactions(_Churn):
    """两条会话:每笔事务里几百个**写**子事务(PL/pgSQL 的 EXCEPTION 块,每进一次就是一个子事务);
    第三条会话不停地读它们正在写的那张表。

    子事务数超过内核在内存里能缓存的上限(每笔事务 64 个)之后,读的一方判断「这一行是不是
    还没提交的事务写的」就得去查子事务映射 —— 争的是子事务与提交日志的共享结构。
    现实里的样子:存储过程里循环套 EXCEPTION 做「单行容错」,一次调用几百上千次循环。

    **2026-10-24 那轮一次都没争起来:** 原来子事务里只做 SELECT —— 只读的子事务不分配事务号,
    根本不进子事务映射;也没有并发读的会话。现在每个子事务插一行 subx_log(分配子事务号),
    并由第三条会话一直读这张表。
    """

    role = "sub"
    LOOPS = 300

    def role_of(self, idx: int) -> str:
        return "read" if idx == SESSIONS - 1 else self.role

    def setup(self, c: Conn, idx: int) -> None:
        # 写的两条异步提交:og5 上分组验证时这一段排第一的是 WALFlushWait(每笔大事务都在等刷盘),
        # 子事务号分配(XidGenLock)、SLRU 读写被它盖住了。
        if idx != SESSIONS - 1:
            self.async_commit(c)

    def step(self, c: Conn, idx: int) -> None:
        if idx == SESSIONS - 1:
            c.query("SELECT count(*) FROM %s.subx_log" % C.SCHEMA)
            return
        c.exec("""DO $$
                  BEGIN
                    FOR i IN 1..%d LOOP
                      BEGIN
                        INSERT INTO %s.subx_log(id, who) VALUES (i, 'L5-%d');
                      EXCEPTION WHEN others THEN NULL;
                      END;
                    END LOOP;
                  END $$;""" % (self.LOOPS, C.SCHEMA, idx))
