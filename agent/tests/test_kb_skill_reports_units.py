"""gaussdb-kb 的 health --json 与 query 检索日志 —— 大盘知识库页的两个数据来源。"""
import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_SCRIPTS = _ROOT / "skills" / "gaussdb-kb" / "scripts"


def _load_kb_cli():
    """两个 skill 的入口都叫 kb.py(gaussdb-kb 与 gaussdb-kb-import)。整套一起跑时
    `import kb` 拿到的是先被谁缓存的那个 —— 单跑本文件绿、全套红,就是这么来的。
    按文件路径、用独一无二的模块名加载,与 test_kb_query_skill_units.py 同一做法。"""
    modname = "kb_query_cli_for_reports"
    if modname in sys.modules:
        return sys.modules[modname]
    if str(_SCRIPTS) not in sys.path:            # 兄弟模块 kb_cite 按脚本目录找,和真实运行一样
        sys.path.append(str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(modname, _SCRIPTS / "kb.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


kbcli = _load_kb_cli()
from common.kb import query as kbquery  # noqa: E402


class _Sess:
    def __init__(self, status):
        self._s = status
        self.pg = type("PG", (), {"warnings": ["cases/bad.md: 缺 frontmatter"]})()

    def status(self):
        return self._s

    def close(self):
        pass


def _fake_open(status):
    return lambda kb: _Sess(status)


def test_health_json_shape_when_attached(monkeypatch, tmp_path, capsys):
    kbdir = tmp_path / "kb"
    (kbdir / "index").mkdir(parents=True)
    (kbdir / "index" / "misses.log").write_text(
        "2026-09-20\tNOPK_BIGTABLE\tq\n2026-09-21\tNOPK_BIGTABLE\tq\n2026-09-21\tREPL_LAG\tq\n", encoding="utf-8")
    st = kbquery.KbStatus(attached=True, version="3", counts={"docs.case": 286, "docs.rule": 412},
                          vector="sqlite-vec(覆盖 93%)", graph="图文件", mode="向量库+图文件")
    monkeypatch.setattr(kbquery.KbSession, "open", staticmethod(_fake_open(st)))
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path / "reports"))
    rc = kbcli.main(["health", "--kb", str(kbdir), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"]["counts"]["docs.case"] == 286 and out["status"]["mode"] == "向量库+图文件"
    assert out["misses"] == [{"code": "NOPK_BIGTABLE", "n": 2}, {"code": "REPL_LAG", "n": 1}]
    assert out["file_warnings"] == ["cases/bad.md: 缺 frontmatter"]
    assert out["readonly"] is False and out["inbox"].endswith("inbox/uploads")
    assert out["pending"] == [] and out["index_state"] == {}
    archived = json.loads((tmp_path / "reports" / "kb" / "health.json").read_text(encoding="utf-8"))
    assert archived["status"]["counts"]["docs.rule"] == 412


def test_health_json_when_not_attached_keeps_reason_and_exit_2(monkeypatch, tmp_path, capsys):
    kbdir = tmp_path / "kb"
    kbdir.mkdir()
    st = kbquery.KbStatus(attached=False, reason="未接入")
    monkeypatch.setattr(kbquery.KbSession, "open", staticmethod(_fake_open(st)))
    monkeypatch.delenv("GSDB_REPORTS_DIR", raising=False)
    rc = kbcli.main(["health", "--kb", str(kbdir), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["status"]["attached"] is False and out["status"]["reason"] == "未接入"


def test_health_text_output_unchanged_and_still_archives(monkeypatch, tmp_path, capsys):
    kbdir = tmp_path / "kb"
    kbdir.mkdir()
    st = kbquery.KbStatus(attached=True, counts={"docs.case": 1, "docs.rule": 2, "docs.raw": 0}, mode="文件")
    monkeypatch.setattr(kbquery.KbSession, "open", staticmethod(_fake_open(st)))
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path / "reports"))
    kbcli.main(["health", "--kb", str(kbdir)])
    text = capsys.readouterr().out
    assert text.startswith("> 知识库") and "缺口清单  : 无记录" in text
    assert (tmp_path / "reports" / "kb" / "health.json").is_file()


# ---- query 检索日志 ----------------------------------------------------------------

def _qr(mode, vector, items):
    st = kbquery.KbStatus(attached=True, mode=mode, vector=vector)
    return kbquery.QueryResult(status=st, items=tuple(items), elapsed_ms=42)


def _ref(i):
    return kbquery.Ref(id=i, kind="case", title=i, score=1.0)


def test_query_appends_one_log_line_per_item(monkeypatch, tmp_path, capsys):
    items = [kbquery.FindingRefs(key="q", label="q", query="慢 SQL 全表扫描",
                                 cases=(_ref("case:1"), _ref("case:2")), clauses=(_ref("rule:1"),))]
    monkeypatch.setattr(kbquery, "from_text", lambda q, kb_dir, **_kw: _qr("向量库+图文件", "sqlite-vec(覆盖 93%)", items))
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path / "reports"))
    (tmp_path / "kb").mkdir()
    assert kbcli.main(["query", "--kb", str(tmp_path / "kb"), "--q", "慢 SQL 全表扫描", "--json"]) == 0
    lines = (tmp_path / "reports" / "kb" / "queries.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[-1])
    assert row["q"] == "慢 SQL 全表扫描" and row["hits_cases"] == 2 and row["hits_rules"] == 1
    assert row["how"] == "semantic" and row["elapsed_ms"] == 42 and row["at"].endswith("Z")


def test_query_how_is_keyword_in_file_mode_or_when_vector_timed_out(monkeypatch, tmp_path, capsys):
    items = [kbquery.FindingRefs(key="q", label="q", query="x")]
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path / "reports"))
    (tmp_path / "kb").mkdir()
    monkeypatch.setattr(kbquery, "from_text", lambda q, kb_dir, **_kw: _qr("文件", "未启用", items))
    kbcli.main(["query", "--kb", str(tmp_path / "kb"), "--q", "x", "--json"])
    monkeypatch.setattr(kbquery, "from_text", lambda q, kb_dir, **_kw: _qr("向量库", "sqlite-vec·本次超时未用", items))
    kbcli.main(["query", "--kb", str(tmp_path / "kb"), "--q", "x", "--json"])
    hows = [json.loads(l)["how"] for l in (tmp_path / "reports" / "kb" / "queries.jsonl").read_text().splitlines()]
    assert hows == ["keyword", "keyword"]


def test_query_without_reports_dir_logs_nothing_and_output_unchanged(monkeypatch, tmp_path, capsys):
    items = [kbquery.FindingRefs(key="q", label="q", query="x")]
    monkeypatch.setattr(kbquery, "from_text", lambda q, kb_dir, **_kw: _qr("文件", "未启用", items))
    monkeypatch.delenv("GSDB_REPORTS_DIR", raising=False)
    (tmp_path / "kb").mkdir()
    assert kbcli.main(["query", "--kb", str(tmp_path / "kb"), "--q", "x", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["elapsed_ms"] == 42
    assert not (tmp_path / "reports").exists()
