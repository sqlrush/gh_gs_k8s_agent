"""health 的 WAL 维度(2026-11-05):xlog 生成速度 + 写入来源(表 / 大字段 / SQL / 会话)。"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-health" / "scripts"))

import wal  # noqa: E402
from common import guc_policy  # noqa: E402
from common.grmp.errors import QueryError  # noqa: E402
from model import Severity  # noqa: E402
from thresholds import Thresholds  # noqa: E402

TH = Thresholds()


def _tw(schema, table, ins, upd=0, dele=0, toast=False, toast_bytes=None):
    return {"schema": schema, "table": table, "is_toast": "t" if toast else "f", "n_tup_ins": str(ins),
            "n_tup_upd": str(upd), "n_tup_del": str(dele), "toast_bytes": "" if toast_bytes is None else str(toast_bytes)}


class _Runner:
    def __init__(self, lsn1="1B7/00000000", lsn2="1B7/12C00000", sessions=None, sessions_error=None,
                 t1=None, t2=None, sqls=None):
        self.pos = [{"lsn": lsn1, "at": "2026-11-05 10:00:00.000000"}, {"lsn": lsn2, "at": "2026-11-05 10:00:05.000000"}]
        self.tw = [t1 if t1 is not None else [_tw("app", "orders", 1000), _tw("app", "docs", 10, toast=False),
                                              _tw("app", "docs", 50, toast=True, toast_bytes=10 << 20)],
                   t2 if t2 is not None else [_tw("app", "orders", 1200), _tw("app", "docs", 2010),
                                              _tw("app", "docs", 8050, toast=True, toast_bytes=900 << 20)]]
        self.sessions, self.sessions_error = sessions, sessions_error
        self.sqls = sqls if sqls is not None else [
            {"unique_sql_id": "777", "query": "INSERT INTO app.docs VALUES (?, ?)", "calls": "2000",
             "rows_written": "2000", "total_sec": "3.1"}]

    def run(self, script, values=None):
        if script == "health.xlog_position":
            return [self.pos.pop(0)]
        if script == "health.table_writes":
            return self.tw.pop(0)
        if script == "health.write_sql":
            return self.sqls
        if script == "health.write_sessions":
            return getattr(self, "writers", [])
        if script == "health.xlog_by_session":
            if self.sessions_error:
                raise QueryError(self.sessions_error)
            return self.sessions or []
        if script.startswith("syssql") or "full_text" in script or script.endswith("from_statement"):
            return []
        raise AssertionError(script)


def setup_module(_m):
    wal._sleep = lambda _s: None


def test_lsn_bytes():
    assert wal.lsn_bytes("1B7/3DC7BAF0") == (0x1B7 << 32) + 0x3DC7BAF0
    assert wal.lsn_bytes("bad") is None


def test_rate_and_sources_with_large_fields():
    d = wal.collect_wal(_Runner(), TH, 10)            # 0x12C00000 字节 / 5 秒 = 60 MB/s
    f = {x.code: x for x in d.findings}["WAL_RATE_HIGH"]
    assert f.severity == Severity.WARN and f.value.startswith("60.00")
    assert "app.docs" in f.evidence and "大字段写入 8000 行" in f.evidence and "sql_id 777" in f.evidence
    assert "分批提交" in f.evidence and not guc_policy.violations(f.evidence)
    assert d.rows[0][0] == "xlog" and any("900" in "".join(r) or "MB" in r[3] for r in d.rows[1:])
    assert "本库没有" not in (d.note or "")


def test_sessions_on_gaussdb_come_first():
    s = [{"sessionid": "4840", "pid": "1", "usename": "app", "application_name": "doc-import", "state": "active",
          "xlog_quantity": str(512 << 20), "unique_sql_id": "777", "query": "INSERT INTO app.docs ..."}]
    d = wal.collect_wal(_Runner(sessions=s), TH, 10)
    ev = d.findings[0].evidence
    assert ev.index("会话 4840") < ev.index("app.docs") and "doc-import" in ev


def test_opengauss_without_xlog_quantity_says_so():
    d = wal.collect_wal(_Runner(sessions_error='column "xlog_quantity" does not exist'), TH, 10)
    assert "本库没有按会话的 xlog 用量" in d.note


def test_quiet_rate_has_no_finding_and_no_writes_says_not_located():
    d = wal.collect_wal(_Runner(lsn2="1B7/00100000", t1=[], t2=[], sqls=[]), TH, 10)
    assert not d.findings and "采样期间无表级写入" in d.headline


def test_unlocated_source_is_said_plainly():
    d = wal.collect_wal(_Runner(t1=[], t2=[], sqls=[]), TH, 10)
    assert "尚未定位到引起它的业务" in d.findings[0].evidence


def test_standby_is_not_an_error():
    class _Standby(_Runner):
        def run(self, script, values=None):
            if script == "health.xlog_position":
                raise QueryError("ERROR: recovery is in progress")
            return super().run(script, values)
    d = wal.collect_wal(_Standby(), TH, 10)
    assert not d.available and "备机" in d.note


def test_long_running_writer_is_named_when_table_counters_lag():
    """存储过程里批量写入:表计数要等 CALL 结束才累加,采样期间没有表增量 —— 靠「正在写的会话」说出是谁。"""
    r = _Runner(t1=[], t2=[], sqls=[])
    r.writers = [{"sessionid": "77", "pid": "1", "usename": "app", "application_name": "doc-import",
                  "query_age_s": "42.5", "unique_sql_id": "9", "query": "CALL drill.load_docs(900);"}]
    d = wal.collect_wal(r, TH, 10)
    ev = d.findings[0].evidence
    assert "正在执行写入的会话 77" in ev and "doc-import" in ev and "load_docs" in ev
    assert "尚未定位" not in ev
