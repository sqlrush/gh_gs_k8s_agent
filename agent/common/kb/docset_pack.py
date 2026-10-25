"""标准知识库包:docsets/<代号>/ ⇄ kb-std-<代号>-<内容版本>.tar.gz(随工具版本发布,部署后自动装上)。

pack:打包前三道检查——测试开关必须关着、该套索引建完整(条目全进索引、要向量的块全有向量)、条目校验零错误;
      任何一道不过就拒绝,不出包。包里有 kb-std.json(清单 + 每个文件的 sha256)。
install:按 kb-std.json 核对每个文件 → 解到临时目录 → 原子换上(旧的留一份在 docsets/.rollback/);
      装着的内容版本(content_hash + built_at)与包里一致就跳过,不动任何文件。客户知识库一个字节都不碰。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import shutil
import tarfile
from typing import Any, Dict, List, Optional, Tuple

from . import docset as kbdocset

PACK_INFO = "kb-std.json"


class PackError(Exception):
    pass


def _sha(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def content_version(manifest: Dict[str, Any]) -> str:
    return f"{manifest.get('content_hash', '')}-{str(manifest.get('built_at', '')).replace(' ', 'T')}"


def preflight(kb: pathlib.Path, ds: kbdocset.Docset, test_switch_on: bool, errors: List[str]) -> List[str]:
    """打包前检查;返回拒绝理由(空 = 可以打包)。errors 是 docset validate 的错误。"""
    why: List[str] = []
    if test_switch_on:
        why.append("kb.yaml 的测试开关 test_switches.docset_applicability_unknown 开着——测试开关不许进正式版本,先关掉")
    idx = ds.manifest.get("index") or {}
    if not idx.get("complete"):
        why.append(f"文档集 {ds.code} 的索引没建完整(还差 {idx.get('docs_left', '?')} 条、向量 "
                   f"{idx.get('chunks_embedded', 0)}/{idx.get('chunks_vector', 0)});先跑完 `kb.py index`")
    if errors:
        why.append(f"文档集 {ds.code} 校验有 {len(errors)} 个错误(`kb.py docset validate {ds.code}`)")
    return why


def pack(ds: kbdocset.Docset, out_dir: pathlib.Path) -> pathlib.Path:
    files = sorted(p for p in ds.root.rglob("*") if p.is_file() and not p.name.endswith((".tmp", ".lock")))
    info = {"schema": "kb-std-1", "code": ds.code, "content_version": content_version(ds.manifest),
            "manifest": ds.manifest,
            "files": {p.relative_to(ds.root).as_posix(): _sha(p) for p in files}}
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"kb-std-{ds.code}-{info['content_version'].split('-')[0]}.tar.gz"
    tmp = out.with_name(out.name + f".tmp-{os.getpid()}")
    with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
        data = json.dumps(info, ensure_ascii=False, indent=1).encode("utf-8")
        ti = tarfile.TarInfo(PACK_INFO)
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
        for p in files:
            tar.add(p, arcname=f"{ds.code}/{p.relative_to(ds.root).as_posix()}", recursive=False)
    os.replace(tmp, out)
    return out


def read_info(pack_path: pathlib.Path) -> Dict[str, Any]:
    try:
        with tarfile.open(pack_path, "r:gz") as tar:
            fh = tar.extractfile(PACK_INFO)
            if fh is None:
                raise PackError(f"{pack_path.name} 里没有 {PACK_INFO}:不是标准知识库包")
            return json.loads(fh.read().decode("utf-8"))
    except (OSError, tarfile.TarError, KeyError, ValueError) as exc:
        raise PackError(f"读不了标准知识库包 {pack_path}:{exc}") from exc


def install(kb: pathlib.Path, pack_path: pathlib.Path) -> Tuple[str, str]:
    """装一个包。返回 (结果, 说明):结果是 installed / skipped。"""
    info = read_info(pack_path)
    code = str(info.get("code") or "")
    if not kbdocset.CODE_RE.match(code):
        raise PackError(f"包里的文档集代号 {code!r} 不合规")
    base = kbdocset.docsets_root(kb)
    current = kbdocset.load_manifest(base / code)
    if current and content_version(current) == info.get("content_version"):
        return "skipped", f"{code} 已是这个版本({info.get('content_version')}),不动"
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".{code}.install-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        with tarfile.open(pack_path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name == PACK_INFO:
                    continue
                rel = pathlib.PurePosixPath(m.name)
                if rel.is_absolute() or ".." in rel.parts or rel.parts[0] != code or not (m.isfile() or m.isdir()):
                    raise PackError(f"包里有不该有的路径:{m.name}")
                if m.isfile():
                    dest = tmp / pathlib.Path(*rel.parts[1:])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src = tar.extractfile(m)
                    with open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
        expected = info.get("files") or {}
        got = {p.relative_to(tmp).as_posix(): _sha(p) for p in tmp.rglob("*") if p.is_file()}
        bad = sorted(k for k in expected if got.get(k) != expected[k])
        extra = sorted(set(got) - set(expected))
        if bad or extra:
            raise PackError(f"校验和对不上:{len(bad)} 个文件不一致、{len(extra)} 个多余(包可能损坏);没有换上,原有的不动")
        kbdocset.swap_in(base / code, tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    m = info.get("manifest") or {}
    return "installed", f"{code} 已装上:{m.get('title', '')} · {m.get('items', 0)} 条 · 版本 {info.get('content_version')}"


def find_packs(directory: pathlib.Path) -> List[pathlib.Path]:
    return sorted(pathlib.Path(directory).glob("kb-std-*.tar.gz")) if pathlib.Path(directory).is_dir() else []


__all__ = ["PackError", "PACK_INFO", "content_version", "preflight", "pack", "read_info", "install", "find_packs"]
