"""技能存档后给大盘直达链接(v1.1.6 待修第 7 项),以及 WDR 的快照窗口进索引(大盘按时间段筛选用)。

**现状(2026-10-14):** 容器版的技能把报告存进大盘,但输出里一个字都不提 —— 模型根本不知道有大盘,
用户说「生成 xx 库的健康检查大盘」时它只能自己拼 Markdown。修法放在脚本层(能用测试钉住),不靠提示词:
存档成功就在**标准错误**打一行固定格式的链接。标准错误而不是标准输出:JSON 模式的 stdout 必须是纯 JSON,
WDR 的最终报告必须是 render 的原样输出,都不能被多出来的一行破坏。
"""
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import reports  # noqa: E402


def test_link_points_at_this_instance_and_this_report():
    p = pathlib.Path("/r/health/api_10-0-0-9-postgres/20261016T072321Z.json")
    assert reports.dash_link("health", p, "api/10-0-0-9-postgres") == \
        "/dash/health?inst=api_10-0-0-9-postgres&run=20261016T072321Z.json"


def test_topsql_link_carries_the_sort_dimension_instead_of_a_file():
    """Top SQL 页按维度页签各取最近一份,没有「切到某一份」—— 链接带 by 让它打开对应页签。"""
    p = pathlib.Path("/r/topsql/k/20261016T072321Z.avg.json")
    assert reports.dash_link("topsql", p, "k", by="avg") == "/dash/topsql?inst=k&by=avg"


def test_no_link_when_nothing_was_archived_or_the_skill_has_no_page():
    assert reports.dash_link("health", None, "k") == ""
    assert reports.dash_link("sqltune", pathlib.Path("/r/x.json"), "k") == "", "调优没有自己的大盘页"


def test_announce_goes_to_stderr_only(capsys):
    reports.announce("health", pathlib.Path("/r/health/k/20261016T072321Z.json"), "k")
    out, err = capsys.readouterr()
    assert out == "", "stdout 留给报告本身(JSON 模式要纯 JSON)"
    assert "已存入大盘" in err and "/dash/health?inst=k&run=20261016T072321Z.json" in err


def test_announce_is_silent_without_an_archive(capsys):
    reports.announce("health", None, "k")
    assert capsys.readouterr() == ("", "")


def test_wdr_window_goes_into_the_index_so_the_dashboard_can_filter_by_snapshot_time(monkeypatch, tmp_path):
    """WDR 的 at 是**存档时间**,不是快照窗口 —— 按时间段筛选要按窗口筛,所以把窗口抄进索引。"""
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    win = {"begin_id": 2936, "end_id": 2937, "begin_ts": "2026-10-16 15:16:42", "end_ts": "2026-10-16 15:26:46",
           "duration_min": 10, "scope": "cluster"}
    reports.archive("wdr", {"overall": 1, "window": win}, instance="k", now="20261016T080000Z")
    idx = json.loads((tmp_path / "wdr" / "k" / "index.json").read_text(encoding="utf-8"))
    assert idx[0]["window"] == {"begin_id": 2936, "end_id": 2937,
                                "begin_ts": "2026-10-16 15:16:42", "end_ts": "2026-10-16 15:26:46"}


def test_reports_without_a_window_index_as_before(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    reports.archive("health", {"overall": 0}, instance="k", now="20261016T080000Z")
    idx = json.loads((tmp_path / "health" / "k" / "index.json").read_text(encoding="utf-8"))
    assert "window" not in idx[0]


def test_kb_link_is_the_one_shared_page(capsys):
    """知识库全体共享一份、不分实例:链接就是 /dash/kb。以前 DASH_PAGES 没有 kb,health 只存不给链接(2026-10-24)。"""
    assert reports.dash_link("kb", pathlib.Path("/r/kb/health.json"), "") == "/dash/kb"
    reports.announce("kb", pathlib.Path("/r/kb/health.json"), "")
    out, err = capsys.readouterr()
    assert out == "" and "[打开知识库大盘](/dash/kb)" in err
