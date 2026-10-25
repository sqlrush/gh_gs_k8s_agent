"""kb.py 的存储侧子命令在没有服务可连时的行为(没配 Neo4j;向量库是知识库目录里的 SQLite 文件,不用配;
还没 index 时走**文件模式**,index 之后走 SQLite 索引):
setup / index / health / query 都不能报「未接入」退出——文件就是知识库,只是没有向量与图库。"""
import importlib.util
import pathlib
import sys
from types import SimpleNamespace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-kb-import" / "scripts"
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_SCRIPTS))


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


kb = _load("kb")
kb_store = _load("kb_store")

# query / health 搬到了查询 skill(gaussdb-kb):文件模式下的行为断言不变,只是从那边取函数
_qspec = importlib.util.spec_from_file_location(
    "gaussdb_kb_kb", _ROOT / "skills" / "gaussdb-kb" / "scripts" / "kb.py")
kbq = importlib.util.module_from_spec(_qspec)
_qspec.loader.exec_module(kbq)

CASE_ID = "S1-20250224-CBST-偶现单条update慢"
CASE = f"""---
id: {CASE_ID}
title: 偶现单条 update 走索引耗时 3s
system: CBST
occurred_at: 2025-02-24
conclusion: 已确认
source: sources/report.v1.docx#前言
objects: [cbst.cosp_asyn_task_dtl]
signals: [autovacuum 频繁触发]
---
## 现场
业务偶现单条 update 耗时 3s,cbst.cosp_asyn_task_dtl 的 autovacuum 次数异常高。
## 判断
autovacuum 持 8 级锁。
## 处置
针对小表调大 autovacuum_vacuum_threshold。
## 复发标志
单条 update 偶发 3s 且该表 autovacuum 次数异常高。
"""
TRIPLES = f"""
- src: {{kind: case, name: x, canonical: "case:{CASE_ID}"}}
  rel: exhibits
  dst: {{kind: symptom, name: 单条 update 偶发秒级}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#现场
  case: {CASE_ID}
- src: {{kind: symptom, name: 单条 update 偶发秒级}}
  rel: caused_by
  dst: {{kind: rootcause, name: autovacuum 持 8 级锁}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#判断
  case: {CASE_ID}
- src: {{kind: rootcause, name: autovacuum 持 8 级锁}}
  rel: handled_by
  dst: {{kind: action, name: 表级调大 autovacuum_vacuum_threshold}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#处置
  case: {CASE_ID}
"""


def _kb(tmp_path, kb_yaml="embeddings: {source: none}\n") -> pathlib.Path:
    d = tmp_path / "kb"
    kb.ensure_kb_skeleton(d)
    for sub in ("cases", "graph"):
        (d / sub).mkdir(exist_ok=True)
    (d / "cases" / f"{CASE_ID}.md").write_text(CASE, encoding="utf-8")
    (d / "graph" / "cbst.yaml").write_text(TRIPLES, encoding="utf-8")
    (d / "kb.yaml").write_text(kb_yaml, encoding="utf-8")
    return d


def _ns(d, **kw):
    base = dict(kb=str(d), rebuild=False, fill_missing=False, q=None, from_findings=None, json=False)
    return SimpleNamespace(**{**base, **kw})


def test_health_in_file_mode_is_attached_and_exits_zero(tmp_path, capsys):
    rc = kbq.cmd_health(_ns(_kb(tmp_path)))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert out.startswith("> 知识库") and "模式:文件(还没建索引" in out
    assert "案例 1" in out and "[error]" not in out


def test_setup_reports_the_index_and_vector_capability_without_any_service(tmp_path, capsys):
    d = _kb(tmp_path)
    rc = kb_store.cmd_setup(_ns(d))
    out = capsys.readouterr().out
    assert rc == 0 and "index/store.sqlite" in out and "向量" in out and "Next: kb.py index" in out
    assert not (d / "index" / "store.sqlite").exists(), "setup 只探测,不建索引"
    assert not list((d / "index").glob("store.sqlite.tmp*")), "探测用的临时副本要删掉"


def test_index_all_builds_the_file_index_and_the_sqlite_index(tmp_path, capsys):
    d = _kb(tmp_path)
    rc = kb.cmd_index_all(_ns(d))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert (d / "CASES.md").is_file() and (d / "RULES.md").is_file()
    assert (d / "index" / "store.sqlite").is_file() and "文档      : 新写" in out
    assert kbq.cmd_health(_ns(d)) == 0
    assert "模式:向量库+图文件" in capsys.readouterr().out, "建了索引之后检索走 SQLite 索引,不再是文件模式"


def test_runtime_reads_the_index_from_a_read_only_mount(tmp_path, capsys):
    """诊断容器把知识库只读挂载(2026-10-23 换 SQLite 后的关键约束):打开、查询、health 都要照常。"""
    d = _kb(tmp_path)
    assert kb.cmd_index_all(_ns(d)) == 0
    capsys.readouterr()
    for p in [d] + [x for x in d.rglob("*") if x.is_dir()]:
        p.chmod(0o555)
    try:
        rc = kbq.cmd_query(_ns(d, q="autovacuum 次数异常高 cbst.cosp_asyn_task_dtl"))
        out = capsys.readouterr().out
        assert rc == 0 and "模式:向量库+图文件" in out and f"{CASE_ID}" in out
    finally:
        for p in [d] + [x for x in d.rglob("*") if x.is_dir()]:
            p.chmod(0o755)


