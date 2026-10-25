"""死锁日志解析(gaussdb-lockwait --deadlock-log)。

样例 fixtures/deadlock/og5_sanitized.log 是 og5 2026-10-17 那份日志的原文摘段(只换了用户名、库名、地址):
2 个事务改 id 10/11、3 个事务改 id 20/21/22(环 20→21→22→20)、UPDATE 两张表交叉、INSERT 同键竞争,
连同前后夹着的「detected deadlock while waiting」「still waiting」「temporary file」这些噪声行。
其余几种形状(中文、贴进对话丢了 tab、syslog 的 #011、只复制了 ERROR 一行、多行 SQL、表锁、咨询锁)
在测试里就地构造。钉住:环与牺牲者不错、模式归类不因常量不同而拆开、给的建议对得上锁的类别。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-lockwait" / "scripts"
for _m in ("render", "lockwait", "deadlocklog", "deadlockreport", "chain", "recovery"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import deadlocklog as L  # noqa: E402
import deadlockreport as R  # noqa: E402
import lockwait  # noqa: E402

FIXTURE = _ROOT / "tests" / "fixtures" / "deadlock" / "og5_sanitized.log"
OG5 = FIXTURE.read_text(encoding="utf-8")


def test_the_og5_log_yields_four_closed_rings_with_the_right_victims():
    p = L.parse(OG5)
    assert len(p.deadlocks) == 4 and p.bare == 0
    assert all(d.closed for d in p.deadlocks)
    three = next(d for d in p.deadlocks if len(d.waits) == 3)
    assert three.at == "2026-10-17 10:48:07.273"
    assert [w.pid for w in three.waits] == ["281440914167456", "281453155636896", "281441067652768"]
    assert three.victim == "281440914167456", "牺牲者 = 第一条等待边的进程(报 ERROR 的那个)"
    assert three.victim_sql == "UPDATE fl.acct SET bal = bal + 1 WHERE id = 20"
    assert [three.sql[w.pid][-2:] for w in three.waits] == ["20", "21", "22"]
    assert {w.kind for w in three.waits} == {L.ROW}


def test_noise_lines_around_a_deadlock_are_not_read_as_waits():
    """「thread … detected deadlock while waiting for ShareLock」与「still waiting」不是等待边,也不是新的死锁。"""
    p = L.parse(OG5)
    assert sum(len(d.waits) for d in p.deadlocks) == 2 + 3 + 2 + 2


def test_the_same_statement_on_different_rows_is_one_pattern():
    text = OG5 + OG5.replace("id = 20", "id = 920").replace("id = 21", "id = 921").replace("id = 22", "id = 922")
    pats = L.group(L.parse(text).deadlocks)
    three = [p for p in pats if p.size == 3]
    assert len(three) == 1 and len(three[0].events) == 2
    assert three[0].shapes == ("UPDATE fl.acct SET bal = bal + ? WHERE id = ?",) * 3


def test_advice_follows_the_kind_of_ring():
    by_size = {(p.size, p.shapes[0][:6]): L.advice(p) for p in L.group(L.parse(OG5).deadlocks)}
    assert "按同一顺序" in by_size[(3, "UPDATE")] and "FOR UPDATE" in by_size[(3, "UPDATE")]
    assert "同键插入" in by_size[(2, "INSERT")]
    mixed = next(p for p in L.group(L.parse(OG5).deadlocks) if len(set(p.shapes)) == 2)
    assert "交叉加行锁" in L.advice(mixed)


def test_a_pasted_block_without_tabs_or_prefix_still_parses():
    """贴进对话时 tab 与前缀常常丢掉。"""
    text = ("ERROR:  deadlock detected\nDETAIL:  Process 11 waits for ShareLock on transaction 900; blocked by process 22.\n"
            "Process 22 waits for ShareLock on transaction 901; blocked by process 11.\n"
            "Process 11: update t set v = 1 where id = 1\nProcess 22: update t set v = 1 where id = 2\n")
    d = L.parse(text).deadlocks[0]
    assert d.closed and d.victim == "11" and d.at == "" and d.sql["22"].endswith("id = 2")


def test_syslog_tab_escapes_are_understood():
    text = OG5.replace("\n\tProcess", "#011Process")
    p = L.parse(text)
    assert len(p.deadlocks) == 4 and all(d.closed for d in p.deadlocks)


def test_a_multi_line_statement_is_kept_whole():
    text = ("2026-10-17 10:00:00.000 x ERROR:  deadlock detected\n"
            "2026-10-17 10:00:00.000 x DETAIL:  Process 1 waits for ShareLock on transaction 5; blocked by process 2.\n"
            "\tProcess 2 waits for ShareLock on transaction 6; blocked by process 1.\n"
            "\tProcess 1: UPDATE t\n\t   SET v = v + 1\n\t WHERE id = 7\n"
            "\tProcess 2: UPDATE t SET v = v + 1 WHERE id = 8\n"
            "2026-10-17 10:00:00.000 x HINT:  See server log for query details.\n")
    d = L.parse(text).deadlocks[0]
    assert d.sql["1"] == "UPDATE t SET v = v + 1 WHERE id = 7"


def test_chinese_messages_are_parsed_best_effort():
    text = ("2026-10-17 10:00:00 x 错误:  检测到死锁\n"
            "2026-10-17 10:00:00 x 详细信息:  进程7等待在事务 300上的ShareLock; 由进程8阻塞.\n"
            "\t进程8等待在事务 301上的ShareLock; 由进程7阻塞.\n\t进程7: update t set v=1 where id=1\n"
            "\t进程8: update t set v=1 where id=2\n")
    d = L.parse(text).deadlocks[0]
    assert d.closed and d.victim == "7" and d.waits[0].kind == L.ROW and d.sql["8"].endswith("id=2")


def test_table_and_advisory_rings_get_their_own_advice():
    rel = ("ERROR:  deadlock detected\nDETAIL:  Process 1 waits for AccessExclusiveLock on relation 16384 of database 13000;"
           " blocked by process 2.\nProcess 2 waits for AccessShareLock on relation 16390 of database 13000; blocked by process 1.\n")
    adv = ("ERROR:  deadlock detected\nDETAIL:  Process 1 waits for ExclusiveLock on advisory lock [13000,0,7,1];"
           " blocked by process 2.\nProcess 2 waits for ExclusiveLock on advisory lock [13000,0,8,1]; blocked by process 1.\n")
    (p_rel,) = L.group(L.parse(rel).deadlocks)
    (p_adv,) = L.group(L.parse(adv).deadlocks)
    assert p_rel.kinds == (L.TABLE,) and "表级锁环" in L.advice(p_rel)
    assert p_adv.kinds == (L.ADVISORY,) and "pg_advisory_lock" in L.advice(p_adv)


def test_only_the_error_line_is_reported_as_incomplete_not_as_no_deadlock():
    p = L.parse("2026-10-17 10:47:32.698 x ERROR:  deadlock detected\n")
    assert p.deadlocks == () and p.bare == 1
    out = R.render_markdown(p)
    assert "DETAIL 几行一起给我" in out


def test_a_broken_ring_is_flagged():
    text = ("ERROR:  deadlock detected\nDETAIL:  Process 1 waits for ShareLock on transaction 5; blocked by process 2.\n"
            "Process 2 waits for ShareLock on transaction 6; blocked by process 3.\n")
    d = L.parse(text).deadlocks[0]
    assert d.closed is False
    assert "首尾没有接上" in R.render_markdown(L.parse(text))


def test_markdown_names_the_victim_and_explains_transaction_locks():
    out = R.render_markdown(L.parse(OG5))
    assert "共解析出 **4** 次死锁" in out and "归成 **4** 种模式" in out
    assert "281440914167456（**牺牲者**，事务被回滚）" in out
    assert "等 281453155636896 的事务结束（对方已锁住它要改的行）" in out
    assert "40P01" in out and "重试整个事务" in out


def test_the_cli_reads_stdin_and_never_connects(monkeypatch, capsys):
    monkeypatch.setattr(lockwait.access, "for_conn", lambda *a, **k: pytest.fail("解析日志不连库"))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(OG5))
    assert lockwait.main(["--deadlock-log", "-", "--format", "json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["deadlocks"] == 4 and d["patterns"][0]["latest"]["closed"] is True
    assert {p["size"] for p in d["patterns"]} == {2, 3}


def test_the_cli_reports_an_unreadable_file(capsys):
    assert lockwait.main(["--deadlock-log", "/nonexistent/x.log"]) == 2
    assert "读不了日志" in capsys.readouterr().err


def test_every_deadlock_count_points_at_the_parser():
    """WDR、健康检查(此刻 / 时段)报死锁次数时,都要说「把日志段贴给我」并点名解析入口。"""
    import re as _re
    srcs = {"wdr": _ROOT / "skills/gaussdb-wdr/scripts/collectors.py",
            "health": _ROOT / "skills/gaussdb-health/scripts/collectors.py",
            "health-window": _ROOT / "skills/gaussdb-health/scripts/window.py"}
    for name, path in srcs.items():
        text = path.read_text(encoding="utf-8")
        assert "deadlock detected" in text and _re.search(r"贴(过来|给我)", text), name
    for name in ("wdr", "health", "health-window"):
        assert "--deadlock-log" in srcs[name].read_text(encoding="utf-8"), name
