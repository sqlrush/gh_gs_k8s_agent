"""跑题一侧(acceptance/runner/)的单测:不连测试环境,环境相关的部分用假对象。"""
import datetime as dt
import io
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

_ACC = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ACC))

from runner import ask, config, fixtures, kbswap, lab, opencode, trajectory  # noqa: E402


def _msg(role, parts=(), **info):
    return {"info": dict(role=role, **info), "parts": list(parts)}


def _tool(tool, cmd, out, status="completed"):
    return {"type": "tool", "tool": tool, "state": {"input": {"command": cmd}, "output": out, "status": status,
                                                     "time": {"start": 1000, "end": 3000}}}


MSGS = [
    _msg("user", [{"type": "text", "text": "第一问"}]),
    _msg("assistant", [{"type": "text", "text": "我先登录"}, _tool("bash", "login.py --alias og5", "已登录")],
         tokens={"input": 10, "output": 5, "reasoning": 1, "cache": {"read": 7}}, time={"created": 0, "completed": 4000}),
    _msg("assistant", [{"type": "text", "text": "第一轮的回答"}], tokens={"input": 3, "output": 2}),
    _msg("user", [{"type": "text", "text": "展开说说第 2 条"}]),
    _msg("assistant", [_tool("bash", "python3 /x/skills/gaussdb-lockwait/scripts/lockwait.py --from a --to b", "链")]),
    _msg("assistant", [{"type": "text", "text": "第二轮的回答"}], error={"name": "APIError", "data": {"message": "rate limit"}}),
]


def test_trajectory_splits_turns_tools_tokens():
    t = trajectory.parse(MSGS)
    assert t.answers == ["第一轮的回答", "第二轮的回答"]
    assert t.answer == "第二轮的回答"
    assert [c.command[:8] for c in t.tools] == ["login.py", "python3 "]
    assert t.tokens == {"input": 13, "output": 7, "reasoning": 1, "cache_read": 7}
    assert t.tool_s == 4.0 and t.errors == ["rate limit"]
    assert "$ login.py --alias og5\n已登录" in t.tool_text()


def test_trajectory_keeps_written_drafts():
    msgs = [_msg("user"), _msg("assistant", [{"type": "tool", "tool": "write", "state": {
        "input": {"filePath": "/nas/kb/inbox/k3/source.md", "content": "3.4 长事务(超过 8 小时)"}, "output": "ok"}}])]
    t = trajectory.parse(msgs)
    assert "3.4 长事务(超过 8 小时)" in t.written() and "3.4 长事务" in t.tool_text()


def test_trajectory_counts_pending_question_as_part_of_answer():
    pending = [{"questions": [{"question": "要查哪个实例?", "options": [{"label": "og5"}, {"label": "og-std"}]}]}]
    t = trajectory.parse(MSGS[:3], pending)
    assert t.answer.endswith("【向用户提问】要查哪个实例?(选项:og5 / og-std)")


class FakePortal(opencode.Portal):
    def __init__(self, script):
        super().__init__("http://x", sleep=lambda s: None)
        self.script = list(script)

    def call(self, method, path, body=None, timeout=60):
        if path == "/question":
            return self.script.pop(0) if self.script and isinstance(self.script[0], list) else []
        if path == "/session/status":
            return self.script.pop(0) if self.script else {}
        return None


def test_wait_reports_idle_question_and_timeout():
    assert FakePortal([[], {"s": {"type": "busy"}}, [], {}]).wait("s", 100, clock=lambda: 0) == "idle"
    assert FakePortal([[{"sessionID": "s", "questions": []}]]).wait("s", 100) == "question"
    ticks = iter(range(0, 1000, 50))
    busy = [[], {"s": {"type": "busy"}}] * 20
    assert FakePortal(busy).wait("s", 100, clock=lambda: next(ticks)) == "timeout"


def test_portal_retries_503_then_gives_up(monkeypatch):
    import urllib.error
    import urllib.request
    calls = []

    def boom(req, timeout=0):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {"Retry-After": "1"}, io.BytesIO(b"busy"))
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    p = opencode.Portal("http://gw", attempts=3, sleep=lambda s: None)
    with pytest.raises(opencode.PortalError):
        p.call("GET", "/session/status")
    assert len(calls) == 3


