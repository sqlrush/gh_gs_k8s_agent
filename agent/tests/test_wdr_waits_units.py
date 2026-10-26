"""WDR「等待」维度要带事件名,不只是等待类。

faultlab L1(2026-10-24 og5,快照 3149→3150):提交风暴造出的是 WALFlushWait,waitevent 第 1 窗口
排第一的就是它(315 秒);wdr collect 同一窗口只给了 LWLOCK_EVENT 50% 一行等待类聚合,看不出是 WAL 相关。
事件级增量用白名单里早就有的 waitevent.events(与 wdr.waits 同源、同一谓词),客户不用重导白名单。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-wdr" / "scripts"

# wdr 与 health 等 skill 的模块同名不同内容:照 test_wdr_units.py 的做法先清缓存、再把 wdr 的目录放最前面
for _m in ("model", "thresholds", "util", "collectors", "report",
           "native", "snaps", "interp", "recheck", "finalreport", "kbrefs", "appendix", "mdreport", "render", "ansi"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors as C                                   # noqa: E402
import thresholds                                        # noqa: E402
from common import access                                # noqa: E402
from model import Options                                # noqa: E402


class Runner:
    def __init__(self, **by_name):
        self.by_name = by_name
        self.calls = []

    def run(self, name, params=None):
        self.calls.append((name, dict(params or {})))
        out = self.by_name.get(name)
        if isinstance(out, Exception):
            raise out
        if out is None:
            raise AssertionError("测试没给 %s 的返回值" % name)
        return out


# 3149→3150 的真实量级(waitevent 与 wdr 当时的输出)
CLASSES = [{"wait_class": "LWLOCK_EVENT", "waits": "207462", "wait_us": "320200000"},
           {"wait_class": "IO_EVENT", "waits": "278255741", "wait_us": "314310000"},
           {"wait_class": "LOCK_EVENT", "waits": "190", "wait_us": "8170000"}]
EVENTS = [{"wait_class": "LWLOCK_EVENT", "event": "WALFlushWait", "waits": "163390", "wait_us": "315041687"},
          {"wait_class": "IO_EVENT", "event": "WALWrite", "waits": "1200000", "wait_us": "200000000"},
          {"wait_class": "IO_EVENT", "event": "DataFileRead", "waits": "90000", "wait_us": "80000000"},
          {"wait_class": "LWLOCK_EVENT", "event": "WALInsertLock", "waits": "40000", "wait_us": "4000000"},
          {"wait_class": "LOCK_EVENT", "event": "transactionid", "waits": "190", "wait_us": "8170000"}]


def _opt():
    return Options(begin=3149, end=3150, top=10, thresholds=thresholds.default_thresholds())


def test_each_wait_class_names_its_top_events():
    d = C.collect_waits(Runner(**{"wdr.waits": CLASSES, "waitevent.events": EVENTS}), _opt())
    assert "主要事件" in d.headers
    lw = next(r for r in d.rows if r[0] == "LWLOCK_EVENT")
    cell = lw[d.headers.index("主要事件")]
    assert cell.startswith("WALFlushWait") and "315" in cell and "WALInsertLock" in cell
    io = next(r for r in d.rows if r[0] == "IO_EVENT")
    assert io[d.headers.index("主要事件")].startswith("WALWrite")


def test_skew_finding_and_headline_name_the_event():
    d = C.collect_waits(Runner(**{"wdr.waits": CLASSES, "waitevent.events": EVENTS}), _opt())
    f = next(f for f in d.findings if f.code == "WDR_WAIT_CLASS_SKEW")
    assert "WALFlushWait" in f.evidence
    assert "WALFlushWait" in d.headline


def test_events_come_from_the_existing_whitelist_script_with_the_same_window():
    r = Runner(**{"wdr.waits": CLASSES, "waitevent.events": EVENTS})
    C.collect_waits(r, _opt())
    names = [n for n, _p in r.calls]
    assert names == ["wdr.waits", "waitevent.events"]
    ev = dict(r.calls[1][1])
    assert ev["b"] == 3149 and ev["e"] == 3150 and ev["top"] >= 20


def test_event_query_failure_keeps_the_class_table_and_says_why():
    r = Runner(**{"wdr.waits": CLASSES, "waitevent.events": access.QueryError("脚本 waitevent.events 未登记")})
    d = C.collect_waits(r, _opt())
    assert d.available and len(d.rows) == 3
    assert "事件名取不到" in d.note and "未登记" in d.note
    f = next(f for f in d.findings if f.code == "WDR_WAIT_CLASS_SKEW")
    assert "事件名取不到" in f.evidence
