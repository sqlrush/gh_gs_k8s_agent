"""kb.py 的存储侧子命令:setup / index / feedback / eval(query / health 在查询 skill gaussdb-kb)。

全部确定性:建索引、检索、打分。模型不参与。
向量 + 词法索引是知识库目录里的 <kb>/index/store.sqlite(SQLite + sqlite-vec),不用配、不用口令;
2026-10-23 起原高斯 / PG 向量库下线。Neo4j(可选)的口令来自 common.credential,这里不接收、不打印口令。
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple

import yaml

from common.kb import config as kbconfig
from common.kb import indexer, query as kbquery, render
from common.kb import store_graph as sg
from common.kb import store as spg
from common.kb import store_sqlite as ssq
from common.kb.embed import Embedder, EmbedError
from common.kb import lock as kblock
from common.kb.atomic import write_text_atomic


class StoreCmdError(Exception):
    pass


def _cfg(kb: pathlib.Path) -> kbconfig.KbConfig:
    try:
        return kbconfig.load(kb)
    except kbconfig.KbConfigError as exc:
        raise StoreCmdError(str(exc))


def _password(name: str) -> str:
    from common.credential import CredentialError, load_secret
    try:
        return load_secret(name)
    except CredentialError as exc:
        raise StoreCmdError(
            f"取不到凭据 {name!r}:{exc}\n先运行 `python3 -m common.credential_cli set {name}` 加密保存口令")


def open_writer(cfg: kbconfig.KbConfig, fresh: bool = False) -> ssq.SqliteStore:
    """在临时副本上写索引;写完 publish() 才换上去。调用方持知识库写锁。"""
    try:
        return ssq.SqliteStore.open_writer(ssq.store_path(cfg.kb_dir), dims=cfg.embeddings.dims, fresh=fresh)
    except spg.StoreError as exc:
        raise StoreCmdError(str(exc))


def legacy_note(cfg: kbconfig.KbConfig) -> str:
    """kb.yaml 里还留着已下线的 store.pg:不报错,提示可以删。"""
    if not cfg.store.legacy_pg:
        return ""
    return ("kb.yaml 里的 store.pg 已不再使用(2026-10-23 起向量库改为知识库目录里的 index/store.sqlite,"
            "不用配),可以删掉这一段;KB_PG_* 环境变量与对应 Secret 也不再需要")


def open_graph(cfg: kbconfig.KbConfig, required: bool = False) -> Optional[sg.GraphStore]:
    if cfg.store.graph is None:
        if required:
            raise StoreCmdError("kb.yaml 未配置 store.graph(Neo4j)")
        return None
    g = cfg.store.graph
    store = sg.GraphStore(g.url, g.user, _password(g.credential), database=g.database)
    try:
        store.ping()
    except sg.GraphStoreError as exc:
        if required:
            raise StoreCmdError(str(exc))
        print(f"警告:图库不可用({exc}),本次只写 SQLite 索引;修好后重跑 index", file=sys.stderr)
        return None
    return store


def open_embedder(cfg: kbconfig.KbConfig, caps: spg.Capabilities, why: str = "") -> Optional[Embedder]:
    if not caps.vector:
        print("提示:向量不可用(%s),本库只做词法 + 图(kb_meta.vector_engine=none)" % (why or "sqlite-vec 不可用"),
              file=sys.stderr)
        return None
    try:
        emb = Embedder.from_config(cfg)
    except (EmbedError, kbconfig.KbConfigError) as exc:
        print(f"警告:embedding 未启用({exc});向量列留空,检索只走词法 + 图", file=sys.stderr)
        return None
    if emb is None:
        print("提示:kb.yaml 未配 embeddings,向量列留空;配好后再跑一次 `kb.py index` 就会补齐", file=sys.stderr)
    return emb


def _version(kb: pathlib.Path) -> str:
    try:
        return (kb / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ---------------------------------------------------------------- setup

def cmd_setup(args: argparse.Namespace) -> int:
    """报告索引与图库的能力。索引文件不用预先建:`kb.py index` 会建。"""
    kb = kbconfig.resolve_kb_dir(args.kb)
    cfg = _cfg(kb)
    store = open_writer(cfg)                 # 只为探测能力;不 publish,原索引不动
    try:
        caps = store.capabilities()
        why = store.vector_note
    finally:
        store.close()
    print(f"索引      : {ssq.store_path(kb)} · {caps.version}")
    print(f"向量      : " + (f"有(sqlite-vec · 精确检索 · 维度 {caps.dims})" if caps.vector
                             else f"无({why})——只做词法 + 图"))
    graph = open_graph(cfg)
    if graph is not None:
        graph.setup()
        print(f"Neo4j     : {graph.ping()} · 约束已建")
    else:
        print("Neo4j     : 未配置/不可用(路径改走 graph/*.yaml 图文件)")
    print(f"embedding : {cfg.embeddings.source}" + (f" · {cfg.embeddings.model} · {cfg.embeddings.dims} 维"
                                                     if cfg.embeddings.source != 'none' else "(未启用)"))
    note = legacy_note(cfg)
    if note:
        print(f"[warn ] {note}")
    print("Next: kb.py index")
    return 0


# ---------------------------------------------------------------- index

def cmd_index(args: argparse.Namespace) -> int:
    kb = kbconfig.resolve_kb_dir(args.kb)
    if not kb.is_dir():
        raise StoreCmdError(f"KB 目录不存在:{kb}")
    cfg = _cfg(kb)
    store = open_writer(cfg, fresh=args.rebuild)
    try:
        caps = store.setup()
        graph = open_graph(cfg)
        embedder = open_embedder(cfg, caps, store.vector_note)
        rep = indexer.run_index(kb, store, graph, embedder, kb_version=_version(kb),
                                rebuild=args.rebuild, fill_missing=args.fill_missing)
        store.publish()
    except spg.StoreError as exc:
        raise StoreCmdError(str(exc))
    finally:
        store.close()
    note = legacy_note(cfg)
    if note:
        print(f"[warn ] {note}")
    print(f"文档      : 新写 {rep.docs_indexed} · 未变 {rep.docs_unchanged} · 删除 {rep.docs_removed} · 切块 {rep.chunks_written}")
    print(f"向量      : 引擎 {rep.vector_engine} · 新算 {rep.embedded} · 缓存 {rep.embed_cached} · 失败 {rep.embed_failed} · "
          f"覆盖 {rep.chunk_embedded}/{rep.chunk_total}")
    print(f"图        : {rep.graph} · 节点 {rep.nodes} · 边 {rep.edges}(已确认 {rep.edges_confirmed})")
    for w in rep.warnings:
        print(f"[warn ] {w}")
    if not rep.coverage_ok:
        print(f"[error] 向量覆盖率 {rep.chunk_embedded}/{rep.chunk_total} < 100%——"
              f"embedding 失败或未配置;修好后再跑一次 `kb.py index`(端点不稳时加 --fill-missing 强制逐批重试)。未补齐前状态行会如实显示。")
        return 2
    if rep.graph == "unavailable":
        return 2
    return 0


# ---------------------------------------------------------------- feedback

def cmd_feedback(args: argparse.Namespace) -> int:
    kb = kbconfig.resolve_kb_dir(args.kb)
    path = kb / "eval" / "feedback.yaml"
    entry = {"id": args.id, "verdict": "useful" if args.useful else "irrelevant",
             "at": datetime.date.today().isoformat(), "note": args.note or ""}
    with kblock.hold(kb):                       # 追加也是写共享文件:持锁,整文件原子替换
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        write_text_atomic(path, old + "- " + json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"已记录:{entry['id']} → {entry['verdict']}(采纳率加权在下次 index 后生效)")
    return 0


# ---------------------------------------------------------------- eval

def cmd_eval(args: argparse.Namespace) -> int:
    """eval/queries.yaml:[{q, expect: [doc_id…], canary: bool}] → recall@k 与金丝雀命中。"""
    kb = kbconfig.resolve_kb_dir(args.kb)
    path = kb / "eval" / "queries.yaml"
    if not path.is_file():
        raise StoreCmdError(f"没有黄金查询集:{path}")
    try:
        cases = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError as exc:
        raise StoreCmdError(f"queries.yaml 解析失败:{exc}")
    sess = kbquery.KbSession.open(kb)
    if not sess.attached:
        raise StoreCmdError(f"知识库未接入:{sess.reason}")
    k = int(args.k)
    hit = total = 0
    canary_miss: List[str] = []
    try:
        for c in cases:
            q = str(c.get("q") or "")
            expect = [str(e) for e in (c.get("expect") or [])]
            res = kbquery.from_text(q, session=sess)
            got: List[str] = []
            for it in res.items:      # 分类型各取前 k:条款和案例不互相挤占名额
                got += [r.id for r in it.clauses][:k] + [r.id for r in it.cases][:k] + [r.id for r in it.raws][:k]
                got += [r.id for r in it.typed]           # 六类类型化条目:每类本来就只出 top_typed 条
            ok = any(e in got for e in expect) if expect else not got
            total += 1
            hit += int(ok)
            mark = "✓" if ok else "✗"
            print(f"[{mark}] {q[:50]} → {got[:3]}")
            if c.get("canary") and not ok:
                canary_miss.append(q)
    finally:
        sess.close()
    recall = hit / total if total else 0.0
    print(f"recall@{k}: {hit}/{total} = {recall:.2f}" + (f" · 金丝雀未中 {len(canary_miss)}" if canary_miss else ""))
    if canary_miss:
        for q in canary_miss:
            print(f"[error] 金丝雀未命中:{q}")
        return 2
    return 0 if recall >= float(args.min_recall) else 2


# ---------------------------------------------------------------- parser wiring

def add_admin_subcommands(sub: "argparse._SubParsersAction") -> None:
    """setup / feedback / eval:只在导入 skill 里。query / health 在 gaussdb-kb(查询)。"""
    p = sub.add_parser("setup", help="报告索引(SQLite + sqlite-vec)与 Neo4j 的能力,Neo4j 配了就建约束")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("feedback", help="DBA 对一次引用打分:--useful / --irrelevant")
    p.add_argument("id")
    p.add_argument("--kb")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--useful", action="store_true")
    g.add_argument("--irrelevant", action="store_true")
    p.add_argument("--note")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("eval", help="跑 eval/queries.yaml,报 recall@k 与金丝雀")
    p.add_argument("--kb")
    p.add_argument("--k", default=3)
    p.add_argument("--min-recall", default=0.8)
    p.set_defaults(func=cmd_eval)