def test_render_placeholders_and_missing():
    text, missing = fixtures.render("{instance} 在 {start} 到 {end},{sql_id:S3}", {"instance": "og5", "start": "s"})
    assert text == "og5 在 s 到 {end},{sql_id:S3}" and missing == ["end", "sql_id:S3"]


def test_minute_rounding_and_old_moment():
    assert fixtures.minute("2026-10-27 14:05:12.33") == "2026-10-27 14:05"
    assert fixtures.minute("2026-10-27 14:25:12", ceil=True) == "2026-10-27 14:26"
    assert fixtures.minute("2026-10-27 14:25:00", ceil=True) == "2026-10-27 14:25"
    assert fixtures.old_moment(60, dt.datetime(2026, 10, 27, 9)) == "2026-08-28 03:00"


def test_paste_block_strips_sql_comments(tmp_path):
    f = tmp_path / "s4.sql"
    f.write_text("-- 成因:索引列上套转换\nSELECT 1\n  -- 注释\nFROM t;\n", encoding="utf-8")
    block = fixtures.paste_block(f, strip=True)
    assert "成因" not in block and block.startswith("\n\n```sql\nSELECT 1\nFROM t;")


def test_build_prompt_upload_places_file(tmp_path):
    nas = tmp_path / "kb"
    c = ask.Ctx("r1", tmp_path, {"instance": "og5"}, None, None, 60, tmp_path, tmp_path, nas, "/nas/kb")
    q = {"id": "K1", "ask": "把这份报告导入知识库。", "attach": {"file": "acceptance/fixtures/C5-change.sql", "mode": "upload"}}
    text, follow, missing = ask.build_prompt(q, c)
    assert (nas / "inbox" / "uploads" / "acceptance-r1-K1" / "C5-change.sql").is_file()
    assert "/nas/kb/inbox/uploads/acceptance-r1-K1/C5-change.sql" in text and not missing


def test_clip_payload_shape(monkeypatch):
    page = '<html><title>复盘</title><img src="/a.png"><img src="http://127.0.0.1:18301/logo.png"></html>'

    class R:
        def __init__(self, b):
            self.b = b

        def read(self):
            return self.b

    class Op:
        def open(self, url, data=None, timeout=0):
            return R(b"\x89PNG" if url.endswith(".png") else page.encode() if "display" in url else b"")
    monkeypatch.setattr(fixtures.urllib.request, "build_opener", lambda *a: Op())
    p = fixtures.clip_payload("http://127.0.0.1:18300", "2026-10-27T10:00:00+08:00")
    assert p["kind"] == "page" and p["title"] == "复盘" and p["url"].endswith(fixtures.WIKI_PAGE)
    assert p["images"][0]["data"].startswith("data:image/png;base64,")
    assert p["skipped"] == [{"src": "http://127.0.0.1:18301/logo.png", "reason": "跨站图片,读不到"}]


def test_db_cleanup_reads_drop_statements(tmp_path):
    seen = []
    fixtures.db_cleanup(_ACC / "fixtures" / "C4-proc.sql", seen.append)
    assert seen == ["DROP PROCEDURE IF EXISTS fl.p_accept_adjust;\nDROP TABLE IF EXISTS fl.accept_err_log;"]


@pytest.mark.skipif(shutil.which("rsync") is None, reason="没有 rsync")
def test_kb_swap_backup_install_restore_roundtrip(tmp_path):
    nas, bk, local = tmp_path / "kb", tmp_path / "bk", tmp_path / "local"
    (nas / "docsets" / "G1" / "items").mkdir(parents=True)
    (nas / "docsets" / "G1" / "items" / "PD-G1-00001.md").write_text("doc", encoding="utf-8")
    (nas / "cases").mkdir()
    (nas / "cases" / "old.md").write_text("old case", encoding="utf-8")
    (nas / "index" / "embed-cache").mkdir(parents=True)
    (nas / "index" / "store.sqlite").write_text("db", encoding="utf-8")
    (nas / "kb.yaml").write_text("embeddings: {}", encoding="utf-8")
    before = kbswap.fingerprint(nas)
    kbswap.build_local(local, nas)
    with pytest.raises(kbswap.SwapError):
        kbswap.install(nas, local, bk)                     # 没备份不许清
    kbswap.backup(nas, bk, "r1")
    with pytest.raises(kbswap.SwapError):
        kbswap.backup(nas, bk, "r2")                       # 上一轮没还原不许再备份
    kbswap.install(nas, local, bk)
    assert not (nas / "cases" / "old.md").exists() and (nas / "rules" / "vacuum.yaml").is_file()
    assert (nas / "docsets" / "G1" / "items" / "PD-G1-00001.md").is_file() and (nas / "kb.yaml").is_file()
    assert (nas / "index" / "embed-cache").is_dir() and not (nas / "index" / "store.sqlite").exists()
    assert kbswap.fingerprint(nas) != before
    assert kbswap.restore(nas, bk).startswith("已还原")
    assert (nas / "cases" / "old.md").is_file() and not (nas / "rules").exists()
    assert kbswap.fingerprint(nas) == before and not bk.exists()


