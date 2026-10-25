"""知识库的派生索引:<kb>/index/store.sqlite 一个文件 —— 切块 + 词法倒排 + 向量 + 节点向量。

2026-10-22 客户要求:向量库改为 sqlite-vec,GaussDB 向量库下线(原 store_pg.py 与 vectordb 组件)。
文件才是真相,这个库随时可以 `kb.py index --rebuild` 重建。

  · 读:`mode=ro&immutable=1` 打开 —— 诊断容器把知识库只读挂载,不能建锁文件 / WAL;
  · 写:只有导入容器一个写方(且持知识库写锁)。在**临时副本**上改,改完 os.replace 换上去,
    读方任何时刻打开的都是完整的一份(已打开的读方继续读旧 inode,不受影响);
  · 词法:自建倒排表(term → 块、词频)+ bm25.py,与文件模式同一套算式、同一个刻度。
    不用 FTS5:镜像里的 SQLite 3.40 不支持无内容表的删除,且 FTS5 的 idf 在小库上会被压成 0
    (一半以上文档含的词 idf 为负、被截成 1e-6),小知识库的命中分全被 lexical_min 挡掉;
  · 向量:sqlite-vec 的 vec_distance_cosine 精确检索(几万条以内够用,不需要近似索引);
    存 float32 小端 BLOB。sqlite-vec 加载不了(没装 / Python 不许加载扩展)时 vector=False,
    状态行如实写「向量:未启用」,词法与图照常。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import struct
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import bm25
from .store import Capabilities, ChunkRow, DocRow, Hit, NodeVectorRow, StoreError

STORE_NAME = "store.sqlite"
SCHEMA_VERSION = "sqlite-1"
ENGINE = "sqlite"
VECTOR_ENGINE = "sqlite-vec"

_TABLES = ("kb_chunk_terms", "kb_node_terms", "kb_chunks", "kb_node_vectors", "kb_docs", "kb_meta")
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS kb_docs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, source TEXT, "
    "version TEXT, meta TEXT, content_hash TEXT)",
    "CREATE TABLE IF NOT EXISTS kb_chunks (rid INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, doc_id TEXT NOT NULL, "
    "seq INTEGER NOT NULL, section TEXT, content TEXT NOT NULL, content_hash TEXT, ntok REAL NOT NULL DEFAULT 0, "
    "embedding BLOB, novec INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS kb_chunks_doc_idx ON kb_chunks (doc_id, seq)",
    "CREATE TABLE IF NOT EXISTS kb_chunk_terms (term TEXT NOT NULL, rid INTEGER NOT NULL, tf REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS kb_chunk_terms_term_idx ON kb_chunk_terms (term)",
    "CREATE INDEX IF NOT EXISTS kb_chunk_terms_rid_idx ON kb_chunk_terms (rid)",
    "CREATE TABLE IF NOT EXISTS kb_node_vectors (rid INTEGER PRIMARY KEY, node_id TEXT UNIQUE NOT NULL, "
    "kind TEXT NOT NULL, name TEXT NOT NULL, signals TEXT, ntok REAL NOT NULL DEFAULT 0, embedding BLOB)",
    "CREATE TABLE IF NOT EXISTS kb_node_terms (term TEXT NOT NULL, rid INTEGER NOT NULL, tf REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS kb_node_terms_term_idx ON kb_node_terms (term)",
    "CREATE INDEX IF NOT EXISTS kb_node_terms_rid_idx ON kb_node_terms (rid)",
    "CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT)",
)


def store_path(kb: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(kb) / "index" / STORE_NAME


def _pack(values: Sequence[float]) -> bytes:
    """sqlite-vec 认的 float32 小端 BLOB。"""
    return struct.pack("<%df" % len(values), *(float(v) for v in values))


def _load_vec(conn: sqlite3.Connection) -> Tuple[bool, str]:
    """(能不能做向量, 版本或不能的原因)。"""
    try:
        import sqlite_vec
    except ImportError:
        return False, "没装 sqlite-vec(pip 包 sqlite-vec)"
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True, str(conn.execute("SELECT vec_version()").fetchone()[0])
    except (AttributeError, sqlite3.Error) as exc:
        # AttributeError:Python 编译时没开扩展加载(如 macOS 系统自带的 python)
        return False, f"加载 sqlite-vec 失败({exc})"


def _placeholders(n: int) -> str:
    return ", ".join("?" * n)


def _meta_json(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


class SqliteStore:
    """一个 SQLite 连接上的知识库表操作。不是线程安全的;每个进程开一个。

    用 open_readonly() 读,用 open_writer() 写(写完必须 publish(),否则临时副本丢弃、原文件不动)。
    """

    def __init__(self, conn: sqlite3.Connection, path: pathlib.Path, dims: int,
                 tmp: Optional[pathlib.Path] = None, force_no_vector: bool = False):
        self._conn = conn
        self.path = path
        self.dims = int(dims)
        self._tmp = tmp
        if force_no_vector:
            self._vector, self._vec_info = False, "已关闭"
        else:
            self._vector, self._vec_info = _load_vec(conn)

    # --- 打开 / 关闭 ----------------------------------------------------------

    @classmethod
    def open_readonly(cls, path: pathlib.Path, dims: int = 1024, force_no_vector: bool = False) -> "SqliteStore":
        path = pathlib.Path(path).resolve()
        if not path.is_file():
            # 措辞里不带命令:这句会进诊断报告的状态行,而诊断环境里没有导入命令(建索引是导入环境的事)
            raise StoreError(f"还没建索引({path})")
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"打不开索引文件 {path}:{exc}(要重建索引)") from exc
        return cls(conn, path, dims, force_no_vector=force_no_vector)

    @classmethod
    def open_writer(cls, path: pathlib.Path, dims: int = 1024, fresh: bool = False,
                    force_no_vector: bool = False) -> "SqliteStore":
        """在临时副本上写。fresh=True 不复制旧库(重建)。调用方要持知识库写锁。"""
        path = pathlib.Path(path).resolve()
        tmp = path.with_name("%s.tmp-%d" % (path.name, os.getpid()))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.unlink(missing_ok=True)
            if path.is_file() and not fresh:
                shutil.copyfile(path, tmp)
            # nolock:只有本进程碰这个临时文件(知识库写锁已持有);NAS 上 SQLite 的文件锁靠不住,不让它去锁
            conn = sqlite3.connect(tmp.as_uri() + "?nolock=1", uri=True, isolation_level=None)
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
        except (OSError, sqlite3.Error) as exc:
            tmp.unlink(missing_ok=True)
            raise StoreError(f"建不出索引临时文件 {tmp}:{exc}") from exc
        return cls(conn, path, dims, tmp=tmp, force_no_vector=force_no_vector)

    def publish(self) -> None:
        """写完换上去:关连接 → 刷盘 → 原子替换。"""
        if self._tmp is None:
            raise StoreError("只读打开的索引不能 publish")
        tmp, self._tmp = self._tmp, None
        try:
            self._conn.close()
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise StoreError(f"索引换不上去({self.path}):{exc}") from exc

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
        if self._tmp is not None:             # 没 publish 就关:临时副本丢掉,原文件不动
            self._tmp.unlink(missing_ok=True)
            self._tmp = None

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- 底层 ---------------------------------------------------------------

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple]:
        try:
            return [tuple(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]
        except sqlite3.Error as exc:
            raise StoreError(f"{exc}\nSQL: {sql.strip()[:200]}") from exc

    def _transaction(self, statements: Iterable[Tuple[str, Sequence[Any]]]) -> None:
        """多条语句一个事务:全成或全败,不留半截文档。"""
        try:
            self._conn.execute("BEGIN")
            for sql, params in statements:
                self._conn.execute(sql, tuple(params))
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise StoreError(str(exc)) from exc

    def _has_novec(self) -> bool:
        """旧索引文件(只读打开、没法补列)里没有 novec:覆盖率与补向量按「全都要向量」算,与以前一致。"""
        if not hasattr(self, "_novec_col"):
            try:
                cols = [r[1] for r in self._query("PRAGMA table_info(kb_chunks)")]
            except StoreError:
                cols = []
            self._novec_col = "novec" in cols
        return self._novec_col

    def _has_table(self, name: str) -> bool:
        return bool(self._query("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)))

    # --- 能力与建表 -----------------------------------------------------------

    def capabilities(self) -> Capabilities:
        version = "SQLite %s" % sqlite3.sqlite_version + (" · sqlite-vec %s" % self._vec_info if self._vector else "")
        return Capabilities(engine=ENGINE, version=version, vector=self._vector, hnsw=False, dims=self.dims)

    @property
    def vector_note(self) -> str:
        """向量不可用时的原因(状态行用)。"""
        return "" if self._vector else self._vec_info

    def setup(self) -> Capabilities:
        """幂等建表。旧格式的库(schema_version 对不上)整个重建 —— 它只是派生索引。"""
        if self._has_table("kb_meta") and self._meta_get("schema_version") not in (None, SCHEMA_VERSION):
            self._drop_all()
        self._transaction([(sql, ()) for sql in _SCHEMA])
        self.__dict__.pop("_novec_col", None)     # 表可能刚建 / 刚删过:按现状重查,不用上一次的缓存
        if not self._has_novec():                 # 2026-10-25 前建的库没有 novec 列:补上(默认 0,行为不变)
            self._transaction([("ALTER TABLE kb_chunks ADD COLUMN novec INTEGER NOT NULL DEFAULT 0", ())])
            self._novec_col = True
        caps = self.capabilities()
        self._meta_set("schema_version", SCHEMA_VERSION)
        self._meta_set("engine", ENGINE)
        self._meta_set("vector_engine", VECTOR_ENGINE if caps.vector else "none")
        self._meta_set("dims", str(self.dims))
        return caps

    def _drop_all(self) -> None:
        self._transaction([("DROP TABLE IF EXISTS %s" % t, ()) for t in _TABLES])

    def rebuild(self) -> Capabilities:
        """清空派生索引(文件才是真相)。"""
        self._drop_all()
        return self.setup()

    def has_index(self) -> bool:
        return self._has_table("kb_docs") and self._meta_get("schema_version") == SCHEMA_VERSION

    # --- meta -------------------------------------------------------------

    def check_embed_model(self, model: str, rebuild: bool) -> None:
        """记住这套向量是哪个模型算的;换了模型而不重建就拒绝。

        维度一样、模型不同时,库里会混着两套向量空间,相似度失去意义,而覆盖率仍显示 100%。
        """
        recorded = self._meta_get("embed_model")
        if recorded and recorded != model and not rebuild:
            raise StoreError(
                f"这套向量是用 {recorded} 算的，现在配置的是 {model}。"
                f"两个模型的向量空间不同，混在一起算相似度没有意义，而覆盖率仍会显示 100%。"
                f"要换模型请跑 `kb.py index --rebuild`（清空重灌）；"
                f"若只是配置写错了，把 kb.yaml 的 embeddings.model 改回 {recorded}。")
        if recorded != model:
            self._meta_set("embed_model", model)

    def _meta_get(self, key: str) -> Optional[str]:
        if not self._has_table("kb_meta"):
            return None
        rows = self._query("SELECT value FROM kb_meta WHERE key = ?", (key,))
        return str(rows[0][0]) if rows else None

    def _meta_set(self, key: str, value: str) -> None:
        self._transaction([("INSERT OR REPLACE INTO kb_meta (key, value) VALUES (?, ?)", (key, value))])

    def meta_get(self, key: str) -> Optional[str]:
        return self._meta_get(key)

    def meta_set(self, key: str, value: str) -> None:
        self._meta_set(key, value)

    # --- 写 -----------------------------------------------------------------

    def _embedding(self, values: Optional[Sequence[float]]) -> Optional[bytes]:
        if values is None or not self._vector:
            return None
        if len(values) != self.dims:
            raise StoreError(f"向量维度 {len(values)} 与配置的 {self.dims} 不一致(kb.yaml embeddings.dims)")
        return _pack(values)

    @staticmethod
    def _delete_doc_stmts(doc_id: str) -> List[Tuple[str, Sequence[Any]]]:
        return [("DELETE FROM kb_chunk_terms WHERE rid IN (SELECT rid FROM kb_chunks WHERE doc_id = ?)", (doc_id,)),
                ("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,)),
                ("DELETE FROM kb_docs WHERE id = ?", (doc_id,))]

    def upsert_doc(self, doc: DocRow, chunks: Sequence[ChunkRow]) -> None:
        """整篇替换:旧块与它们的词条删光再写新块,一个事务。"""
        stmts = self._delete_doc_stmts(doc.id) + [(
            "INSERT INTO kb_docs (id, kind, title, source, version, meta, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc.id, doc.kind, doc.title, doc.source, doc.version,
             json.dumps(doc.meta, ensure_ascii=False), doc.content_hash))]
        for ch in chunks:
            tf, length = bm25.term_freqs(list(ch.tokens), list(ch.signal_tokens))
            stmts.append((
                "INSERT INTO kb_chunks (id, doc_id, seq, section, content, content_hash, ntok, embedding, novec) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ch.id, ch.doc_id, int(ch.seq), ch.section, ch.content, ch.content_hash, length,
                 None if ch.novec else self._embedding(ch.embedding), 1 if ch.novec else 0)))
            stmts += [("INSERT INTO kb_chunk_terms (term, rid, tf) "
                       "VALUES (?, (SELECT rid FROM kb_chunks WHERE id = ?), ?)", (t, ch.id, f))
                      for t, f in tf.items()]
        self._transaction(stmts)

    def delete_doc(self, doc_id: str) -> None:
        self._transaction(self._delete_doc_stmts(doc_id))

    def upsert_node_vectors(self, rows: Sequence[NodeVectorRow]) -> None:
        stmts: List[Tuple[str, Sequence[Any]]] = []
        for r in rows:
            tf, length = bm25.term_freqs(list(r.tokens), list(r.signal_tokens))
            stmts += [
                ("DELETE FROM kb_node_terms WHERE rid IN (SELECT rid FROM kb_node_vectors WHERE node_id = ?)",
                 (r.node_id,)),
                ("DELETE FROM kb_node_vectors WHERE node_id = ?", (r.node_id,)),
                ("INSERT INTO kb_node_vectors (node_id, kind, name, signals, ntok, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                 (r.node_id, r.kind, r.name, r.signals, length, self._embedding(r.embedding)))]
            stmts += [("INSERT INTO kb_node_terms (term, rid, tf) "
                       "VALUES (?, (SELECT rid FROM kb_node_vectors WHERE node_id = ?), ?)", (t, r.node_id, f))
                      for t, f in tf.items()]
        if stmts:
            self._transaction(stmts)

    def delete_node_vectors(self, node_ids: Sequence[str]) -> None:
        stmts: List[Tuple[str, Sequence[Any]]] = []
        for n in node_ids:
            stmts += [("DELETE FROM kb_node_terms WHERE rid IN (SELECT rid FROM kb_node_vectors WHERE node_id = ?)", (n,)),
                      ("DELETE FROM kb_node_vectors WHERE node_id = ?", (n,))]
        if stmts:
            self._transaction(stmts)

    def node_ids(self) -> List[str]:
        return [r[0] for r in self._query("SELECT node_id FROM kb_node_vectors ORDER BY node_id")]

    def set_chunk_embedding(self, chunk_id: str, embedding: Sequence[float]) -> None:
        self._transaction([("UPDATE kb_chunks SET embedding = ? WHERE id = ?",
                            (self._embedding(embedding), chunk_id))])

    def set_node_embedding(self, node_id: str, embedding: Sequence[float]) -> None:
        self._transaction([("UPDATE kb_node_vectors SET embedding = ? WHERE node_id = ?",
                            (self._embedding(embedding), node_id))])

    # --- 读:词法 -------------------------------------------------------------

    def _lexical(self, table: str, terms: str, query_tokens: Sequence[str], extra_cols: str,
                 join: str = "", where: str = "", params: Sequence[Any] = ()) -> List[Tuple[float, Tuple]]:
        """[(分, (rid, *extra_cols))],只含命中的。df 与平均长度按全表算,与文件模式一致。"""
        q = [t for t in dict.fromkeys(query_tokens) if t]
        if not q:
            return []
        n, avg = self._query(f"SELECT count(*), avg(ntok) FROM {table}")[0]
        if not n:
            return []
        marks = _placeholders(len(q))
        df = dict(self._query(f"SELECT term, count(*) FROM {terms} WHERE term IN ({marks}) GROUP BY term", q))
        docs: Dict[int, Tuple[float, Dict[str, float], Tuple]] = {}
        for row in self._query(
                f"SELECT t.rid, t.term, t.tf, x.ntok, {extra_cols} FROM {terms} t JOIN {table} x ON x.rid = t.rid "
                f"{join} WHERE t.term IN ({marks}){where}", list(q) + list(params)):
            rid, term, tf, ntok, extra = row[0], row[1], row[2], row[3], row[4:]
            entry = docs.setdefault(rid, (float(ntok), {}, extra))
            entry[1][term] = float(tf)
        scored = bm25.scores(q, df, int(n), float(avg or 1.0),
                             ((rid, length, tf) for rid, (length, tf, _x) in docs.items()))
        return [(s, (rid,) + docs[rid][2]) for rid, s in scored]

    def search_chunks_lexical(self, query_tokens: Sequence[str], k: int = 10,
                              kinds: Optional[Sequence[str]] = None) -> List[Hit]:
        where, params = self._kind_filter(kinds)
        scored = self._lexical("kb_chunks", "kb_chunk_terms", query_tokens, "x.doc_id, x.seq",
                               join="JOIN kb_docs d ON d.id = x.doc_id", where=where, params=params)
        scored.sort(key=lambda p: (-p[0], p[1][1], p[1][2]))
        top = scored[:int(k)]
        rows = {r[0]: r for r in self._query(
            "SELECT c.rid, c.id, c.doc_id, c.seq, c.section, c.content, d.kind, d.title, d.source, d.meta "
            "  FROM kb_chunks c JOIN kb_docs d ON d.id = c.doc_id WHERE c.rid IN (%s)" % _placeholders(len(top)),
            [key[0] for _s, key in top])} if top else {}
        return [self._chunk_hit(rows[key[0]][1:], s) for s, key in top if key[0] in rows]

    def search_nodes_lexical(self, query_tokens: Sequence[str], k: int = 10,
                             kinds: Optional[Sequence[str]] = None) -> List[Hit]:
        where, params = "", []
        if kinds:
            where, params = " AND x.kind IN (%s)" % _placeholders(len(kinds)), list(kinds)
        scored = self._lexical("kb_node_vectors", "kb_node_terms", query_tokens,
                               "x.node_id, x.kind, x.name, x.signals", where=where, params=params)
        scored.sort(key=lambda p: (-p[0], p[1][1]))
        return [Hit(id=key[1], kind=key[2], title=key[3], score=float(s), content=key[4] or "")
                for s, key in scored[:int(k)]]

    # --- 读:向量 -------------------------------------------------------------

    def _kind_filter(self, kinds: Optional[Sequence[str]]) -> Tuple[str, List[Any]]:
        if not kinds:
            return "", []
        return " AND d.kind IN (%s)" % _placeholders(len(kinds)), list(kinds)

    def search_chunks_vector(self, embedding: Sequence[float], k: int = 10,
                             kinds: Optional[Sequence[str]] = None) -> List[Hit]:
        if not self._vector:
            return []
        where, params = self._kind_filter(kinds)
        rows = self._query(
            "SELECT c.id, c.doc_id, c.seq, c.section, c.content, d.kind, d.title, d.source, d.meta, "
            "       vec_distance_cosine(c.embedding, ?) AS dist "
            "  FROM kb_chunks c JOIN kb_docs d ON d.id = c.doc_id "
            f" WHERE c.embedding IS NOT NULL{where} ORDER BY dist, c.doc_id, c.seq LIMIT ?",
            [_pack(embedding)] + params + [int(k)])
        return [self._chunk_hit(r[:9], 1.0 - float(r[9])) for r in rows]

    def search_nodes_vector(self, embedding: Sequence[float], k: int = 10,
                            kinds: Optional[Sequence[str]] = None) -> List[Hit]:
        if not self._vector:
            return []
        where, params = "", []
        if kinds:
            where, params = " AND kind IN (%s)" % _placeholders(len(kinds)), list(kinds)
        rows = self._query(
            "SELECT node_id, kind, name, vec_distance_cosine(embedding, ?) AS dist, signals FROM kb_node_vectors "
            f" WHERE embedding IS NOT NULL{where} ORDER BY dist, node_id LIMIT ?",
            [_pack(embedding)] + params + [int(k)])
        return [Hit(id=r[0], kind=r[1], title=r[2], score=1.0 - float(r[3]), content=r[4] or "") for r in rows]

    @staticmethod
    def _chunk_hit(r: Tuple, score: float) -> Hit:
        return Hit(id=r[0], doc_id=r[1], seq=int(r[2]), section=r[3] or "", content=r[4] or "",
                   kind=r[5] or "", title=r[6] or "", source=r[7] or "", meta=_meta_json(r[8]),
                   score=float(score))

    # --- 覆盖率与统计 -------------------------------------------------------

    def missing_embeddings(self, limit: int = 500) -> List[Tuple[str, str]]:
        """(chunk_id, content) —— 还没向量的块;向量不可用时返回空。"""
        if not self._vector:
            return []
        novec = " AND novec = 0" if self._has_novec() else ""
        return [(r[0], r[1]) for r in self._query(
            f"SELECT id, content FROM kb_chunks WHERE embedding IS NULL{novec} ORDER BY doc_id, seq LIMIT ?", (int(limit),))]

    def lexical_only_count(self) -> int:
        if not self._has_novec():
            return 0
        return int(self._query("SELECT count(*) FROM kb_chunks WHERE novec = 1")[0][0])

    def missing_count(self) -> int:
        if not self._vector:
            return 0
        novec = " AND novec = 0" if self._has_novec() else ""
        return int(self._query(f"SELECT count(*) FROM kb_chunks WHERE embedding IS NULL{novec}")[0][0])

    def missing_node_embeddings(self, limit: int = 500) -> List[Tuple[str, str]]:
        if not self._vector:
            return []
        return [(r[0], r[1]) for r in self._query(
            "SELECT node_id, name FROM kb_node_vectors WHERE embedding IS NULL ORDER BY node_id LIMIT ?", (int(limit),))]

    def coverage(self) -> Tuple[int, int]:
        """(要向量的切块总数, 有向量的块数)。只建关键词的块(novec)不计入。向量不可用时第二项恒 0。"""
        novec = " WHERE novec = 0" if self._has_novec() else ""
        total = int(self._query(f"SELECT count(*) FROM kb_chunks{novec}")[0][0])
        if not self._vector:
            return total, 0
        return total, int(self._query("SELECT count(*) FROM kb_chunks WHERE embedding IS NOT NULL")[0][0])

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {f"docs.{kind}": int(n)
                               for kind, n in self._query("SELECT kind, count(*) FROM kb_docs GROUP BY kind")}
        out["chunks"] = int(self._query("SELECT count(*) FROM kb_chunks")[0][0])
        out["nodes"] = int(self._query("SELECT count(*) FROM kb_node_vectors")[0][0])
        return out

    def doc_hashes(self) -> Dict[str, str]:
        """id → content_hash,增量索引靠它判断哪些文件变了。"""
        return {r[0]: (r[1] or "") for r in self._query("SELECT id, content_hash FROM kb_docs")}

    def docs_by_ids(self, ids: Sequence[str]) -> Dict[str, DocRow]:
        if not ids:
            return {}
        return {r[0]: DocRow(id=r[0], kind=r[1] or "", title=r[2] or "", source=r[3] or "", version=r[4] or "",
                             meta=_meta_json(r[5]), content_hash=r[6] or "")
                for r in self._query("SELECT id, kind, title, source, version, meta, content_hash FROM kb_docs "
                                     "WHERE id IN (%s)" % _placeholders(len(ids)), list(ids))}

    def doc_sections(self, doc_id: str) -> Dict[str, str]:
        """section → 正文(去掉面包屑首行);案例的「处置」「复发标志」渲染时要用。"""
        out: Dict[str, str] = {}
        for section, content in self._query(
                "SELECT section, content FROM kb_chunks WHERE doc_id = ? ORDER BY seq", (doc_id,)):
            body = content or ""
            if "\n" in body and " › " in body.split("\n", 1)[0]:
                body = body.split("\n", 1)[1]
            out.setdefault(section or "", body)
        return out
