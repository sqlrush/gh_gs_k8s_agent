"""sql_id 查不到时的说明(common/sqlnotfound.py,explain 与 sqltune 共用)。

2026-10-19 测试:大盘时段报告上点「看执行计划」,语句 547475528 来自 10-17 上午,实例 10-17 21:13 重启过。
原来的报错只列「按可能性排序」的三种原因,再让用户自己去 WDR 附录找文本 —— 而重启时刻与文本,工具自己都拿得到。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import sqlnotfound  # noqa: E402
from common.grmp.errors import QueryError  # noqa: E402

SQL = "-- 成因:没有索引,故意不建\n-- 第二行说明\nUPDATE fl.acct SET bal = bal + ?\n WHERE id = ?"


class Runner:
    def __init__(self, clock=True):
        self.clock = clock

    def run(self, script, params):
        if script == "health.live_counters" and self.clock:
            return [{"item": "now", "value": "2026-10-19 09:28:30"},
                    {"item": "postmaster_start", "value": "2026-10-17 21:13:48"},
                    {"item": "stats_reset", "value": "2026-10-17 21:13:50"}]
        raise QueryError("script %s not registered" % script)


@pytest.fixture()
def archive(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    d = tmp_path / "wdr" / "api_10-0-0-9-postgres"
    d.mkdir(parents=True)
    rep = {"window": {"begin_ts": "2026-10-17 10:46", "end_ts": "2026-10-17 11:08"},
           "sql_texts": {"547475528": SQL}}
    (d / "20261019T012730Z.json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    (d / "latest.json").write_text(json.dumps({"sql_texts": {"547475528": "stale"}}), encoding="utf-8")
    return tmp_path


def test_a_statement_from_before_the_restart_gets_a_definite_reason_and_its_text(archive):
    msg = sqlnotfound.message(Runner(), "547475528")
    assert "原因已确定" in msg and "2026-10-17 21:13:48" in msg and "不是统计没开" in msg
    assert "按可能性排序" not in msg, "原因已经确定时不再让用户猜"
    assert "UPDATE fl.acct SET bal = bal + ?\n WHERE id = ?" in msg
    assert "成因" not in msg, "开头的注释不给出来:那是别人的推断,会被照抄成结论"
    assert "2 个 ?" in msg and "gaussdb-sqltune" in msg and "--sql-stdin" in msg


def test_without_an_archive_the_restart_time_is_still_stated(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    msg = sqlnotfound.message(Runner(), "42")
    assert "实例在 2026-10-17 21:13:48 启动" in msg and "按可能性排序" in msg
    assert "大盘点 sql_id 的抽屉" in msg


def test_without_clock_or_reports_dir_it_falls_back_to_the_three_causes(monkeypatch):
    monkeypatch.delenv("GSDB_REPORTS_DIR", raising=False)
    msg = sqlnotfound.message(Runner(clock=False), "42", degraded="permission denied")
    assert "按可能性排序" in msg and "实例重启过" in msg and "启动" not in msg.split("按可能性")[0]
    assert "statement_history 本身不可用:permission denied" in msg


def test_a_statement_seen_after_the_restart_is_not_blamed_on_the_restart(archive):
    rep = archive / "wdr" / "api_10-0-0-9-postgres" / "20261019T012730Z.json"
    d = json.loads(rep.read_text(encoding="utf-8"))
    d["window"] = {"begin_ts": "2026-10-18 20:14", "end_ts": "2026-10-18 21:14"}
    rep.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    msg = sqlnotfound.message(Runner(), "547475528")
    assert "原因已确定" not in msg and "按可能性排序" in msg
    assert "WDR 存档里找到了" in msg


def test_explain_by_id_uses_the_new_message(archive, monkeypatch, capsys):
    for m in ("render", "sqlfetch", "explain"):
        sys.modules.pop(m, None)
    sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-explain" / "scripts"))
    import explain as explain_mod

    class Empty(Runner):
        def run(self, script, params=None):
            if script in ("explain.from_history", "explain.from_statement"):
                return []
            return super().run(script, params)

    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: Empty())
    assert explain_mod.main(["--sql-id", "547475528"]) == 2
    err = capsys.readouterr().err
    assert "原因已确定" in err and "UPDATE fl.acct" in err