def test_legacy_store_pg_in_kb_yaml_is_ignored_with_a_hint(tmp_path, capsys):
    """升级不能因为 kb.yaml 里一段已下线的 store.pg 就让知识库用不了:照常工作,提示可以删。"""
    d = _kb(tmp_path)
    (d / "kb.yaml").write_text("store:\n  pg: {host: h, database: kb, user: kb_rw, credential: kb-pg, "
                               "password_env: KB_PG_PASSWORD}\n", encoding="utf-8")
    assert kb.cmd_index_all(_ns(d)) == 0
    assert "store.pg 已不再使用" in capsys.readouterr().out and (d / "index" / "store.sqlite").is_file()
    assert kbq.cmd_health(_ns(d)) == 0
    assert "store.pg 已不再使用" in capsys.readouterr().out


def test_query_in_file_mode_renders_hits_and_exits_zero(tmp_path, capsys):
    rc = kbq.cmd_query(_ns(_kb(tmp_path), q="autovacuum 次数异常高 cbst.cosp_asyn_task_dtl"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "模式:文件(" in out
    assert f"- **历史相似** {CASE_ID}(结论强度:已确认" in out
    assert "- **本行历史路径** 单条 update 偶发秒级 → autovacuum 持 8 级锁 → 表级调大" in out


def test_query_without_kb_dir_is_still_unattached(tmp_path, capsys):
    rc = kbq.cmd_query(_ns(tmp_path / "nope", q="x"))
    assert rc == 2 and "知识库未接入" in capsys.readouterr().out


def test_index_that_cannot_write_the_store_keeps_the_file_index(tmp_path, capsys):
    """索引写不进去(这里让 index/store.sqlite 是个目录,换不上去):文件索引照建、检索退到文件模式,
    退出码是 2(有活没干完)而不是 1(整条命令失败)。报出原因,也报出「还能用」——两句都要有。"""
    d = _kb(tmp_path)
    (d / "index" / "store.sqlite").mkdir(parents=True)
    (d / "index" / "store.sqlite" / "x").write_text("x", encoding="utf-8")
    rc = kb.cmd_index_all(_ns(d))
    out = capsys.readouterr().out
    assert rc == 2, out
    assert (d / "CASES.md").is_file() and (d / "RULES.md").is_file()
    assert "写索引失败" in out and "store.sqlite" in out
    assert "检索照常" in out and "有旧索引时继续用旧索引" in out and "没有索引时走文件模式" in out
    assert not list((d / "index").glob("store.sqlite.tmp*")), "失败也不能留临时副本"


def test_health_with_a_broken_index_reports_the_reason(tmp_path, capsys):
    d = _kb(tmp_path)
    (d / "index").mkdir(exist_ok=True)
    (d / "index" / "store.sqlite").write_bytes(b"garbage" * 200)
    rc = kbq.cmd_health(_ns(d))
    out = capsys.readouterr().out
    assert rc == 0 and "模式:文件(打不开索引文件" in out


# --------------------------------------------------------------------------
# 现象节点断成两半 —— 真跑抓到的静默失效
#
# 模型抽边时常常把「案例 exhibits 现象」的现象按发现代码命名(symptom:cache_low),
# 把因果链的起点按描述命名(symptom:缓存命中率低…),两者没在 canonical.yaml 里归一。
# 后果:这个案例永远走不到自己的 现象→根因→处置 链。向量检索能靠语义相似绕过去,
# 纯词法(文件模式 / 没配 embedding)就直接查不到——不报错,只是「路径:无」。
# --------------------------------------------------------------------------
_SPLIT = f"""
- src: {{kind: case, name: x, canonical: "case:{CASE_ID}"}}
  rel: exhibits
  dst: {{kind: symptom, name: CACHE_LOW}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#现场
  case: {CASE_ID}
- src: {{kind: symptom, name: 缓存命中率低,报表时段 blks_read 冲高}}
  rel: caused_by
  dst: {{kind: rootcause, name: 报表 SQL 全表扫描挤缓存}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#判断
  case: {CASE_ID}
- src: {{kind: rootcause, name: 报表 SQL 全表扫描挤缓存}}
  rel: handled_by
  dst: {{kind: action, name: 报表迁只读实例}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#处置
  case: {CASE_ID}
"""


def test_validate_flags_symptoms_that_cases_can_never_reach(tmp_path, capsys):
    d = _kb(tmp_path)
    (d / "graph" / "split.yaml").write_text(_SPLIT, encoding="utf-8")
    kb.cmd_index(_ns(d))                               # validate 要有 INDEX.md / RULES.md
    capsys.readouterr()
    rc = kb.cmd_validate(_ns(d))
    out = capsys.readouterr().out
    assert rc == 0                                     # 是 warn,不是 error:链可能只是还没抽
    assert "CACHE_LOW" in out                          # 点名走不到链的那个现象
    assert "canonical.yaml" in out and "路径" in out    # 说清怎么修、后果是什么
    assert "文件模式" in out


def test_validate_is_quiet_when_the_symptom_is_merged(tmp_path, capsys):
    d = _kb(tmp_path)
    (d / "graph" / "split.yaml").write_text(_SPLIT, encoding="utf-8")
    (d / "graph" / "canonical.yaml").write_text(
        "symptom:cache_low: [CACHE_LOW, 缓存命中率低,报表时段 blks_read 冲高]\n", encoding="utf-8")
    kb.cmd_index(_ns(d))
    capsys.readouterr()
    kb.cmd_validate(_ns(d))
    out = capsys.readouterr().out
    assert "canonical.yaml" not in out or "走不到" not in out


def test_health_prints_the_upload_inbox_dir(tmp_path, capsys, monkeypatch):
    """用户要导入自己电脑上的文件时,模型得能说出「传到沙箱哪个目录」——这行就是那个目录。"""
    monkeypatch.delenv("GSDB_KB_INBOX", raising=False)
    d = _kb(tmp_path)
    kbq.cmd_health(_ns(d))
    out = capsys.readouterr().out
    assert "收件目录" in out and str(d / "inbox" / "uploads") in out
