"""会话模型:一条会话 = 一个线程 + 一条自己的 psycopg2 连接。

**为什么必须一人一条连接:** 锁、事务、等待状态在数据库里都是按后端算的。两个场景共用
一条连接的话,它们的事务会被搅在一起,造出来的现象就不是设计的那个,而人看输出根本看不出来。

**为什么要有「预期会报错」这档:** 死锁场景里牺牲者**一定**会收到报错,唯一键冲突、
锁超时也一样。把它们当失败的话,整个运行会被自己的设计意图判成红。所以每个会话都要
声明自己预期什么错:`expect_errors=True` 的会话把报错计数、照常继续;没声明的会话
一出错就记进 errors,运行结束时如实报出来。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import config as C
from .db import Conn

POLL_S = 0.2
# 等一条会话自己退出最多等这么久。等不到也不强等:它多半正挂在锁等待里,
# 而 cleanup 紧接着会把数据库会话杀掉 —— 会话一断,那条 SQL 就返回,线程随即结束。
# (线程都是 daemon,不会挡住进程退出。)
JOIN_TIMEOUT_S = 8.0


class Ctx:
    """跑一次的共享上下文:全局停止信号、时间线日志、错误与计数、这次的窗口长度。"""

    def __init__(self, log: Callable[[str], None], window_s: int = C.WINDOW_S):
        self.stop = threading.Event()
        self.log = log
        self.window_s = window_s
        self._lock = threading.Lock()
        self.errors: List[Dict[str, str]] = []
        self.counters: Dict[str, int] = {}

    def error(self, who: str, exc: BaseException) -> None:
        with self._lock:
            self.errors.append({"who": who, "error": "%s: %s" % (type(exc).__name__,
                                                                 str(exc).splitlines()[0][:200])})

    def bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + n

    def snapshot_counters(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.counters)


# 停场景时我们自己取消 / 终止会话,会话那边收到的就是这几种报错;
# 最后一种是按场景时段设的等锁上限到了(lock_guard_s)—— 也是我们自己设的兜底。
_STOPPED_TEXTS = ("server closed the connection", "terminating connection",
                  "connection already closed", "no connection to the server",
                  "canceling statement due to user request", "lock wait timeout")


def stopped_on_purpose(error_text: str, stopping: bool) -> bool:
    """收工阶段「语句被取消 / 连接被断开」是我们自己干的,不算故障。

    cleanup 会 pg_terminate_session 掉所有造故障的会话、停场景时会 pg_cancel_session 掉
    还挂着的语句 —— 那几条会话必然收到上面这些报错。第一版把它们记成了出错会话,
    于是每次运行都带着两三条假错误收场(2026-10-17 全量跑时 S3/S5 就是这样)。
    只在**停止位已经置上**(本场景停或全局停)之后才这么宽容,别把运行中途真的断连也掩盖掉。
    """
    if not stopping:
        return False
    text = (error_text or "").lower()
    return any(t in text for t in _STOPPED_TEXTS)


class Worker:
    """一条会话。fn(conn, stop) 跑在自己的线程里;stop 被 set 后应尽快返回。

    连接在线程里建、在线程里关 —— psycopg2 的连接不是线程安全的,不能在主线程建好
    再交给别人用。

    lock_guard_s  这条会话最多等一把锁多久(会话级 lockwait_timeout,按场景时段算)。
    drain         场景按排期停时**不打断**正在跑的那条语句,让它跑完(慢 SQL 的最后一轮:
                  打断了就进不了执行历史);只有全局停(窗口结束)时才取消。
    terminate     停的时候不取消语句、直接终止会话。**慢 SQL 用它:** 被取消的语句在 openGauss 里
                  照样记进 dbe_perf.statement(2026-10-24 第二轮:S1 被取消后在执行历史里出现了
                  「1 次、1182 秒、CPU 0」),一直跑不完的 S1/S2 就不再「历史里没有」;终止会话则不记。
    """

    def __init__(self, ctx: Ctx, app: str, fn: Callable[[Conn, threading.Event], None], *,
                 autocommit: bool = True, guards: bool = True, expect_errors: bool = False,
                 lock_guard_s: Optional[int] = None, drain: bool = False, terminate: bool = False):
        self.ctx = ctx
        self.app = app
        self.drain = drain
        self.terminate = terminate
        self._fn = fn
        self._autocommit = autocommit
        self._guards = guards
        self._lock_guard_s = lock_guard_s
        self._expect_errors = expect_errors
        self._own_stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.started = threading.Event()        # 连接已建好(不代表已拿到锁)

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> "Worker":
        self._thread = threading.Thread(target=self._run, name=self.app, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        conn = None
        try:
            conn = Conn(self.app, autocommit=self._autocommit, guards=self._guards,
                        lockwait_s=self._lock_guard_s)
            self.started.set()
            self._fn(conn, self._stop_view())
        except BaseException as exc:            # noqa: BLE001 —— 线程里不接就只会打到 stderr
            stopping = self._own_stop.is_set() or self.ctx.stop.is_set()
            if self._expect_errors or stopped_on_purpose(str(exc), stopping):
                self.ctx.bump("expected_errors")
            else:
                self.ctx.error(self.app, exc)
            self.started.set()
        finally:
            if conn is not None:
                conn.rollback()
                conn.close()

    def _stop_view(self) -> threading.Event:
        """合并「本会话停」与「全局停」。

        线程里只认一个 Event 最简单,所以起一个小看门线程把两者或起来 ——
        比在每个场景里到处写 `if a.is_set() or b.is_set()` 不容易漏。
        """
        merged = threading.Event()

        def watch() -> None:
            while not merged.is_set():
                if self._own_stop.is_set() or self.ctx.stop.is_set():
                    merged.set()
                    return
                time.sleep(POLL_S)

        threading.Thread(target=watch, name=self.app + "-watch", daemon=True).start()
        return merged

    def signal(self) -> None:
        """只置停止位,不等。

        **为什么要和 join 分开:** 一条正挂在锁等待里的会话,要等那条 SQL 返回才能回到
        Python 代码里看停止位 —— 不会主动醒。如果边置位边 join,6 条这样的会话就要
        串着各等一遍超时(实测收工花了 60 秒)。先把所有人的位都置上,再一起等,
        慢的也只慢一次。
        """
        self._own_stop.set()

    def stop(self, join_s: float = JOIN_TIMEOUT_S) -> None:
        self.signal()
        self.join(join_s)

    def join(self, timeout_s: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_s))

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


# ---------------------------------------------------------------- 停一组会话

def _db_killer() -> Tuple[Callable[[str, str], int], Callable[[], None]]:
    """真库上的「停这条会话」:懒建一条观测级连接(fl:stop),用完关掉。"""
    from .db import OBSERVER_GUARDS, stop_sessions
    holder: List[Conn] = []

    def kill(app: str, action: str) -> int:
        if not holder:
            holder.append(Conn("stop", guards=OBSERVER_GUARDS))
        return stop_sessions(holder[0], app, action)

    def close() -> None:
        for c in holder:
            c.close()
    return kill, close


def _join_all(workers: List["Worker"], timeout_s: float) -> None:
    """一起等,总共最多 timeout_s 秒(不是每条各等一遍)。"""
    deadline = time.time() + timeout_s
    for w in workers:
        w.join(deadline - time.time())


def stop_workers(workers: List["Worker"], ctx: Ctx, *,
                 killer: Optional[Callable[[str, str], int]] = None,
                 grace_s: float = C.STOP_GRACE_S, cancel_grace_s: float = C.CANCEL_GRACE_S,
                 join_s: float = JOIN_TIMEOUT_S) -> Dict[str, int]:
    """停一组会话,**确保挂在锁等待里的语句也停得下来**。

    只置停止位是不够的:正在等锁的会话要等那条 SQL 返回才回到 Python 里看停止位。
    2026-10-24 全量跑时 K3 按排期停下,它的 ALTER TABLE 却一直排在 fl.payments 的锁队列里,
    把后面的 S5、status、explain、health 全堵住了。所以分三步:
      ① 全部置停止位,一起等 grace_s —— 自己会收的(空闲持锁者、循环里的会话)在这一步就收了;
      ② 还活着的,按 application_name 取消当前语句(pg_cancel_session),再等 cancel_grace_s;
      ③ 还活着的,终止会话(pg_terminate_session)。
    drain 会话(慢 SQL 的最后一轮)在场景按排期停时只置位、不打断、不等;全局停时照样取消。

    killer(app, action) 返回动了几条会话;单测里换成替身,不连库。
    返回 {"cancelled": n, "terminated": n, "left_running": n}。
    """
    for w in workers:
        w.signal()
    targets = [w for w in workers if ctx.stop.is_set() or not w.drain]
    out = {"cancelled": 0, "terminated": 0, "left_running": len(workers) - len(targets)}
    _join_all(targets, grace_s)
    stuck = [w for w in targets if w.alive]
    if not stuck:
        return out
    close: Callable[[], None] = lambda: None
    if killer is None:
        killer, close = _db_killer()

    def act(w: "Worker", action: str) -> int:
        # 停不掉一条会话不能让整个编排器跟着出错:记下来,后面还有会话级等锁上限与 cleanup 兜着
        try:
            return killer(w.app, action)
        except Exception as exc:                 # noqa: BLE001
            ctx.log("停 %s 时 %s 失败:%s" % (w.app, action, str(exc).splitlines()[0][:100]))
            return 0
    try:
        for w in stuck:
            if not getattr(w, "terminate", False):
                out["cancelled"] += act(w, "cancel")
        if any(not getattr(w, "terminate", False) for w in stuck):
            _join_all(stuck, cancel_grace_s)
        stuck = [w for w in stuck if w.alive]
        for w in stuck:
            out["terminated"] += act(w, "terminate")
        _join_all(stuck, join_s)
    finally:
        close()
    return out


# ---------------------------------------------------------------- 常用循环

def until(stop: threading.Event, step: Callable[[], None], *, period_s: float = 0.0,
          on_error: Optional[Callable[[BaseException], None]] = None) -> None:
    """反复跑 step 直到 stop。

    step 抛错时:给了 on_error 就交给它(死锁场景用来计数并继续),没给就抛出去
    (由 Worker 记进 errors)。**不会静默吞掉** —— 悄悄吞错会让「场景没生效」
    看起来和「场景正常跑着」一模一样。
    """
    while not stop.is_set():
        t0 = time.time()
        try:
            step()
        except BaseException as exc:            # noqa: BLE001
            if on_error is None:
                raise
            on_error(exc)
        left = period_s - (time.time() - t0)
        if left > 0:
            stop.wait(left)


def sleep_until(stop: threading.Event, seconds: float) -> None:
    """可被打断的 sleep。"""
    stop.wait(seconds)


def wait_for(cond: Callable[[], bool], timeout_s: float, *, interval_s: float = POLL_S) -> bool:
    """等一个条件成立(比如「等锁会话已经排上队」)。

    **不用固定 sleep 猜时间** —— tools/probe_lock_matrix.py 在审查里被打回过的
    就是这个:holder 还没确认拿到锁,waiter 已经抢先,测量作废却看不出来。
    """
    end = time.time() + timeout_s
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval_s)
    return False


def app_name(code: str, role: str = "") -> str:
    """application_name 里的名字:`fl:K1/root`。status 与 cleanup 按 `fl:` 前缀认人。"""
    return "%s/%s" % (code, role) if role else code


def tagged(code: str, sql: str) -> str:
    """给 SQL 带上场景编号的注释,让 Top SQL / 慢 SQL 的输出里能认出是哪一项。"""
    return "/* %s%s */ %s" % (C.APP_PREFIX, code, sql)
