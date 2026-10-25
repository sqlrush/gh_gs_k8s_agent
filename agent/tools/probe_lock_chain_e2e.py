#!/usr/bin/env python3
"""三层堵塞链的真库实测：A 等 B、B 等 C（C 持锁不放），验证 `lockwait.py`
把 A 的根阻塞会话算成 **C**、而不是链条中间那个 B —— 杀 B 解不开 A，
杀 C 才解得开，这正是 chain.py 存在的理由（见其 docstring）。

**搭链条的方式，以及为什么：** 三条会话依次对**同一张**空表申请
`ACCESS EXCLUSIVE MODE`（C 先拿到，B、A 依次排队）。这不是随手选的——
本工具跑之前先用一次性诊断脚本在真库上验证过 openGauss 的
`pg_thread_wait_status`（`lockwait.chain` 的数据源）在这种排队场景下记录
的是**队列相邻**关系而不是「最终持锁人」：

    C: sessionid=4150（holder）
    B: sessionid=4151，pg_thread_wait_status 记 4151 等 4150（即 B 等 C）
    A: sessionid=4152，pg_thread_wait_status 记 4152 等 4151（即 A 等 B）

也就是说，若只读 chain 表的第一层（不做 chain.py 的上溯），会把 A 的
阻塞者误判成 B——这正是这份探针要防的那个错误结论。`pairs.yaml`
（`pg_locks` 自连接）则是另一回事：它只认「已授权」的持有者，B 还在排队、
没有被授权，所以 A 和 B 在 `pairs` 里的 holder 都直接是 C。两条独立视图
在这个场景下给出的信息不对称，恰好覆盖了 `lockwait.py` 里
`_classify_pairs()`/`chain.roots()` 要联合处理的真实情形。

结构照 `tools/probe_lock_matrix.py`：`threading` + `Database.connect`+
显式的「拿到锁/进入等待」信号确认，不靠 `time.sleep` 猜时间——那个猜测
式的盲等正是 probe_lock_matrix.py 在 Task 3 审查里被打回的真实缺陷
（holder 还没确认拿到锁，waiter 已经抢先，整格测量作废但看不出来）。
这里同理：B/A 是否已经排上号，靠 admin 连接反复查 `pg_locks
.granted=false` 确认，不是睡一个固定时间赌它们已经排上。

用法（mac 上）：
    GSDB_HOME=~/.gdaa python3 tools/probe_lock_chain_e2e.py -c og

`-c` 必须是 `driver: psycopg2` 的连接——三条持锁会话要跨语句保持事务
（BEGIN 之后一直不提交/不回滚，直到探针主动放它们走），gsql 驱动
每条语句起独立子进程，做不到。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Optional

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # 供本文件被当模块 import

from common.db import Database  # noqa: E402

TBL = "zz_lockwait_chain_e2e_probe"
LOCKWAIT_PY = _ROOT / "skills" / "gaussdb-lockwait" / "scripts" / "lockwait.py"
PY = sys.executable or "python3"

ACQUIRE_TIMEOUT_S = 8.0     # C 确认拿到锁 / B、A 确认排上队的超时
POLL_INTERVAL_S = 0.05
JOIN_TIMEOUT_S = 15.0


class ChainSetupError(RuntimeError):
    """链条没能在超时内搭起来（拿锁 / 排队确认失败），探测作废。

    宁可在这里报错，也不要拿一个没确认过的状态去跑后面的断言——那样
    「链条没搭好」和「lockwait.py 判错了」会长得一模一样，事后分不清。
    """


class ResidueError(RuntimeError):
    """清理完毕后，pg_stat_activity 里仍能查到与探测表相关的会话。"""


def _self_ids(db) -> "tuple[int, int]":
    _, rows = db.query(
        "SELECT sessionid, pid FROM pg_stat_activity WHERE pid = pg_backend_pid()")
    if not rows:
        raise ChainSetupError("拿不到本会话的 sessionid/pid"
                              "（pg_stat_activity 还没来得及记录这一行）")
    return int(rows[0][0]), int(rows[0][1])


def _hold_access_exclusive(conn: str, ids: dict, ready: threading.Event,
                            acquired: threading.Event, stop: threading.Event,
                            errors: list) -> None:
    """开一条持久会话，BEGIN 后申请整表 ACCESS EXCLUSIVE，直到 stop 才放手。

    C/B/A 三个角色跑的是同一份函数——它们的差别只在「跑的时候表已经被
    谁占着」，不在代码里,这样三段逻辑保证完全一致，不会因为写了三份
    相近但不完全一样的代码而在某一份里悄悄漏掉信号确认。
    """
    db = None
    try:
        db = Database.connect(conn, read_only=False)
        sid, pid = _self_ids(db)
        ids["sessionid"], ids["pid"] = sid, pid
        ready.set()
        db.execute("BEGIN")
        db.execute("LOCK TABLE %s IN ACCESS EXCLUSIVE MODE" % TBL)   # 可能阻塞
        acquired.set()
        while not stop.is_set():
            time.sleep(POLL_INTERVAL_S)
        db.execute("ROLLBACK")
    except Exception as exc:   # 必须接住：否则默认 excepthook 把它印到 stderr，
        errors.append(exc)     # 主线程的等待逻辑看不出这一路已经废了
        ready.set()
        acquired.set()
    finally:
        if db is not None:
            db.close()


def _pid_is_waiting(admin, pid: int) -> bool:
    _, rows = admin.query(
        "SELECT 1 FROM pg_locks WHERE pid = %d AND granted = false LIMIT 1" % pid)
    return bool(rows)


def _wait_ready(role: str, ready: threading.Event, errors: list) -> None:
    if not ready.wait(ACQUIRE_TIMEOUT_S) or errors:
        raise ChainSetupError(
            "%s 在 %.1fs 内未能建立连接/取到 sessionid：%r"
            % (role, ACQUIRE_TIMEOUT_S, errors[:1]))


def _wait_holds(role: str, acquired: threading.Event, errors: list) -> None:
    if not acquired.wait(ACQUIRE_TIMEOUT_S) or errors:
        raise ChainSetupError(
            "%s 未能在 %.1fs 内确认拿到锁：%r" % (role, ACQUIRE_TIMEOUT_S, errors[:1]))


def _wait_queued(role: str, admin, pid: int) -> None:
    """轮询 pg_locks 确认某 pid 已经排进等待队列——不是睡一段时间赌它排上了。"""
    deadline = time.time() + ACQUIRE_TIMEOUT_S
    while time.time() < deadline:
        if _pid_is_waiting(admin, pid):
            return
        time.sleep(POLL_INTERVAL_S)
    raise ChainSetupError(
        "%s（pid %s）在 %.1fs 内未进入 pg_locks.granted=false 状态，"
        "链条没搭起来，探测作废" % (role, pid, ACQUIRE_TIMEOUT_S))


def _verify_no_residue(conn: str) -> None:
    """确认清理干净：没有任何会话的最近一条语句还提着探测表。

    用一条**新**连接查，不用 admin 自己那条——admin 刚执行过
    `DROP TABLE`，它自己在 pg_stat_activity 里的最近一条语句文本就含表名，
    用同一条连接查会把「自己」误判成残留。
    """
    checker = Database.connect(conn, read_only=True)
    try:
        _, rows = checker.query(
            "SELECT sessionid, state, query FROM pg_stat_activity "
            "WHERE query LIKE '%%%s%%' AND pid <> pg_backend_pid()" % TBL)
    finally:
        checker.close()
    if rows:
        raise ResidueError(
            "清理后 pg_stat_activity 仍有 %d 条会话引用探测表 %s：%r"
            % (len(rows), TBL, rows))


@contextmanager
def build_three_level_chain(conn: str):
    """搭好 A→B→C 三层 ACCESS EXCLUSIVE 排队链，yield (sid_a, sid_b, sid_c)。

    退出 with 块时（正常结束或异常）一律：唤醒三条持锁线程→回滚→关闭连接
    →DROP 探测表→核实无残留。任何一步搭建失败都会在 __enter__ 阶段抛
    ChainSetupError，不会把半搭好的状态交给调用方。
    """
    admin = Database.connect(conn, read_only=False)
    admin.execute("DROP TABLE IF EXISTS %s" % TBL)
    admin.execute("CREATE TABLE %s (i int)" % TBL)

    stop = threading.Event()
    roles = ("C", "B", "A")
    ids = {r: {} for r in roles}
    ready = {r: threading.Event() for r in roles}
    acquired = {r: threading.Event() for r in roles}
    errors = {r: [] for r in roles}
    threads: list = []

    def start(role: str) -> threading.Thread:
        t = threading.Thread(
            target=_hold_access_exclusive,
            args=(conn, ids[role], ready[role], acquired[role], stop, errors[role]),
            daemon=True, name="chain-%s" % role)
        threads.append(t)
        t.start()
        return t

    try:
        start("C")
        _wait_ready("C", ready["C"], errors["C"])
        _wait_holds("C", acquired["C"], errors["C"])

        start("B")
        _wait_ready("B", ready["B"], errors["B"])
        _wait_queued("B", admin, ids["B"]["pid"])

        start("A")
        _wait_ready("A", ready["A"], errors["A"])
        _wait_queued("A", admin, ids["A"]["pid"])

        yield ids["A"]["sessionid"], ids["B"]["sessionid"], ids["C"]["sessionid"]
    finally:
        stop.set()
        alive = []
        for t in threads:
            t.join(timeout=JOIN_TIMEOUT_S)
            if t.is_alive():
                alive.append(t.name)
        for role in roles:
            if errors[role]:
                print("!!! %s 线程收尾阶段出错，仅供排查：%r" % (role, errors[role][-1]),
                     file=sys.stderr)
        if alive:
            # 有线程还卡着——它大概率还在持锁或等锁，这时候硬 DROP 可能连
            # 自己都被挡住，或者更糟地在不确定状态下动数据。不猜、不清理，
            # 直接把情况摆出来让人工介入。
            admin.close()
            raise ChainSetupError(
                "以下线程在 %.1fs 内未能结束，数据库可能仍持有探测表的锁，"
                "已放弃自动清理，需要人工核查 pg_stat_activity/pg_locks："
                " %s" % (JOIN_TIMEOUT_S, ", ".join(alive)))
        admin.execute("DROP TABLE IF EXISTS %s" % TBL)
        admin.close()
        _verify_no_residue(conn)


# ---------------------------------------------------------------------------
# 解析 lockwait.py 的 markdown 输出，供本文件的 main() 和 matrix_lockwait.py 复用
# ---------------------------------------------------------------------------

_KILL_TARGET_RE = re.compile(r"pg_(?:cancel|terminate)_session\(\s*\d+\s*,\s*(\d+)\s*\)")


def _table_rows(markdown: str):
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        yield [c.strip() for c in line.strip("|").split("|")]


def root_of(markdown: str, waiter_sessionid) -> Optional[str]:
    """在「阻塞明细」表里找 waiter 那一行，返回「根阻塞会话」列的值；
    没找到这一行就返回 None（调用方要把 None 当断言失败处理，不是「未知」）。
    """
    target = str(waiter_sessionid)
    for cells in _table_rows(markdown):
        if len(cells) >= 6 and cells[0] == target:
            return cells[5]
    return None


def kill_target_sessionids(markdown: str) -> set:
    """恢复语句一节里出现过的全部 kill 目标 sessionid（去重）。"""
    return set(_KILL_TARGET_RE.findall(markdown))


# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="三层堵塞链真库实测：A 等 B、B 等 C，验证根算到 C")
    ap.add_argument("-c", "--conn", default="og",
                    help="连接名（必须是 driver: psycopg2——需要持久会话）")
    args = ap.parse_args(argv)

    try:
        with build_three_level_chain(args.conn) as (sid_a, sid_b, sid_c):
            print("链条搭好：A(会话 %s) 等 B(会话 %s) 等 C(会话 %s，持锁不放)"
                 % (sid_a, sid_b, sid_c))

            try:
                proc = subprocess.run(
                    [PY, str(LOCKWAIT_PY), "-c", args.conn],
                    capture_output=True, text=True, timeout=60)
                out, err, rc = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired:
                # 不让超时以未捕获异常的形式冒出去——那和这份探针本身要防的
                # 「用户看到一段栈」是同一类问题，不能对自己例外。
                out, err, rc = "", "lockwait.py 60s 内未返回（TIMEOUT）", -99

            print("\n--- lockwait.py 输出 ---")
            print(out)
            if err:
                print("--- stderr ---", file=sys.stderr)
                print(err, file=sys.stderr)

            failures = []
            blob = out + err
            if "Traceback (most recent call last)" in blob:
                failures.append("输出含 Traceback")
            if rc != 0:
                failures.append("退出码 %d（期望 0）：%s"
                                % (rc, (err or out)[:300]))

            root_a = root_of(out, sid_a)
            if root_a != str(sid_c):
                failures.append(
                    "A（会话 %s）的根阻塞会话解析为 %r，期望是 C（会话 %s）而不是 "
                    "中间的 B（会话 %s）——若解析成了 %s，回 chain.roots() 检查"
                    % (sid_a, root_a, sid_c, sid_b, sid_b))

            root_b = root_of(out, sid_b)
            if root_b != str(sid_c):
                failures.append(
                    "B（会话 %s）的根阻塞会话解析为 %r，期望是 C（会话 %s）"
                    % (sid_b, root_b, sid_c))

            targets = kill_target_sessionids(out)
            if targets != {str(sid_c)}:
                failures.append(
                    "kill 语句的目标会话集合是 %r，期望只有 C 一个（会话 %s）——"
                    "杀链条中间的 B 不解堵，报告不该为它单独生成 kill 语句"
                    % (targets, sid_c))

            if failures:
                print("\n断言失败：", file=sys.stderr)
                for f in failures:
                    print("  - " + f, file=sys.stderr)
                return 1

            print("\n全部断言通过：")
            print("  - A 的根阻塞会话 = C（会话 %s），不是中间的 B（会话 %s）"
                 % (sid_c, sid_b))
            print("  - kill 语句只针对 C（会话 %s）生成" % sid_c)
            print("  - 退出码 0，输出无 Traceback")
            return 0
    except ChainSetupError as exc:
        print("!!! 链条没能搭起来，探测作废：%s" % exc, file=sys.stderr)
        return 2
    except ResidueError as exc:
        print("!!! 清理后仍有残留，需人工核查：%s" % exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
