"""短问题与「后配 embedding」(2026-10-24 测试环境语义检索验收时发现)。

一、查询分词把含「等」「对」「从」…的二元组整个丢掉:「锁等待」一个词都不剩,「对象」「主从」「对账」也没了。
二、用户自己问的很短的问题(「死锁」「连接数」)没有强 token,不开向量时(客户现场的默认)一条都查不到。
三、已有知识库新配上 embedding 后跑 index,文档没变就不重新切块,覆盖率停在 0 —— 要记得加 --fill-missing。
这里用仓库里的样例知识库与真的 SQLite 索引核对,不用假存储。
"""
import importlib.util
import pathlib
import shutil
import sys
from types import SimpleNamespace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-kb-import" / "scripts"       # 本仓库:导入与建索引在 kb-import
_SAMPLE = _ROOT / "skills" / "gaussdb-kb-import" / "testdata" / "sample-kb"
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_SCRIPTS))

from common.kb import indexer, query, store_sqlite, text as kbtext  # noqa: E402

DEADLOCK = "case:S2-20250812-CBMS-死锁频发"
CONN = "case:S2-20250725-CBST-连接数逼近上限"


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sample_kb(tmp_path_factory):
    """样例知识库建好索引(不开向量 = 客户现场的默认)。"""
    kb = _load("kb")
    d = tmp_path_factory.mktemp("skb") / "kb"
    shutil.copytree(_SAMPLE, d)
    (d / "kb.yaml").write_text("embeddings: {source: none}\n", encoding="utf-8")
    ns = SimpleNamespace(kb=str(d), rebuild=False, fill_missing=False, q=None, from_findings=None, json=False)
    assert kb.cmd_index_all(ns) in (0, 2)
    assert (d / "index" / "store.sqlite").is_file()
    return d


def _case_ids(result) -> list:
    return [c.id for c in result.items[0].cases]


# ---------------------------------------------------------------- 一、分词

@pytest.mark.parametrize("q,must", [
    ("锁等待", {"等待"}),
    ("对象与索引", {"对象", "索引"}),
    ("主从切换", {"主从", "切换"}),
    ("从库延迟", {"从库", "延迟"}),
    ("清算对账", {"清算", "对账"}),
])
def test_domain_words_with_stop_chars_survive(q, must):
    assert must <= set(kbtext.query_tokens(q)), kbtext.query_tokens(q)


def test_function_words_and_question_phrasing_are_still_dropped():
    assert kbtext.query_tokens("有没有死锁的案例") == ["死锁"]
    toks = kbtext.query_tokens("对于这个问题")
    assert "对于" not in toks and "这个" not in toks
    assert "锁等" not in kbtext.query_tokens("锁等待"), "跨词的二元组照旧丢"


# ---------------------------------------------------------------- 二、短问题

@pytest.mark.parametrize("q,want", [
    ("死锁", DEADLOCK),
    ("有没有死锁的案例", DEADLOCK),
    ("连接数", CONN),
])
def test_short_questions_find_the_case_without_vectors(sample_kb, q, want):
    res = query.from_text(q, kb_dir=sample_kb)
    assert res.status.attached and "未启用" in res.status.vector
    assert want in _case_ids(res), (q, _case_ids(res))


def test_unrelated_short_question_still_finds_nothing(sample_kb):
    assert _case_ids(query.from_text("火箭发射", kb_dir=sample_kb)) == []


def test_findings_keep_the_strict_gate(sample_kb):
    """诊断发现生成的查询不放宽:一句只有「死锁」的证据,照旧过不了强 token 门槛(泛词误召回的防线)。"""
    f = SimpleNamespace(code="X", dimension="d", metric="死锁", value="", evidence="死锁", severity="", skill="")
    assert _case_ids(query.from_findings([f], kb_dir=sample_kb)) == []


def test_long_questions_behave_as_before(sample_kb):
    res = query.from_text("空闲事务持锁阻塞了很多会话怎么办", kb_dir=sample_kb)
    assert "case:S2-20250120-CBST-空闲事务持锁阻塞" in _case_ids(res)


# ---------------------------------------------------------------- 三、后配 embedding

class _Embedder:
    model = "fake-4d"

    def __init__(self, ok=True):
        from common.kb.embed import EmbedStats
        self.ok, self.stats, self.calls = ok, EmbedStats(), 0

    def embed(self, texts, timeout_s=None):
        from common.kb.embed import EmbedStats
        self.calls += 1
        if not self.ok:
            self.stats = EmbedStats(requested=len(texts), failed=len(texts), last_error="down")
            return [None] * len(texts)
        self.stats = EmbedStats(requested=len(texts), embedded=len(texts))
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text, timeout_s=None):
        return self.embed([text])[0]


def _store(tmp_path):
    pytest.importorskip("sqlite_vec", reason="没装 sqlite-vec")
    return store_sqlite.SqliteStore.open_writer(tmp_path / "store.sqlite", dims=4)


def test_configuring_embeddings_later_fills_vectors_without_the_flag(tmp_path):
    kb = tmp_path / "kb"
    shutil.copytree(_SAMPLE, kb)
    pg = _store(tmp_path)
    try:
        first = indexer.run_index(kb, pg, None, None, kb_version="t", rebuild=True)
        assert first.chunk_total > 0 and first.chunk_embedded == 0
        again = indexer.run_index(kb, pg, None, _Embedder(), kb_version="t")   # 文档没变,没加 --fill-missing
        assert again.docs_unchanged > 0 and again.docs_indexed == 0
        assert again.chunk_embedded == again.chunk_total, "配好 embedding 再跑一次 index 就要补齐"
        assert again.coverage_ok
    finally:
        pg.close()


def test_a_dead_endpoint_is_probed_once_not_retried_batch_by_batch(tmp_path):
    kb = tmp_path / "kb"
    shutil.copytree(_SAMPLE, kb)
    pg = _store(tmp_path)
    try:
        indexer.run_index(kb, pg, None, None, kb_version="t", rebuild=True)
        dead = _Embedder(ok=False)
        rep = indexer.run_index(kb, pg, None, dead, kb_version="t")
        assert dead.calls <= 2, "端点不通时只试一条(节点向量另算一次),不逐批硬补"
        assert rep.chunk_embedded == 0 and any("没有向量" in w for w in rep.warnings)
        forced = _Embedder(ok=False)
        indexer.run_index(kb, pg, None, forced, kb_version="t", fill_missing=True)
        assert forced.calls >= 1, "--fill-missing 时照样逐批重试"
    finally:
        pg.close()