def test_kb_changes_count_only_knowledge_dirs():
    before = {"cases/a.md": "1", "sources/x.md": "1", "INDEX.md": "1"}
    after = {"cases/a.md": "2", "sources/x.md": "1", "sources/y.md": "1", "INDEX.md": "2", "topics/t.md": "1",
             "strategies/tickets.yaml": "1"}
    diff = kbswap.changed(before, after)
    assert diff == ["INDEX.md", "cases/a.md", "sources/y.md", "strategies/tickets.yaml", "topics/t.md"]
    assert kbswap.knowledge_changed(diff) == ["cases/a.md", "strategies/tickets.yaml"]


def test_local_kb_has_default_ticket_strategy(tmp_path):
    kbswap.build_local(tmp_path / "local", tmp_path / "nas")
    s = (tmp_path / "local" / "strategies" / "tickets.yaml").read_text(encoding="utf-8")
    assert len([l for l in s.splitlines() if ":" in l]) >= 8


def test_reindex_reruns_until_done():
    rcs = iter([2, 2, 0])
    runs = []

    def ex(argv):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, next(rcs), "建了一批\n", "")
    kbswap.reindex(ex, "/nas/kb", "/kb.py")
    assert len(runs) == 3 and runs[0] == ["python3", "/kb.py", "index", "--kb", "/nas/kb"]
    with pytest.raises(kbswap.SwapError):
        kbswap.reindex(lambda a: subprocess.CompletedProcess(a, 1, "", "坏了"), "/nas/kb", "/kb.py")


def test_lab_sampler_and_timeline(tmp_path):
    outs = iter(["fl:K1/root|4100|0\nfl:S3/run|4200|-77\nfl:S3/run|4200|-77\n", "fl:K1/root|4100|0\n",
                 "version|openGauss 5\nmax_connections|1000\n"])
    s = lab.Sampler(lambda sql: next(outs))
    s.sample()
    s.sample()
    s.sample_env()
    assert s.pid("fl:K1/root") == "4100" and s.sql_id("S3") == "-77" and s.sql_id("K1") == ""
    assert s.env == {"version": "openGauss 5", "max_connections": "1000"}
    rows = [{"event": "故障窗口开始", "db_time": "2026-10-27 14:05:12"}, {"event": "  K1", "detail": "等锁 5 个"},
            {"event": "场景起不来", "detail": "K9:没有权限"}, {"event": "快照号", "detail": "3185, 3186, 3190"},
            {"event": "故障窗口结束", "db_time": "2026-10-27 14:25:12"}]
    sm = lab.summarize(rows)
    assert sm["start"].endswith("14:05:12") and sm["snapshots"] == [3185, 3186, 3190]
    assert sm["probes"] == {"K1": "等锁 5 个"} and sm["failed"] == ["K9:没有权限"]


def test_config_parse_and_errors(tmp_path):
    raw = json.loads(json.dumps(__import__("yaml").safe_load((_ACC / "run.example.yaml").read_text(encoding="utf-8"))))
    cfg = config.parse(raw)
    assert cfg.names["broken"] == "og-std" and cfg.admin_portal == "http://127.0.0.1:18095"
    assert cfg.admin["sso_sim"] == config.REPO / "k8s" / "tools" / "sso-sim.py" and cfg.judge.model
    raw["names"].pop("broken")
    with pytest.raises(config.ConfigError):
        config.parse(raw)
    with pytest.raises(config.ConfigError):
        config.load(tmp_path / "nope.yaml")
