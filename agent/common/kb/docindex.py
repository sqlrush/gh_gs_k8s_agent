"""官方文档集的检索索引:docsets/<代号>/items/*.md → docsets/<代号>/index.sqlite(与客户知识库的索引分开)。

切块:摘要一块(GUC 参数名作信号 token);其余小节各自滑动切;错误码页按错误码一码一块、只建关键词(novec)。
增量:按条目文件内容哈希,没变的不重写、不重算向量。
限时:budget_s 到了就停在一个完整条目之后,发布已写的部分;再跑一次从没写的接着来(条目级幂等)。
对话里单次命令有超时,一次算一万多块向量要好几分钟,所以 kb.py index 默认带时限,没做完退出 2「有活没干完」。
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from . import docset as kbdocset
from . import docsplit
from . import store as spg
from . import text as kbtext
from .embed import Embedder

_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_SECTION_RE = re.compile(r"^## (.+?)\s*$", re.M)
CHUNK_LEN = 800
CHUNK_OVERLAP = 100
_META_KEYS = ("docset", "product", "versions", "deployment", "kind", "doc_path", "page", "doc_key", "summary_by", "as_of")


@dataclass(frozen=True)
class DocsetIndexReport:
    code: str
    docs_total: int = 0
    docs_indexed: int = 0
    docs_unchanged: int = 0
    docs_removed: int = 0
    docs_left: int = 0           # 时限到了还没写的条目数
    chunks_written: int = 0
    embedded: int = 0
    embed_failed: int = 0
    chunk_total: int = 0         # 要向量的块
    chunk_embedded: int = 0
    lexical_only: int = 0        # 只建关键词的块(错误码)
    embed_model: str = ""
    elapsed_s: float = 0.0

    @property
    def done(self) -> bool:
        return self.docs_left == 0 and (not self.embed_model or self.chunk_total == self.chunk_embedded)


def parse_item(text: str) -> Tuple[Dict, Dict[str, str]]:
    """条目文件 → (frontmatter, {小节: 正文})。小节名可以带空格(与案例的解析不同)。"""
    m = _FRONT_RE.match(text)
    meta = yaml.safe_load(m.group(1)) if m else {}
    body = text[m.end():] if m else text
    marks = list(_SECTION_RE.finditer(body))
    secs: Dict[str, str] = {}
    for i, mk in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        secs[mk.group(1).strip()] = body[mk.end():end].strip()
    return (meta if isinstance(meta, dict) else {}), secs


def sliding(text: str, size: int = CHUNK_LEN, overlap: int = CHUNK_OVERLAP) -> List[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        out.append(text[start:start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return out


def _chunk(doc_id: str, seq: int, section: str, content: str, signal: Sequence[str] = (),
           novec: bool = False) -> spg.ChunkRow:
    return spg.ChunkRow(id=f"{doc_id}#{seq}", doc_id=doc_id, seq=seq, section=section, content=content,
                        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
                        tokens=kbtext.tokenize(content), signal_tokens=tuple(signal), novec=novec)


def item_doc(path: pathlib.Path) -> Optional[Tuple[spg.DocRow, List[spg.ChunkRow]]]:
    raw = path.read_text(encoding="utf-8")
    meta, secs = parse_item(raw)
    item_id, title = str(meta.get("id") or path.stem), str(meta.get("title") or path.stem)
    doc_id = f"product:{item_id}"
    kind = str(meta.get("kind") or docsplit.KIND_PAGE)
    summary = secs.get("摘要", "")
    signal = kbtext.tokenize(title.split("(")[0]) if kind == docsplit.KIND_GUC else ()
    chunks = [_chunk(doc_id, 0, "摘要", f"{title} › 摘要\n{summary}", signal)]
    for name, body in secs.items():
        if name == "摘要" or not body:
            continue
        if kind == docsplit.KIND_ERRCODE:
            for code, block in docsplit.errcode_blocks(body) or [("正文", body)]:
                chunks.append(_chunk(doc_id, len(chunks), code, f"{title} › {code}\n{block}", novec=True))
            continue
        for piece in sliding(body):
            chunks.append(_chunk(doc_id, len(chunks), name, f"{title} › {name}\n{piece}"))
    dmeta = {k: meta[k] for k in _META_KEYS if meta.get(k) not in (None, "", [])}
    dmeta.update({"type": "product", "topics": list(meta.get("topics") or []), "summary": summary,
                  "file": f"{kbdocset.DOCSETS_DIR}/{meta.get('docset', '')}/items/{path.name}"})
    doc = spg.DocRow(id=doc_id, kind="product", title=title, source=str(meta.get("source") or ""),
                     version=str(meta.get("versions") or ""), meta=dmeta,
                     content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])
    return doc, chunks


def run(ds: kbdocset.Docset, store, embedder: Optional[Embedder], budget_s: float = 0.0,
        rebuild: bool = False) -> DocsetIndexReport:
    """把一套文档集写进它自己的 index.sqlite(store 是该文件的写入副本,调用方负责 publish)。"""
    started = time.monotonic()
    caps = store.rebuild() if rebuild else store.setup()
    use_vec = caps.vector and embedder is not None
    if use_vec:
        store.check_embed_model(embedder.model, rebuild)
    files = sorted((ds.root / "items").glob("*.md"))
    existing = store.doc_hashes()
    wanted_ids = set()
    indexed = unchanged = written = embedded = failed = left = 0
    for i, path in enumerate(files):
        built = item_doc(path)
        if built is None:
            continue
        doc, chunks = built
        wanted_ids.add(doc.id)
        if not rebuild and existing.get(doc.id) == doc.content_hash:
            unchanged += 1
            continue
        if budget_s and time.monotonic() - started > budget_s:
            left = len(files) - i
            break
        if use_vec:
            want = [c for c in chunks if not c.novec]
            vecs = embedder.embed([c.content for c in want])
            s = embedder.stats
            embedded, failed = s.embedded, s.failed
            by_id = {c.id: v for c, v in zip(want, vecs)}
            chunks = [spg.ChunkRow(**{**c.__dict__, "embedding": by_id.get(c.id)}) for c in chunks]
        store.upsert_doc(doc, chunks)
        indexed += 1
        written += len(chunks)
    removed = 0
    if not left:                                   # 只有完整跑完一遍才知道哪些条目真没了
        for doc_id in existing:
            if doc_id not in wanted_ids:
                store.delete_doc(doc_id)
                removed += 1
    if use_vec and not left and (not budget_s or time.monotonic() - started < budget_s):
        for chunk_id, content in store.missing_embeddings(limit=2000):
            if budget_s and time.monotonic() - started > budget_s:
                break
            v = embedder.embed_one(content)
            if v is not None:
                store.set_chunk_embedding(chunk_id, v)
    total, done = store.coverage()
    lex_only = store.lexical_only_count()
    return DocsetIndexReport(code=ds.code, docs_total=len(files), docs_indexed=indexed, docs_unchanged=unchanged,
                             docs_removed=removed, docs_left=left, chunks_written=written, embedded=embedded,
                             embed_failed=failed, chunk_total=total, chunk_embedded=done, lexical_only=lex_only,
                             embed_model=embedder.model if use_vec else "",
                             elapsed_s=round(time.monotonic() - started, 1))


def state_of(rep: DocsetIndexReport) -> Dict:
    return {"complete": rep.done, "docs": rep.docs_total, "docs_left": rep.docs_left,
            "chunks_vector": rep.chunk_total, "chunks_embedded": rep.chunk_embedded,
            "chunks_lexical_only": rep.lexical_only, "embed_model": rep.embed_model,
            "indexed_at": time.strftime("%Y-%m-%d %H:%M:%S")}


__all__ = ["DocsetIndexReport", "parse_item", "item_doc", "run", "state_of", "sliding"]
