"""官方文档集(标准知识库的一层):<kb>/docsets/<代号>/ —— 由脚本从 CHM 生成、随工具版本发布、现场只读。

    docsets/G25132C/
      manifest.json      代号、文档名、产品系列与版本、条目数、建索引状态(向量模型、覆盖)
      catalog.json       每条的 ID / 标题 / 摘要 / 专题 / 目录路径(L0 / L1 / 大盘只读它,不扫几千个文件)
      items/<ID>.md      L2 条目(type: product,带 docset 字段)
      pages/<页>.md      L3 原页(整页 markdown)
      plan/              构建配方:plan.yaml(元数据与范围)、topic-map.yaml(专题映射与人工覆盖)、
                         ids.yaml(doc_key → ID,永不复用)、summaries.yaml(模型精修过的摘要)
      index.sqlite       这一套文档自己的检索索引(与客户知识库的 index/store.sqlite 分开,检索时分池)

流程:scan(解包、读目录、出范围清单)→ plan(元数据、专题映射表、样例)→ apply(写条目,原子换上)→ kb.py index。
同一份 CHM + 同一套 plan/ 重放 apply,结果逐条一致——发版本靠它,不再调用模型。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from . import chmdoc, docsplit, htmldoc
from . import topics as kbtopics
from .atomic import write_text_atomic

DOCSETS_DIR = "docsets"
SCHEMA = "docset-1"
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
ID_RE = re.compile(r"^PD-([A-Z][A-Z0-9]{1,9})-(\d{5})$")
SEP = " › "
REQUIRED_META = ("code", "product", "versions")


class DocsetError(Exception):
    """消息直接给用户看,写明下一步做什么。"""


@dataclass(frozen=True)
class Docset:
    code: str
    root: pathlib.Path
    manifest: Dict[str, Any]

    @property
    def title(self) -> str:
        return str(self.manifest.get("title") or self.code)

    @property
    def index_path(self) -> pathlib.Path:
        return self.root / "index.sqlite"

    def catalog(self) -> List[Dict[str, Any]]:
        return load_catalog(self.root)

    def item_path(self, item_id: str) -> pathlib.Path:
        return self.root / "items" / f"{item_id}.md"


# ---------------------------------------------------------------- 路径与读取

def docsets_root(kb: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(kb) / DOCSETS_DIR


def load_manifest(root: pathlib.Path) -> Dict[str, Any]:
    try:
        data = json.loads((pathlib.Path(root) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def installed(kb: pathlib.Path) -> List[Docset]:
    """装好的文档集(目录名合法、有 manifest);临时目录、回滚副本不算。"""
    root = docsets_root(kb)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and CODE_RE.match(d.name):
            m = load_manifest(d)
            if m.get("schema") == SCHEMA:
                out.append(Docset(d.name, d, m))
    return out


def get(kb: pathlib.Path, code: str) -> Docset:
    for ds in installed(kb):
        if ds.code == code:
            return ds
    names = "、".join(d.code for d in installed(kb)) or "无"
    raise DocsetError(f"没有文档集 {code}(已装:{names})")


def load_catalog(root: pathlib.Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads((pathlib.Path(root) / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def code_of(item_id: str) -> Optional[str]:
    m = ID_RE.match(item_id or "")
    return m.group(1) if m else None


def find_item(kb: pathlib.Path, item_id: str) -> Optional[pathlib.Path]:
    code = code_of(item_id)
    if not code:
        return None
    p = docsets_root(kb) / code / "items" / f"{item_id}.md"
    return p if p.is_file() else None


# ---------------------------------------------------------------- scan

def _chapter_rows(nodes: Sequence[chmdoc.TocNode], root: pathlib.Path, depth: int = 3
                  ) -> List[Dict[str, Any]]:
    rows: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for n in nodes:
        key = n.path[:depth] if len(n.path) >= 2 else n.path
        r = rows.setdefault(key, {"chapter": SEP.join(key), "pages": 0, "bytes": 0})
        if n.local and (root / n.local).is_file():
            r["pages"] += 1
            r["bytes"] += (root / n.local).stat().st_size
    return list(rows.values())


def scan(src: pathlib.Path, staging: pathlib.Path) -> Dict[str, Any]:
    """解包到 staging/unpacked,读目录,写 staging/scan.json。返回扫描结果(不碰知识库正式目录)。"""
    root = chmdoc.unpack(src, staging / "unpacked")
    nodes = chmdoc.load_toc(root)
    pages = [n for n in nodes if n.local and (root / n.local).is_file()]
    result = {
        "source_file": pathlib.Path(src).name, "source_sha256": _sha256(src) if pathlib.Path(src).is_file() else "",
        "unpacked": str(root.relative_to(staging)) if root.is_relative_to(staging) else str(root),
        "nodes": len(nodes), "pages": len(pages), "books": sorted({n.path[0] for n in nodes}),
        "chapters": _chapter_rows(nodes, root, depth=2), "versions_seen": chmdoc.version_strings(root, nodes)[:8],
        "scanned_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_text_atomic(staging / "scan.json", json.dumps(result, ensure_ascii=False, indent=1))
    return result


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def unpacked_root(staging: pathlib.Path) -> pathlib.Path:
    scan_file = staging / "scan.json"
    if not scan_file.is_file():
        raise DocsetError(f"{staging} 还没 scan(先 `kb.py ingest <CHM>`)")
    rel = json.loads(scan_file.read_text(encoding="utf-8")).get("unpacked", "unpacked")
    root = pathlib.Path(rel) if pathlib.Path(rel).is_absolute() else staging / rel
    return chmdoc.find_root(root)


# ---------------------------------------------------------------- 切分整份文档

def split_all(root: pathlib.Path, nodes: Sequence[chmdoc.TocNode], exclude: Sequence[str],
              taxonomy: Sequence[kbtopics.Topic]) -> Tuple[List[docsplit.DocItem], Dict[str, List[str]]]:
    """全部页面 → 条目(按目录顺序);同一页面在目录里出现多次只取第一次。返回 (条目, 页面 → markdown 行)。"""
    excl = [tuple(p.split(SEP)) for p in exclude if p]
    items: List[docsplit.DocItem] = []
    pages: Dict[str, List[str]] = {}
    for n in nodes:
        if not n.local or n.local in pages or any(n.path[:len(e)] == e for e in excl):
            continue
        p = root / n.local
        if not p.is_file():
            continue
        lines = htmldoc.page_lines(p)
        pages[n.local] = lines
        items += docsplit.split_page(n.local, n.path, lines, taxonomy)
    return items, pages


def topic_map_rows(items: Sequence[docsplit.DocItem], depth: int = 4) -> List[Dict[str, Any]]:
    """映射表:每个(到第 depth 级的)目录一行,写它下面的条目数与专题分布。给模型核对、给人确认。"""
    rows: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for it in items:
        # GUC 参数按参数类(锁管理、自动清理…)分行,一类一个专题决定;其余按第 depth 级目录
        key = it.path[:5] if it.kind == docsplit.KIND_GUC else it.path[:min(depth, max(1, len(it.path) - 1))]
        r = rows.setdefault(key, {"chapter": SEP.join(key), "items": 0, "topics": {}})
        r["items"] += 1
        for t in it.topics or ("(无)",):
            r["topics"][t] = r["topics"].get(t, 0) + 1
    return list(rows.values())


# ---------------------------------------------------------------- plan

def read_plan(plan_dir: pathlib.Path) -> Dict[str, Any]:
    try:
        data = yaml.safe_load((plan_dir / "plan.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise DocsetError(f"读不了 {plan_dir / 'plan.yaml'}:{exc}") from exc
    missing = [k for k in REQUIRED_META if not data.get(k)]
    if missing:
        raise DocsetError(f"plan.yaml 缺少 {'、'.join(missing)}(`kb.py docset plan <批次> --code … --product … --versions …`)")
    if not CODE_RE.match(str(data["code"])):
        raise DocsetError(f"文档集代号 {data['code']!r} 不合规:大写字母开头、2–10 位大写字母或数字,如 G25132C")
    return data


def overrides(plan_dir: pathlib.Path) -> List[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    try:
        rows = yaml.safe_load((plan_dir / "topic-map.yaml").read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and "override" in r and r.get("chapter"):
            out.append((tuple(str(r["chapter"]).split(SEP)), tuple(str(t) for t in (r["override"] or []))))
    return out


def summaries(plan_dir: pathlib.Path) -> Dict[str, Dict[str, str]]:
    try:
        data = yaml.safe_load((plan_dir / "summaries.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict) and v.get("summary")} if isinstance(data, dict) else {}


def build_items(root: pathlib.Path, plan_dir: pathlib.Path
                ) -> Tuple[List[docsplit.DocItem], Dict[str, List[str]], List[chmdoc.TocNode]]:
    """按 plan/ 切出最终条目:范围排除 → 规则专题 → 人工覆盖 → 精修摘要。"""
    plan = read_plan(plan_dir)
    nodes = chmdoc.load_toc(root)
    items, pages = split_all(root, nodes, plan.get("exclude") or [], kbtopics.DEFAULT_TOPICS)
    items = docsplit.apply_override(items, overrides(plan_dir))
    refined = summaries(plan_dir)
    items = [docsplit.DocItem(**{**it.__dict__, "summary": refined[it.doc_key]["summary"][:docsplit.SUMMARY_MAX],
                                 "summary_by": "model"}) if it.doc_key in refined else it for it in items]
    return items, pages, nodes


# ---------------------------------------------------------------- ID(永不复用)

def assign_ids(code: str, keys: Sequence[str], ids_file: pathlib.Path) -> Dict[str, str]:
    """doc_key → ID。已有的沿用;新的按目录顺序接着编;不再出现的保留在表里(号不再分给别人)。"""
    try:
        data = yaml.safe_load(ids_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    table: Dict[str, str] = dict(data.get("ids") or {})
    used = {int(m.group(2)) for v in table.values() for m in [ID_RE.match(v)] if m}
    nxt = max(used, default=0) + 1
    for k in keys:
        if k not in table:
            table[k] = f"PD-{code}-{nxt:05d}"
            nxt += 1
    if nxt > 99999:
        raise DocsetError(f"文档集 {code} 的编号用完了(5 位);换一个文档集代号")
    ids_file.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(ids_file, yaml.safe_dump({"code": code, "ids": table}, allow_unicode=True, sort_keys=False))
    return table


# ---------------------------------------------------------------- 写盘

def _page_name(local: str) -> str:
    return re.sub(r"[^\w.-]", "_", pathlib.PurePosixPath(local).stem) + ".md"


def _source(plan: Dict[str, Any], it: docsplit.DocItem) -> str:
    tail = f"#{it.doc_key.split('#', 1)[1]}" if "#" in it.doc_key else ""
    return f"{plan.get('source_file') or plan.get('title') or plan['code']}{SEP}{SEP.join(it.path)}{tail}"


def render_item(plan: Dict[str, Any], it: docsplit.DocItem, item_id: str) -> str:
    front = {
        "id": item_id, "type": "product", "title": it.title, "docset": plan["code"],
        "product": plan["product"], "versions": str(plan["versions"]),
        **({"deployment": plan["deployment"]} if plan.get("deployment") else {}),
        "kind": it.kind, "topics": list(it.topics), "source": _source(plan, it),
        "doc_path": list(it.path), "doc_key": it.doc_key, "page": f"pages/{_page_name(it.page)}",
        **({"as_of": str(plan["as_of"])} if plan.get("as_of") else {}), "summary_by": it.summary_by,
    }
    body = ["## 摘要", it.summary, ""]
    for name, text in it.sections:
        body += [f"## {name}", text, ""]
    return "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False) + "---\n" + "\n".join(body)


def render_page(plan: Dict[str, Any], path: Sequence[str], lines: Sequence[str]) -> str:
    head = [f"# {path[-1]}", "", f"> 出处:{plan.get('source_file') or plan['code']}{SEP}{SEP.join(path)}", ""]
    return "\n".join(head + [ln for ln in lines if not ln.startswith("# ")]) + "\n"


def apply(kb: pathlib.Path, root: pathlib.Path, plan_dir: pathlib.Path) -> Dict[str, Any]:
    """按 plan/ 生成 docsets/<代号>/(临时目录写好再原子换上,旧索引带过去以便增量)。返回 manifest。"""
    plan = read_plan(plan_dir)
    code = str(plan["code"])
    items, pages, nodes = build_items(root, plan_dir)
    if not items:
        raise DocsetError("按当前范围切不出任何条目(排除得太多,或目录里的页面都找不到)")
    base = docsets_root(kb)
    base.mkdir(parents=True, exist_ok=True)
    final = base / code
    tmp = base / f".{code}.tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "items").mkdir(parents=True)
    (tmp / "pages").mkdir()
    shutil.copytree(plan_dir, tmp / "plan", dirs_exist_ok=True)
    if (final / "plan" / "ids.yaml").is_file():                 # ID 表以已装的为准:永不复用
        shutil.copyfile(final / "plan" / "ids.yaml", tmp / "plan" / "ids.yaml")
    ids = assign_ids(code, [it.doc_key for it in items], tmp / "plan" / "ids.yaml")
    path_of = {n.local: n.path for n in nodes if n.local}
    catalog = []
    for it in items:
        item_id = ids[it.doc_key]
        (tmp / "items" / f"{item_id}.md").write_text(render_item(plan, it, item_id), encoding="utf-8")
        catalog.append({"id": item_id, "doc_key": it.doc_key, "title": it.title, "kind": it.kind,
                        "summary": it.summary, "summary_by": it.summary_by, "topics": list(it.topics),
                        "path": list(it.path), "sections": [n for n, _b in it.sections], "chars": it.chars})
    for local, lines in pages.items():
        (tmp / "pages" / _page_name(local)).write_text(render_page(plan, path_of.get(local, (local,)), lines),
                                                       encoding="utf-8")
    (tmp / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    manifest = _manifest(plan, items, len(pages))
    (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    if (final / "index.sqlite").is_file():
        shutil.copyfile(final / "index.sqlite", tmp / "index.sqlite")
    swap_in(final, tmp)
    return manifest


def _manifest(plan: Dict[str, Any], items: Sequence[docsplit.DocItem], pages: int) -> Dict[str, Any]:
    kinds: Dict[str, int] = {}
    topics: Dict[str, int] = {}
    for it in items:
        kinds[it.kind] = kinds.get(it.kind, 0) + 1
        for t in it.topics:
            topics[t] = topics.get(t, 0) + 1
    keep = ("code", "title", "source_file", "source_sha256", "product", "versions", "deployment", "doc_version",
            "kernel_version", "engine_version", "as_of")
    return {"schema": SCHEMA, **{k: plan[k] for k in keep if plan.get(k) not in (None, "")},
            "items": len(items), "pages": pages, "kinds": kinds, "topics": topics,
            "model_summaries": sum(1 for it in items if it.summary_by == "model"),
            "built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content_hash": hashlib.sha256("\n".join(f"{it.doc_key}\t{it.summary}\t{it.chars}" for it in items)
                                           .encode("utf-8")).hexdigest()[:16],
            "index": {}}


def swap_in(final: pathlib.Path, tmp: pathlib.Path) -> None:
    """tmp 原子换成 final;旧的留一份在 .rollback/ 里(只留最近一份)。"""
    rollback = final.parent / ".rollback" / final.name
    if final.exists():
        shutil.rmtree(rollback, ignore_errors=True)
        rollback.parent.mkdir(parents=True, exist_ok=True)
        os.replace(final, rollback)
    os.replace(tmp, final)


def update_index_state(root: pathlib.Path, state: Dict[str, Any]) -> None:
    m = load_manifest(root)
    if not m:
        return
    m["index"] = state
    write_text_atomic(root / "manifest.json", json.dumps(m, ensure_ascii=False, indent=1))


def retire(kb: pathlib.Path, code: str) -> pathlib.Path:
    """整套退役:移到 archive/docsets/<代号>-<日期>/。ID 表跟着走,号永不复用。"""
    ds = get(kb, code)
    dest = pathlib.Path(kb) / "archive" / DOCSETS_DIR / f"{code}-{datetime.date.today().isoformat()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(ds.root, dest)
    return dest


__all__ = ["DocsetError", "Docset", "DOCSETS_DIR", "SCHEMA", "CODE_RE", "ID_RE", "SEP", "docsets_root",
           "installed", "get", "load_manifest", "load_catalog", "code_of", "find_item", "scan", "unpacked_root",
           "split_all", "topic_map_rows", "read_plan", "overrides", "summaries", "build_items", "assign_ids",
           "render_item", "render_page", "apply", "swap_in", "update_index_state", "retire"]
