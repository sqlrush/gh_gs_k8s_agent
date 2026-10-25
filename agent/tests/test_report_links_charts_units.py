"""报告链接带时间段、索引带 DB time、对话里的方块字符图(纯函数,不连库)。

三件都是为了让结论能被看见:
  · 用户说了时间段,给的链接就该**已经筛在那一段上** —— 否则他还得去顶栏再选一次,
    而他刚刚已经说过了;
  · 大盘画「是不是越来越卡」只读 index.json(不会把几十份报告全下载),所以 DB time 必须进索引;
  · 纯数字看不出倾斜 —— 「86%、10%、3%」要读三个数再在脑子里比,一条占八成的横条一眼就看出来。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import urllib.parse

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import reports  # noqa: E402


def _load_render():
    """wdr 的 render 与别的 skill 同名,按文件加载成独立模块名。"""
    path = _ROOT / "skills" / "gaussdb-wdr" / "scripts" / "render.py"
    spec = importlib.util.spec_from_file_location("wdr_render_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load_render()


# ---------------------------------------------------------------- 链接带时间段

def _q(link):
    return urllib.parse.parse_qs(urllib.parse.urlparse(link).query)


def test_dash_link_carries_the_span_when_given():
    link = reports.dash_link("wdr", pathlib.Path("/x/a.json"), "api/10-0-0-9-postgres",
                             span=("2026-10-17T10:45", "2026-10-17T11:09"))
    q = _q(link)
    assert q["from"] == ["2026-10-17T10:45"] and q["to"] == ["2026-10-17T11:09"]
    assert q["run"] == ["a.json"]


def test_dash_link_without_a_span_is_unchanged():
    """不给时间段时链接形状必须和以前一字不差 —— 大盘与手册里都引用着这个形状。"""
    link = reports.dash_link("wdr", pathlib.Path("/x/a.json"), "og5")
    assert link == "/dash/wdr?inst=og5&run=a.json"


@pytest.mark.parametrize("span", [("", ""), ("2026-10-17T10:45", ""), ("", "x"), None])
def test_dash_link_ignores_an_incomplete_span(span):
    """只有一头的时间段筛不出东西,不如不带 —— 带半个参数会让大盘筛出空列表,
    用户以为「那段时间没跑过报告」。"""
    link = reports.dash_link("wdr", pathlib.Path("/x/a.json"), "og5", span=span)
    assert "from=" not in link and "to=" not in link


def test_topsql_link_keeps_the_by_dimension_and_adds_the_span():
    link = reports.dash_link("topsql", pathlib.Path("/x/a.json"), "og5", by="time",
                             span=("2026-10-17T10:45", "2026-10-17T11:09"))
    q = _q(link)
    assert q["by"] == ["time"] and "from" in q


# ---------------------------------------------------------------- 索引带 DB time

def _payload(headers, row):
    return {"dims": [{"dimension": "Load Profile", "headers": headers, "rows": [row]}]}


def test_db_time_goes_into_the_index():
    out = reports._dbtime_summary(_payload(
        ["DB time(s)", "CPU time(s)", "CPU占DBtime%"], ["23517.79", "3506.38", "14.91"]))
    assert out == {"db_time_s": 23517.79}


def test_db_time_is_found_by_column_name_not_position():
    """列顺序变过一次。按下标取会静默地把另一列的数当成 DB time。"""
    out = reports._dbtime_summary(_payload(
        ["commits", "DB time(s)", "CPU time(s)"], ["100", "42.5", "1.0"]))
    assert out == {"db_time_s": 42.5}


@pytest.mark.parametrize("payload", [
    {},
    {"dims": []},
    {"dims": [{"dimension": "Top SQL", "headers": ["x"], "rows": [["1"]]}]},
    {"dims": [{"dimension": "Load Profile", "headers": [], "rows": []}]},
    {"dims": [{"dimension": "Load Profile", "headers": ["DB time(s)"], "rows": [["不是数字"]]}]},
])
def test_missing_or_unusable_db_time_writes_nothing(payload):
    """取不到就不写这一项 —— 写个 0 会让趋势图上多一个「那次很闲」的假点。"""
    assert reports._dbtime_summary(payload) == {}


def test_index_entry_includes_db_time(tmp_path, monkeypatch):
    monkeypatch.setenv(reports.ENV_DIR, str(tmp_path))
    p = reports.archive("wdr", _payload(["DB time(s)"], ["123.5"]), instance="og5")
    assert p is not None
    import json
    idx = json.loads((tmp_path / "wdr" / "og5" / "index.json").read_text(encoding="utf-8"))
    assert idx[-1]["db_time_s"] == 123.5


# ---------------------------------------------------------------- 方块字符图

def test_bar_length_tracks_the_percentage():
    assert R.bar(100).count(R.BAR_FULL) == R.BAR_WIDTH
    assert R.bar(0).count(R.BAR_FULL) == 0
    assert R.bar(50).count(R.BAR_FULL) == R.BAR_WIDTH // 2


def test_bar_width_is_constant_so_rows_line_up():
    """宽度不一致的条形没法横向比较,那就失去了画它的意义。"""
    for pct in (0, 3.5, 10.5, 86.01, 100):
        assert len(R.bar(pct)) == R.BAR_WIDTH


@pytest.mark.parametrize("bad", [-5, 150, "abc", None, float("nan")])
def test_bar_never_raises_on_bad_input(bad):
    """报告里一个格式问题不该让整份报告失败。"""
    assert len(R.bar(bad)) == R.BAR_WIDTH


def test_share_lines_right_aligns_the_labels():
    out = R.share_lines([("LOCK_EVENT", 86.0), ("IO_EVENT", 10.5)])
    lines = out.rstrip("\n").split("\n")
    assert len(lines) == 2
    # 两行的条形必须从同一列开始(条形的第一个字符可能是 █ 也可能是 ░)
    def bar_start(line):
        return min(i for i, ch in enumerate(line) if ch in (R.BAR_FULL, R.BAR_EMPTY))
    assert bar_start(lines[0]) == bar_start(lines[1])
    assert "86.0%" in lines[0] and "10.5%" in lines[1]


def test_share_lines_is_empty_when_there_is_nothing_to_draw():
    """宁可没有图,也不要画一张空图。"""
    assert R.share_lines([]) == ""
    assert R.share_lines([("", 10)]) == ""


# ---------------------------------------------------------------- 链接要能点开

def test_the_announced_link_is_a_markdown_link_not_a_bare_path(capsys, tmp_path, monkeypatch):
    """**实测过的问题:** 用户要「这个时间段的 WDR 报告」,拿到的是
    `/dash/wdr?inst=…&run=…` —— 一个**没有站点的路径**。点不开,复制到地址栏也打不开。

    所以这一行给的必须是 `[文字](地址)`:对话页按同源解析它,点一下就到那一份报告。
    """
    monkeypatch.setenv(reports.ENV_DIR, str(tmp_path))
    p = reports.archive("wdr", {"dims": []}, instance="og5")
    reports.announce("wdr", p, "og5", span=("2026-10-17T10:45", "2026-10-17T11:09"))
    err = capsys.readouterr().err
    assert "[打开报告](/dash/wdr?" in err
    assert "from=2026-10-17T10%3A45" in err


def test_a_deployment_can_make_the_link_absolute(monkeypatch):
    """部署方注入对外地址时给出完整地址 —— 只有部署方知道用户从哪个地址访问
    (端口转发 / Ingress / 反代各不相同),skill 自己猜不出来。"""
    monkeypatch.setenv(reports.ENV_DASH_BASE, "https://dba.example.com/")
    assert reports.dash_url("/dash/wdr?inst=og5") == "https://dba.example.com/dash/wdr?inst=og5"


def test_without_that_setting_the_link_stays_relative(monkeypatch):
    """猜一个地址比给相对地址更糟:猜错的话链接指向一个不存在的站点,
    而相对地址在对话页同源下一定是对的。"""
    monkeypatch.delenv(reports.ENV_DASH_BASE, raising=False)
    assert reports.dash_url("/dash/wdr?inst=og5") == "/dash/wdr?inst=og5"


def test_no_link_means_no_announcement(capsys):
    reports.announce("wdr", None, "og5")
    assert capsys.readouterr().err == ""
