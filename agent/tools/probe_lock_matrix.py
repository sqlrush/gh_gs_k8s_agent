#!/usr/bin/env python3
"""在真库上把 8x8 锁互斥矩阵撞出来，与 common/lockmodes.py 的表逐格比对。

用法（mac 上）：
    GSDB_HOME=~/.gdaa python3 tools/probe_lock_matrix.py -c og

一条会话持 A 模式，另一条请求 B 模式；能在超时内拿到就是不互斥，
被挡住就是互斥。需要持久会话，所以只能用 driver: psycopg2 的连接。

**这是矩阵的事实来源。** common/lockmodes.py 里的表若与本工具的结果不一致，
以本工具为准 —— 表是人写的，撞出来的是数据库说的。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import threading
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.db import Database                       # noqa: E402
from common.lockmodes import LOCK_MODES, conflicts   # noqa: E402

TBL = "zz_lock_matrix_probe"
ACQUIRE_TIMEOUT_S = 2.0            # waiter 超过这个时间没拿到，判定为被挡住
HOLDER_ACQUIRE_TIMEOUT_S = 5.0     # holder 超过这个时间没**确认**拿到，测量作废——不猜


class HolderAcquireError(RuntimeError):
    """holder 没能在超时内确认拿到锁，或持锁期间出了错。

    这一格的测量没有意义，不能假装它成立——一个没确认过 holder 是否真的
    拿到锁的格子，比矩阵里空着这一格更危险：它看上去和真实测量一模一样。
    """


def _lock_sql(mode: str) -> str:
    """把 pg_locks 的拼写换成 LOCK TABLE 的语法（AccessShareLock → ACCESS SHARE）。"""
    body = mode[:-4] if mode.endswith("Lock") else mode      # 去掉结尾的 Lock
    out = []
    for ch in body:
        if ch.isupper() and out:
            out.append(" ")
        out.append(ch.upper())
    return "".join(out)


def measure(conn: str, holder_mode: str, waiter_mode: str) -> bool:
    """返回 True 表示实测互斥（waiter 在超时内没拿到锁）。

    holder 必须**确认**拿到锁（LOCK TABLE 执行返回后发个信号）才能放行 waiter，
    不能靠睡一个固定时间去赌它已经拿到了：Database.connect() 本身有网络/鉴权
    开销，一旦这个开销超过原来写死的 0.4s，waiter 就会抢在 holder 之前拿到锁，
    把一对本该互斥的模式误判成不互斥——而且这种误判在打印出来的矩阵里跟
    真实测量长得一模一样，事后没法分辨。
    """
    acquired = threading.Event()
    got = threading.Event()
    stop = threading.Event()
    holder_errors: list = []

    def holder():
        db = None
        try:
            db = Database.connect(conn, read_only=False)
            db.execute("BEGIN")
            db.execute("LOCK TABLE %s IN %s MODE" % (TBL, _lock_sql(holder_mode)))
            acquired.set()
            while not stop.is_set():
                time.sleep(0.05)
            db.execute("ROLLBACK")
        except Exception as exc:   # 必须捕住：否则异常被默认 excepthook 印到
            holder_errors.append(exc)  # stderr，主线程的输出里完全看不出这一格作废了
            acquired.set()              # 唤醒主线程，不要让它傻等到超时才发现
        finally:
            if db is not None:
                db.close()

    def waiter():
        db = Database.connect(conn, read_only=False)
        try:
            db.execute("BEGIN")
            db.execute("LOCK TABLE %s IN %s MODE" % (TBL, _lock_sql(waiter_mode)))
            got.set()
            db.execute("ROLLBACK")
        except Exception:
            pass
        finally:
            db.close()

    th = threading.Thread(target=holder, daemon=True)
    th.start()
    confirmed = acquired.wait(HOLDER_ACQUIRE_TIMEOUT_S)
    if not confirmed or holder_errors:
        stop.set()
        th.join(timeout=10)
        if holder_errors:
            raise HolderAcquireError(
                "holder 在持有 %s 期间出错，这一格（holder=%s waiter=%s）测量作废：%r"
                % (holder_mode, holder_mode, waiter_mode, holder_errors[0])
            ) from holder_errors[0]
        raise HolderAcquireError(
            "holder 在 %.1fs 内未确认拿到 %s 上的锁，这一格（holder=%s waiter=%s）"
            "测量作废——宁可报错也不要猜它已经拿到了"
            % (HOLDER_ACQUIRE_TIMEOUT_S, holder_mode, holder_mode, waiter_mode))

    tw = threading.Thread(target=waiter, daemon=True)
    tw.start()
    blocked = not got.wait(ACQUIRE_TIMEOUT_S)
    stop.set()
    th.join(timeout=10)
    tw.join(timeout=10)
    if holder_errors:
        # 拿到锁之后、收尾阶段（ROLLBACK/关闭连接）才出的错：这一格的测量结果
        # 已经有效（波及不到 blocked 的判定），但异常不能被默默吞掉。
        print("!!! holder(%s) 收尾阶段出错，不影响本格已测出的结果，仅供排查：%r"
              % (holder_mode, holder_errors[-1]), file=sys.stderr)
    return blocked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--conn", default="og",
                    help="连接名（必须是 driver: psycopg2 —— 需要持久会话）")
    args = ap.parse_args()

    admin = Database.connect(args.conn, read_only=False)
    admin.execute("DROP TABLE IF EXISTS %s" % TBL)
    admin.execute("CREATE TABLE %s (i int)" % TBL)
    mismatches = []
    try:
        print("%-26s %s" % ("holder \\ waiter", " ".join(
            "%2d" % (i + 1) for i in range(len(LOCK_MODES)))))
        for h in LOCK_MODES:
            row = []
            for w in LOCK_MODES:
                try:
                    actual = measure(args.conn, h, w)
                except HolderAcquireError as exc:
                    # 探测工具本身不可信了，不能继续假装后面的格子有意义。
                    # finally 里的 DROP/close 仍会跑，锁和会话不会遗留。
                    print("\n!!! 探测中止：%s" % exc, file=sys.stderr)
                    return 2
                expected = conflicts(h, w)
                row.append("X" if actual else ".")
                if actual != expected:
                    mismatches.append((h, w, expected, actual))
            print("%-26s %s" % (h, "  ".join(row)))
    finally:
        admin.execute("DROP TABLE IF EXISTS %s" % TBL)
        admin.close()

    if mismatches:
        print("\n!!! 与 common/lockmodes.py 不一致的格子（以实测为准）：")
        for h, w, e, a in mismatches:
            print("  holder=%s waiter=%s 表里=%s 实测=%s" % (h, w, e, a))
        return 1
    print("\n8x8 全部与 common/lockmodes.py 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
