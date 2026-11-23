"""校准慢 SQL 的规模:实测小规模耗时,按比例推出「跑到目标耗时」要多大的规模。

**为什么不能写死规模:** S3 / S4 / S5 的代价结构是「外层行数 × 内层全表扫描」,
机器快一倍,同一个规模的耗时就减一半。写死一个数,换台机器(或者 og5 上别的测试
正在占 CPU)就会变成「两秒就跑完了」或者「一天也跑不完」,两种都让场景失去意义。

**怎么推:** 先用一个很小的规模实测,得到「每行外层大约多少毫秒」,再按目标耗时折算。
S3/S4/S5 是严格线性的(每行一次全表扫);S6 是排序,n log n,线性外推会偏大,
所以给它打 0.8 的折 —— 宁可比目标短一点,也不要长到跑不完。

校准结果写进 ~/.faultlab/caps.json,run 时直接用。**换机器 / 换数据量后要重跑一次。**
"""
from __future__ import annotations

import time
from typing import Dict, Optional

from . import config as C
from .db import Conn, LabError, assert_expected_instance
from .scenarios import slow

# 试跑用的小规模。取值原则:在 og5 上几秒钟能跑完,又不至于太小而被固定开销淹没。
PROBE_CAP: Dict[str, int] = {"S3": 300, "S4": 300, "S5": 300, "S6": 20_000}
PROBE_TIMEOUT_S = 120
MIN_PROBE_S = 0.5               # 试跑快于这个就说明规模太小,加大重试
SUBLINEAR = {"S6": 0.8}         # 排序类的折扣,见 docstring
CAP_LIMIT = {"S3": 1_000_000, "S4": 1_000_000, "S5": C.IMPORT_ROWS, "S6": 1_000_000}


def _probe_once(c: Conn, code: str, cap: int) -> Optional[float]:
    """按给定规模跑一次,返回耗时;超时返回 None(不是错误 —— 说明规模已经偏大)。"""
    spec = slow.SPECS[code]
    c.set("work_mem", spec.work_mem)
    c.set("statement_timeout", "%ds" % PROBE_TIMEOUT_S)
    t0 = time.time()
    try:
        c.query(slow.sql_for(code, cap))
    except Exception as exc:                     # noqa: BLE001
        if "timeout" in str(exc).lower() or "canceling" in str(exc).lower():
            c.rollback()
            return None
        raise
    return time.time() - t0


def _calibrate_one(c: Conn, code: str, target_s: int) -> Dict[str, object]:
    """把一个慢 SQL 校准到目标耗时附近。"""
    cap = PROBE_CAP[code]
    took = None
    for _ in range(4):                           # 太快就翻倍重试,最多四轮
        took = _probe_once(c, code, cap)
        if took is None:                         # 已经超时 → 规模砍半再试
            cap = max(PROBE_CAP[code] // 4, 50)
            continue
        if took >= MIN_PROBE_S:
            break
        cap = min(cap * 8, CAP_LIMIT[code])
    if took is None or took <= 0:
        raise LabError("%s 校准失败:试跑规模 %s 时 %d 秒还没跑完" % (code, cap, PROBE_TIMEOUT_S))
    per_row = took / cap
    want = int(target_s / per_row * SUBLINEAR.get(code, 1.0))
    want = max(100, min(want, CAP_LIMIT[code]))
    return {"试跑规模": cap, "试跑耗时": "%.1fs" % took,
            "每行约": "%.2fms" % (per_row * 1000), "定为": want,
            "预计耗时": "%.0fs" % (want * per_row * SUBLINEAR.get(code, 1.0))}


def run(target_s: int = 12 * 60) -> Dict[str, object]:
    """校准四条「要跑完」的慢 SQL。S1 / S2 设计上跑不完,没有规模可校。

    target_s 只是**上限意义上的参考**:每条 SQL 按自己 spec 里的 target_s 来,
    命令行给的值用于整体缩放(比如想让整轮短一些就给小一点)。
    """
    if target_s < 30 or target_s > 3600:
        raise LabError("目标耗时要在 30~3600 秒之间,给的是 %r" % (target_s,))
    c = Conn("calibrate")
    out: Dict[str, object] = {}
    try:
        assert_expected_instance(c)
        # 从 SPECS 的默认值起,不从 caps.json 起 —— 否则上一次校准留下的旧值会把
        # 代码里改过的默认值盖掉(S1 的默认规模改大过一次,就被旧文件按回去了)。
        caps = {code: spec.cap for code, spec in slow.SPECS.items()}
        for code in ("S3", "S4", "S5", "S6"):
            spec = slow.SPECS[code]
            want_s = min(spec.target_s, target_s)
            print("  校准 %s(目标 %d 秒)…" % (code, want_s), flush=True)
            info = _calibrate_one(c, code, want_s)
            caps[code] = int(info["定为"])
            out[code] = info
        slow.save_caps(caps)
        out["写入"] = str(slow.CAPS_FILE)
    finally:
        c.close()
    return out
