"""common.kb.store_sqlite —— 知识库的 SQLite 索引(2026-10-23 起取代高斯 DataVec / PG 向量库)。

断言与原来 test_kb_store_pg_live.py 打真库的那一套一一对应(那套是两种引擎「行为一致」的证据,
这里是「换成 SQLite 行为不变」的证据);不需要任何服务,单测里就能跑。
向量相关的用例要 sqlite-vec(没装时这几条跳过并写明原因)。
另加 SQLite 版特有的:临时副本 + 原子替换、只读打开、旧格式重建、与文件模式同一刻度的词法分。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import store as kbstore, store_files, store_sqlite, text as kbtext  # noqa: E402

_DIMS = 4      # 测试用小维度,断言余弦排序时人脑能算


def _has_vec() -> bool:
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    return True


needs_vec = pytest.mark.skipif(not _has_vec(), reason="没装 sqlite-vec(pip install sqlite-vec==0.1.9),向量用例跳过")


@pytest.fixture
def store(tmp_path):
    s = store_sqlite.SqliteStore.open_writer(tmp_path / "index" / "store.sqlite", dims=_DIMS)
    s.setup()
    yield s
    s.close()


def _doc(i: str, kind="case", title="t"):
    return kbstore.DocRow(id=i, kind=kind, title=title, source="src.v1", version="v1",
                          meta={"system": "CBST"}, content_hash="h" + i)


def _chunk(cid, doc_id, seq, content, signals=(), emb=None):
    return kbstore.ChunkRow(id=cid, doc_id=doc_id, seq=seq, section="现场", content=content,
                            content_hash="c" + cid, tokens=kbtext.tokenize(content),
                            signal_tokens=kbtext.tokenize(" ".join(signals)), embedding=emb)


# ---------------------------------------------------------------- setup

def test_setup_reports_engine_and_vector_capability(store):
    caps = store.capabilities()
    assert caps.engine == "sqlite" and caps.hnsw is False and caps.dims == _DIMS
    assert store.meta_get("schema_version") == store_sqlite.SCHEMA_VERSION
    assert store.meta_get("vector_engine") == ("sqlite-vec" if caps.vector else "none")
    assert caps.vector is _has_vec()


def test_setup_is_idempotent(store):
    store.setup()
    store.setup()
    assert store.counts()["chunks"] == 0


# ---------------------------------------------------------------- lexical

def test_lexical_search_hits_chinese_bigrams_and_identifiers(store):
    store.upsert_doc(_doc("d1"), [
        _chunk("c1", "d1", 0, "业务偶现单条update走索引执行耗时3s"),
        _chunk("c2", "d1", 1, "针对 cbst.cosp_asyn_task_dtl 小表调大 autovacuum_vacuum_threshold"),
    ])
    hits = store.search_chunks_lexical(kbtext.query_tokens("单条update偶现"))
    assert hits and hits[0].id == "c1"
    hits = store.search_chunks_lexical(kbtext.query_tokens("autovacuum_vacuum_threshold"))
    assert hits and hits[0].id == "c2"
    hits = store.search_chunks_lexical(kbtext.query_tokens("cosp_asyn_task_dtl"))
    assert hits and hits[0].id == "c2", "对象名去掉 schema 前缀也要命中"
    assert hits[0].meta == {"system": "CBST"} and hits[0].kind == "case" and hits[0].section == "现场"


def test_lexical_signal_weight_ranks_higher(store):
    """同样命中「autovacuum」,带信号权重的块排前面。"""
    store.upsert_doc(_doc("d1"), [
        _chunk("plain", "d1", 0, "autovacuum 参数说明"),
        _chunk("signal", "d1", 1, "无关正文", signals=("autovacuum 频繁触发",)),
    ])
    hits = store.search_chunks_lexical(["autovacuum"])
    assert [h.id for h in hits][0] == "signal"


def test_lexical_no_match_returns_empty_not_error(store):
    store.upsert_doc(_doc("d1"), [_chunk("c1", "d1", 0, "索引膨胀")])
    assert store.search_chunks_lexical(kbtext.query_tokens("完全无关的词")) == []
    assert store.search_chunks_lexical([]) == []


def test_kind_filter(store):
    store.upsert_doc(_doc("r1", kind="rule"), [_chunk("rc", "r1", 0, "索引列数不超过 4 列")])
    store.upsert_doc(_doc("k1", kind="case"), [_chunk("kc", "k1", 0, "索引列数过多导致写放大")])
    only_rules = store.search_chunks_lexical(["索引"], kinds=["rule"])
    assert [h.id for h in only_rules] == ["rc"]


def test_lexical_scores_match_file_mode_exactly(store):
    """query.py 的门槛(lexical_min / relative_floor)只认一套刻度:同样的块,两种存储的分必须一样。

    2026-10-23 换 SQLite 时没用 FTS5,就是为了这一条 —— FTS5 的 idf 在小库上会被压成 0。
    """
    chunks = [_chunk("a", "d1", 0, "单条 update 偶发秒级,autovacuum 频繁", signals=("autovacuum 频繁触发",)),
              _chunk("b", "d1", 1, "索引膨胀导致 update 变慢"),
              _chunk("c", "d1", 2, "锁等待冲高,update 被阻塞")]
    store.upsert_doc(_doc("d1"), chunks)
    q = kbtext.query_tokens("update 偶发 autovacuum")
    got = {h.id: round(h.score, 9) for h in store.search_chunks_lexical(q)}
    lex = store_files.Lexicon([(c.tokens, c.signal_tokens) for c in chunks])
    want = {chunks[k].id: round(s, 9) for k, s in lex.scores(q)}
    assert got == want and len(got) == 3


# ---------------------------------------------------------------- vector

@needs_vec
def test_vector_search_orders_by_cosine(store):
    store.upsert_doc(_doc("d1"), [
        _chunk("east", "d1", 0, "a", emb=[1, 0, 0, 0]),
        _chunk("north", "d1", 1, "b", emb=[0, 1, 0, 0]),
        _chunk("diag", "d1", 2, "c", emb=[1, 1, 0, 0]),
    ])
    hits = store.search_chunks_vector([1, 0.1, 0, 0], k=3)
    assert [h.id for h in hits] == ["east", "diag", "north"]
    assert hits[0].score > hits[1].score > hits[2].score
    assert 0.99 < hits[0].score <= 1.0001


@needs_vec
def test_vector_search_skips_null_embeddings_and_coverage_counts(store):
    store.upsert_doc(_doc("d1"), [
        _chunk("has", "d1", 0, "a", emb=[1, 0, 0, 0]),
        _chunk("none", "d1", 1, "b"),
    ])
    assert [h.id for h in store.search_chunks_vector([1, 0, 0, 0])] == ["has"]
    assert store.coverage() == (2, 1)
    assert store.missing_embeddings() == [("none", "b")]
    store.set_chunk_embedding("none", [0, 1, 0, 0])
    assert store.coverage() == (2, 2)
    assert store.missing_embeddings() == []


@needs_vec
def test_vector_kind_filter_and_wrong_dims(store):
    store.upsert_doc(_doc("r1", kind="rule"), [_chunk("rc", "r1", 0, "x", emb=[1, 0, 0, 0])])
    store.upsert_doc(_doc("k1", kind="case"), [_chunk("kc", "k1", 0, "y", emb=[1, 0, 0, 0])])
    assert [h.id for h in store.search_chunks_vector([1, 0, 0, 0], kinds=["case"])] == ["kc"]
    with pytest.raises(kbstore.StoreError, match="维度"):
        store.set_chunk_embedding("rc", [1, 0, 0])


# ---------------------------------------------------------------- nodes

def _nodes(emb=True):
    return [
        kbstore.NodeVectorRow(node_id="symptom:update_slow", kind="symptom", name="单条 update 偶发秒级",
                              tokens=kbtext.tokenize("单条 update 偶发秒级"),
                              signal_tokens=kbtext.tokenize("autovacuum 频繁触发"), signals="autovacuum 频繁触发",
                              embedding=[1, 0, 0, 0] if emb else None),
        kbstore.NodeVectorRow(node_id="symptom:lock_wait", kind="symptom", name="锁等待冲高",
                              tokens=kbtext.tokenize("锁等待冲高"), embedding=[0, 1, 0, 0] if emb else None),
    ]


def test_node_vectors_lexical_and_lifecycle(store):
    store.upsert_node_vectors(_nodes(emb=False))
    lex = store.search_nodes_lexical(kbtext.query_tokens("autovacuum 触发频繁"), kinds=["symptom"])
    assert lex and lex[0].id == "symptom:update_slow" and lex[0].content == "autovacuum 频繁触发"
    assert store.node_ids() == ["symptom:lock_wait", "symptom:update_slow"]
    store.delete_node_vectors(["symptom:lock_wait"])
    assert store.counts()["nodes"] == 1
    assert store.search_nodes_lexical(["锁等"]) == [], "删掉的节点不能还留着词条"


@needs_vec
def test_node_vectors_vector(store):
    store.upsert_node_vectors(_nodes())
    vec = store.search_nodes_vector([0, 1, 0, 0], k=1)
    assert vec[0].id == "symptom:lock_wait"


# ---------------------------------------------------------------- doc lifecycle

def test_upsert_replaces_old_chunks_and_delete_cascades(store):
    store.upsert_doc(_doc("d1"), [_chunk("old", "d1", 0, "旧内容")])
    store.upsert_doc(_doc("d1"), [_chunk("new", "d1", 0, "新内容")])
    assert store.search_chunks_lexical(["旧内"]) == []
    assert store.search_chunks_lexical(["新内"])[0].id == "new"
    assert store.doc_hashes() == {"d1": "hd1"}
    store.delete_doc("d1")
    assert store.counts()["chunks"] == 0 and "docs.case" not in store.counts()
    assert store.search_chunks_lexical(["新内"]) == [], "删文档要连词条一起删"


def test_upsert_is_atomic_on_failure(store):
    """第二块故意撞主键:整篇回滚,不能留下半截文档。"""
    store.upsert_doc(_doc("other"), [_chunk("dup", "other", 0, "x")])
    with pytest.raises(kbstore.StoreError):
        store.upsert_doc(_doc("d1"), [_chunk("fresh", "d1", 0, "a"), _chunk("dup", "d1", 1, "b")])
    assert "d1" not in store.doc_hashes()
    assert store.search_chunks_lexical(["a"]) == []


def test_rebuild_empties_everything(store):
    store.upsert_doc(_doc("d1"), [_chunk("c", "d1", 0, "x")])
    store.rebuild()
    assert store.counts() == {"chunks": 0, "nodes": 0}
    assert store.meta_get("schema_version") == store_sqlite.SCHEMA_VERSION


def test_docs_by_ids_and_sections(store):
    store.upsert_doc(_doc("d1", title="慢 SQL"), [
        kbstore.ChunkRow(id="s1", doc_id="d1", seq=0, section="处置", content="案例 › 处置\n查杀长事务",
                         content_hash="x", tokens=["查杀"])])
    assert store.docs_by_ids(["d1", "nope"])["d1"].title == "慢 SQL"
    assert store.doc_sections("d1") == {"处置": "查杀长事务"}


# ---------------------------------------------------------------- 没有向量

def test_no_vector_path(tmp_path):
    """sqlite-vec 不可用(没装 / Python 不许加载扩展):向量查询返回空,覆盖率 (n, 0),词法照常。"""
    s = store_sqlite.SqliteStore.open_writer(tmp_path / "s.sqlite", dims=_DIMS, force_no_vector=True)
    try:
        s.setup()
        assert s.capabilities().vector is False and s.vector_note
        assert s.meta_get("vector_engine") == "none"
        s.upsert_doc(_doc("d1"), [_chunk("c1", "d1", 0, "索引膨胀", emb=[1, 0, 0, 0])])
        assert s.search_chunks_vector([1, 0, 0, 0]) == []
        assert s.search_chunks_lexical(["索引"])[0].id == "c1"
        assert s.coverage() == (1, 0)
        assert s.missing_embeddings() == []
    finally:
        s.close()


# ---------------------------------------------------------------- 发布与只读(SQLite 版特有)

def _write(path, *docs, fresh=False):
    w = store_sqlite.SqliteStore.open_writer(path, dims=_DIMS, fresh=fresh)
    try:
        w.setup()
        for doc_id, text in docs:
            w.upsert_doc(_doc(doc_id), [_chunk(doc_id + "-0", doc_id, 0, text)])
        w.publish()
    finally:
        w.close()


def test_writer_works_on_a_copy_and_publish_swaps_it_in(tmp_path):
    path = tmp_path / "store.sqlite"
    _write(path, ("d1", "索引膨胀"))
    w = store_sqlite.SqliteStore.open_writer(path, dims=_DIMS)
    w.setup()
    w.upsert_doc(_doc("d2"), [_chunk("d2-0", "d2", 0, "锁等待")])
    with store_sqlite.SqliteStore.open_readonly(path, dims=_DIMS) as r:
        assert r.doc_hashes() == {"d1": "hd1"}, "没 publish 之前读方看到的还是旧的"
    w.publish()
    w.close()
    with store_sqlite.SqliteStore.open_readonly(path, dims=_DIMS) as r:
        assert set(r.doc_hashes()) == {"d1", "d2"}, "增量写:旧库复制过来再改"
    assert [p.name for p in tmp_path.iterdir()] == ["store.sqlite"], "临时副本不能留下"


def test_closing_without_publish_leaves_the_original_untouched(tmp_path):
    path = tmp_path / "store.sqlite"
    _write(path, ("d1", "索引膨胀"))
    with store_sqlite.SqliteStore.open_writer(path, dims=_DIMS) as w:
        w.setup()
        w.delete_doc("d1")                     # 写到一半出错退出
    with store_sqlite.SqliteStore.open_readonly(path, dims=_DIMS) as r:
        assert r.doc_hashes() == {"d1": "hd1"}
    assert [p.name for p in tmp_path.iterdir()] == ["store.sqlite"]


def test_fresh_writer_starts_empty(tmp_path):
    path = tmp_path / "store.sqlite"
    _write(path, ("d1", "索引膨胀"))
    _write(path, ("d2", "锁等待"), fresh=True)
    with store_sqlite.SqliteStore.open_readonly(path, dims=_DIMS) as r:
        assert set(r.doc_hashes()) == {"d2"}


def test_readonly_works_on_a_read_only_directory(tmp_path):
    """诊断容器把知识库只读挂载:不能建锁文件 / 日志文件,打开与查询都要照常。"""
    d = tmp_path / "ro"
    d.mkdir()
    _write(d / "store.sqlite", ("d1", "索引膨胀"))
    d.chmod(0o555)
    try:
        with store_sqlite.SqliteStore.open_readonly(d / "store.sqlite", dims=_DIMS) as r:
            assert r.has_index() and r.search_chunks_lexical(["索引"])[0].id == "d1-0"
            with pytest.raises(kbstore.StoreError):
                r.delete_doc("d1")
    finally:
        d.chmod(0o755)


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(kbstore.StoreError, match="还没建索引"):
        store_sqlite.SqliteStore.open_readonly(tmp_path / "nope.sqlite")


def test_old_format_is_rebuilt_not_misread(tmp_path):
    path = tmp_path / "store.sqlite"
    _write(path, ("d1", "索引膨胀"))
    w = store_sqlite.SqliteStore.open_writer(path, dims=_DIMS)
    w.meta_set("schema_version", "sqlite-0")
    w.publish()
    w.close()
    with store_sqlite.SqliteStore.open_readonly(path, dims=_DIMS) as r:
        assert not r.has_index(), "旧格式:读方当作没有索引(退到文件模式),不拿新代码读旧表"
    w = store_sqlite.SqliteStore.open_writer(path, dims=_DIMS)
    w.setup()
    assert w.counts()["chunks"] == 0 and w.meta_get("schema_version") == store_sqlite.SCHEMA_VERSION
    w.close()


def test_store_path_is_inside_the_kb_index_dir(tmp_path):
    assert store_sqlite.store_path(tmp_path) == tmp_path / "index" / "store.sqlite"
