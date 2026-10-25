"""四个技能的存档钩子:跑一次 main(取数被替换成假的),GSDB_REPORTS_DIR 下就该有按实例分目录的 latest.json。

不测取数,不测渲染 —— 那些各有各的测试;这里只钉「输出之后确实存档了」「落在实例目录里」和「格式对得上」。
测试里 resolved_name 一律替换成 "og",所以实例键就是 og。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from common import reports  # noqa: E402,F401
from common.finding import Finding, Severity  # noqa: E402


def _load(scripts: str, *purge: str):
    """按 test_health_units.py 的做法:清掉同名模块缓存再把 scripts 目录压到 sys.path 头。"""
    for m in purge:
        sys.modules.pop(m, None)
    sys.path.insert(0, str(_ROOT / "skills" / scripts / "scripts"))


@pytest.fixture()
def rdir(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    return tmp_path


_HEALTH_MODS = ("model", "thresholds", "util", "collectors", "report", "health", "aggregate", "render")
_WDR_MODS = ("model", "thresholds", "util", "collectors", "report", "native", "snaps",
             "interp", "recheck", "finalreport", "ansi", "wdr", "render")


def test_health_main_archives_json_shape(rdir, monkeypatch):
    _load("gaussdb-health", *_HEALTH_MODS)
    import health, model  # noqa: E402
    ev = model.HealthEvidence(conn="og", target="10.0.0.9/postgres", overall=Severity.WARN,
                              findings=[Finding("slowsql", "SLOW_AVG_MS", Severity.WARN, "avg_ms", "8412", ">1000", "x")])
    monkeypatch.setattr(health.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(health.aggregate, "collect_all", lambda *a, **k: [])
    monkeypatch.setattr(health, "run_health", lambda *a, **k: ev)
    monkeypatch.setattr(health.common.config, "resolved_name", lambda c: "og")
    assert health.main(["-c", "og", "--format", "json"]) == 0
    latest = json.loads((rdir / "health" / "og" / "latest.json").read_text(encoding="utf-8"))
    assert latest["overall"] == 2 and latest["findings"][0]["code"] == "SLOW_AVG_MS"
    assert "sub_skills" in latest and "dims" in latest
    idx = json.loads((rdir / "health" / "og" / "index.json").read_text(encoding="utf-8"))
    assert idx[-1]["overall"] == 2 and idx[-1]["counts"] == {"2": 1}
    targets = json.loads((rdir / "health" / "targets.json").read_text(encoding="utf-8"))
    assert targets[0]["key"] == "og" and targets[0]["conn"] == "og"


def test_health_markdown_output_still_archives_json(rdir, monkeypatch):
    _load("gaussdb-health", *_HEALTH_MODS)
    import health, model  # noqa: E402
    monkeypatch.setattr(health.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(health.aggregate, "collect_all", lambda *a, **k: [])
    monkeypatch.setattr(health, "run_health", lambda *a, **k: model.HealthEvidence(conn="og"))
    monkeypatch.setattr(health.common.config, "resolved_name", lambda c: "og")
    assert health.main(["-c", "og"]) == 0
    assert (rdir / "health" / "og" / "latest.json").is_file(), "markdown 输出时也要存 JSON,大盘只认 JSON"


@pytest.mark.parametrize("scope", [["--include", "logs"], ["--exclude", "bloat"]])
def test_health_partial_run_is_not_archived_as_a_patrol(rdir, monkeypatch, scope):
    """只查部分维度的运行不是一次巡检,不能成为大盘上的「最近一次」。

    2026-09-23 user 反馈「健康检查为什么变成了只有一个维度」:深挖会话里模型用 --include 只查了
    检查点一个维度,这份局部结果被存成最新,整块大盘只剩一张卡,历史条里也多了一次「巡检」。
    """
    _load("gaussdb-health", *_HEALTH_MODS)
    import health, model  # noqa: E402
    monkeypatch.setattr(health.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(health.aggregate, "collect_all", lambda *a, **k: [])
    monkeypatch.setattr(health, "run_health", lambda *a, **k: model.HealthEvidence(conn="og"))
    monkeypatch.setattr(health.common.config, "resolved_name", lambda c: "og")
    assert health.main(["-c", "og"] + scope) == 0
    assert not (rdir / "health").exists(), "局部运行照常输出,但不存档"


def test_topsql_main_archives_with_by_in_name(rdir, monkeypatch):
    _load("gaussdb-topsql", "topsql", "render")
    import topsql  # noqa: E402
    rows = [topsql.StmtRow(sql_id="1a9f", query="select 1", calls=3, total_sec=1.5, avg_ms=500.0, rows=3)]
    monkeypatch.setattr(topsql.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(topsql, "top_sql", lambda runner, by, limit: rows)
    monkeypatch.setattr(topsql.common.config, "resolved_name", lambda c: "og")
    assert topsql.main(["-c", "og", "--by", "avg", "--format", "json"]) == 0
    files = sorted(p.name for p in (rdir / "topsql" / "og").glob("*.avg.json"))
    assert len(files) == 1, "文件名要带 by:%s" % list((rdir / "topsql").rglob("*"))
    latest = json.loads((rdir / "topsql" / "og" / "latest.json").read_text(encoding="utf-8"))
    assert latest["by"] == "avg" and latest["rows"][0]["sql_id"] == "1a9f" and latest["conn"] == "og"


def test_topsql_payload_carries_conn(rdir, monkeypatch):
    """Top SQL 报告原来没带 conn —— 没有它就没法归到实例目录,也没法在大盘上显示是哪个库。"""
    _load("gaussdb-topsql", "topsql", "render")
    import topsql  # noqa: E402
    monkeypatch.setattr(topsql.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(topsql, "top_sql", lambda runner, by, limit: [])
    monkeypatch.setattr(topsql.common.config, "resolved_name", lambda c: "api/10-0-0-9-postgres")
    assert topsql.main(["-c", "x", "--format", "json"]) == 0
    latest = json.loads((rdir / "topsql" / "api_10-0-0-9-postgres" / "latest.json").read_text(encoding="utf-8"))
    assert latest["conn"] == "api/10-0-0-9-postgres"


def test_wdr_collect_archives_evidence(rdir, monkeypatch):
    _load("gaussdb-wdr", *_WDR_MODS)
    import wdr, model  # noqa: E402
    ev = model.Evidence(conn="og", target="t")
    ev.window.begin_id, ev.window.end_id = 1915, 1916
    monkeypatch.setattr(wdr.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(wdr, "collect_evidence", lambda runner, opt: ev)
    monkeypatch.setattr(wdr.common.config, "resolved_name", lambda c: "og")
    assert wdr.main(["collect", "-c", "og", "--begin", "1915", "--end", "1916", "--format", "json"]) == 0
    latest = json.loads((rdir / "wdr" / "og" / "latest.json").read_text(encoding="utf-8"))
    assert latest["window"]["begin_id"] == 1915


def test_wdr_native_html_defaults_into_instance_dir(rdir, monkeypatch):
    """原生 WDR 报告也要落在实例目录里:大盘「下载 HTML →」链接的是 /reports/wdr/<key>/<文件名>。"""
    _load("gaussdb-wdr", *_WDR_MODS)
    import wdr, model  # noqa: E402
    seen = {}

    def fake_collect(runner, opt):
        seen["save_html"] = opt.save_html
        return model.Evidence(conn="og")

    monkeypatch.setattr(wdr.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(wdr, "collect_evidence", fake_collect)
    monkeypatch.setattr(wdr.common.config, "resolved_name", lambda c: "og")
    wdr.main(["collect", "-c", "og", "--begin", "1", "--end", "2", "--format", "json"])
    assert seen["save_html"].startswith(str(rdir / "wdr" / "og")) and seen["save_html"].endswith(".native.html")
    assert (rdir / "wdr" / "og").is_dir(), "目录要先建好,否则 native.py 落盘会失败并把失败写进 note"
    wdr.main(["collect", "-c", "og", "--begin", "1", "--end", "2", "--save-html", "/tmp/x.html", "--format", "json"])
    assert seen["save_html"] == "/tmp/x.html", "用户显式给的路径优先"


def test_wdr_explicit_save_html_still_leaves_a_copy_for_the_dashboard(rdir, monkeypatch, tmp_path):
    """显式 --save-html 时,大盘的「下载 HTML →」照样要能下到。

    SKILL.md 写着「--save-html <path> 留底原生 WDR」,模型于是自己挑 /tmp/...;存档 JSON 里
    saved_path 就是 /tmp 的路径,大盘按文件名去报告目录找 → 404。2026-09-23 集群全链路测试抓到。
    修法:用户指定的路径照写,报告目录里再留一份,存档 JSON 指向那一份;屏幕输出不变。
    """
    _load("gaussdb-wdr", *_WDR_MODS)
    import wdr, model  # noqa: E402
    user_path = tmp_path / "mine.html"

    def fake_collect(runner, opt):
        pathlib.Path(opt.save_html).write_text("<html>" + "x" * 2000, encoding="utf-8")
        return model.Evidence(conn="og", native=model.NativeInfo(generated=True, bytes=2006, saved_path=opt.save_html))

    monkeypatch.setattr(wdr.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(wdr, "collect_evidence", fake_collect)
    monkeypatch.setattr(wdr.common.config, "resolved_name", lambda c: "og")
    assert wdr.main(["collect", "-c", "og", "--begin", "1", "--end", "2", "--save-html", str(user_path),
                     "--format", "json"]) == 0
    assert user_path.is_file(), "用户要的那份照写"
    latest = json.loads((rdir / "wdr" / "og" / "latest.json").read_text(encoding="utf-8"))
    archived = pathlib.Path(latest["native"]["saved_path"])
    assert archived.parent == rdir / "wdr" / "og", "存档里指向报告目录的副本:%s" % archived
    assert archived.read_text(encoding="utf-8") == user_path.read_text(encoding="utf-8")


def test_wdr_native_path_empty_without_reports_dir(monkeypatch):
    monkeypatch.delenv("GSDB_REPORTS_DIR", raising=False)
    _load("gaussdb-wdr", *_WDR_MODS)
    import wdr  # noqa: E402
    assert wdr.default_native_path("og") == ""


def test_sqltune_policy_skip_is_archived_so_the_dashboard_stops_offering_tune(rdir, monkeypatch):
    """系统 SQL 按策略跳过时也要留一份存档。

    Top SQL 榜上常是监控类 SQL,每行都挂着「调优 →」;点下去调优技能按策略跳过、不存档,
    大盘上那一行就永远在邀请用户去点一个注定被拒的按钮。2026-09-23 集群全链路测试发现。
    """
    _load("gaussdb-sqltune", "sqltune")
    import sqltune  # noqa: E402
    sqltune.archive_tune(sqltune.skip_payload("825712617", "og", ["pg_database"]))
    d = json.loads((rdir / "sqltune" / "og" / "825712617.json").read_text(encoding="utf-8"))
    assert d["skipped"] == "system" and d["system_objects"] == ["pg_database"] and d["sql_id"] == "825712617"


def test_sqltune_archive_named_by_sql_id_under_instance_and_skipped_without_id(rdir, monkeypatch):
    _load("gaussdb-sqltune", "sqltune")
    import sqltune  # noqa: E402
    sqltune.archive_tune({"sql_id": "1a9f", "conn": "og", "x": 1})    # 有 sql_id → 存到实例目录
    assert (rdir / "sqltune" / "og" / "1a9f.json").is_file()
    sqltune.archive_tune({"sql_id": "", "conn": "og", "x": 2})        # 没有 → 不存,不报错
    assert len(list((rdir / "sqltune" / "og").glob("*.json"))) == 3   # 1a9f.json + latest.json + index.json
    sqltune.archive_tune({"sql_id": "b3d1", "x": 3})                  # 没有 conn → _default
    assert (rdir / "sqltune" / "_default" / "b3d1.json").is_file()


# --- 存档之后在标准错误里给大盘直达链接(v1.1.6 待修第 7 项) -------------------------
# 标准输出一个字不能多:JSON 模式要纯 JSON(wdr 的 collect 输出还要被 render 读回去)。

def test_health_announces_the_dashboard_link_on_stderr_and_keeps_stdout_pure_json(rdir, monkeypatch, capsys):
    _load("gaussdb-health", *_HEALTH_MODS)
    import health, model  # noqa: E402
    monkeypatch.setattr(health.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(health.aggregate, "collect_all", lambda *a, **k: [])
    monkeypatch.setattr(health, "run_health", lambda *a, **k: model.HealthEvidence(conn="og"))
    monkeypatch.setattr(health.common.config, "resolved_name", lambda c: "og")
    assert health.main(["-c", "og", "--format", "json"]) == 0
    out, err = capsys.readouterr()
    json.loads(out)                                   # 多一行就不是合法 JSON 了
    run = json.loads((rdir / "health" / "og" / "index.json").read_text(encoding="utf-8"))[-1]["file"]
    assert "已存入大盘:[打开报告](/dash/health?inst=og&run=%s)" % run in err


def test_partial_health_run_gives_no_link(rdir, monkeypatch, capsys):
    _load("gaussdb-health", *_HEALTH_MODS)
    import health, model  # noqa: E402
    monkeypatch.setattr(health.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(health.aggregate, "collect_all", lambda *a, **k: [])
    monkeypatch.setattr(health, "run_health", lambda *a, **k: model.HealthEvidence(conn="og"))
    monkeypatch.setattr(health.common.config, "resolved_name", lambda c: "og")
    health.main(["-c", "og", "--include", "logs"])
    assert "已存入大盘" not in capsys.readouterr().err, "没存档就不能给链接"


def test_topsql_link_opens_the_same_sort_tab(rdir, monkeypatch, capsys):
    _load("gaussdb-topsql", "topsql", "render")
    import topsql  # noqa: E402
    monkeypatch.setattr(topsql.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(topsql, "top_sql", lambda runner, by, limit: [])
    monkeypatch.setattr(topsql.common.config, "resolved_name", lambda c: "api/10-0-0-9-postgres")
    assert topsql.main(["-c", "x", "--by", "avg"]) == 0
    assert ("已存入大盘:[打开报告](/dash/topsql?inst=api_10-0-0-9-postgres&by=avg)"
            in capsys.readouterr().err)


def test_wdr_collect_link_and_window_in_the_index(rdir, monkeypatch, capsys):
    _load("gaussdb-wdr", *_WDR_MODS)
    import wdr, model  # noqa: E402
    ev = model.Evidence(conn="og", target="t")
    ev.window.begin_id, ev.window.end_id = 2936, 2937
    ev.window.begin_ts, ev.window.end_ts = "2026-10-16 15:16:42", "2026-10-16 15:26:46"
    monkeypatch.setattr(wdr.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(wdr, "collect_evidence", lambda runner, opt: ev)
    monkeypatch.setattr(wdr.common.config, "resolved_name", lambda c: "og")
    assert wdr.main(["collect", "-c", "og", "--begin", "2936", "--end", "2937", "--format", "json"]) == 0
    out, err = capsys.readouterr()
    json.loads(out)
    assert "已存入大盘:[打开报告](/dash/wdr?inst=og&run=" in err
    idx = json.loads((rdir / "wdr" / "og" / "index.json").read_text(encoding="utf-8"))
    assert idx[-1]["window"]["begin_id"] == 2936 and idx[-1]["window"]["end_ts"] == "2026-10-16 15:26:46"
