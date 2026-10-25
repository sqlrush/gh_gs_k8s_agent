"""官方文档包:解包(.chm / 含 .hhc 的 .zip / 已解开的目录)与读目录(.hhc)。

华为产品文档以 CHM 发布:里面是一页一个 HTML,加一份目录文件 .hhc(嵌套的 <UL>/<OBJECT>,
每个节点有 Name 与 Local)。目录才是这份文档的骨架——条目的「书 › 章 › 节」路径、导航页、专题归类都从它来。

解 CHM 用 7-Zip(镜像里装的是 Debian 的 7zip 包,命令 7zz;也认 7z / 7za)。没有就明说装什么,
不去猜、不退回别的办法——解不全的文档比没导更糟(条目少了,却没人知道)。
"""
from __future__ import annotations

import html
import pathlib
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from . import htmldoc

SUFFIXES = (".chm", ".zip")
_SEVENZIP = ("7zz", "7z", "7za")
_TIMEOUT = 900


class ChmError(Exception):
    """解包 / 目录读取失败;消息直接给用户看,写明下一步做什么。"""


@dataclass(frozen=True)
class TocNode:
    path: Tuple[str, ...]      # 从书名到本节点的完整路径
    local: str                 # 页面文件(相对解包目录);纯目录节点为空

    @property
    def title(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def depth(self) -> int:
        return len(self.path)


def is_docset_source(path: pathlib.Path) -> bool:
    p = pathlib.Path(path)
    if p.is_dir():
        return any(p.glob("*.hhc"))
    if p.suffix.lower() == ".chm":
        return True
    if p.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(p) as zf:
                return any(n.lower().endswith(".hhc") for n in zf.namelist())
        except zipfile.BadZipFile:
            return False
    return False


def _sevenzip() -> str:
    for name in _SEVENZIP:
        found = shutil.which(name)
        if found:
            return found
    raise ChmError("解 CHM 需要 7-Zip,本机没有(7zz / 7z / 7za 都找不到)。"
                   "容器镜像应已自带(Debian 包 7zip);本机:macOS `brew install sevenzip`,Debian/Ubuntu `apt install 7zip`。")


def _safe_extract_zip(src: pathlib.Path, dest: pathlib.Path) -> None:
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise ChmError(f"压缩包里有越出解包目录的路径:{info.filename};拒绝解包")
        zf.extractall(dest)


def unpack(src: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    """把文档包解到 dest,返回含 .hhc 的目录。src 已是目录时原样返回。"""
    src = pathlib.Path(src)
    if src.is_dir():
        return find_root(src)
    if not src.is_file():
        raise ChmError(f"文件不存在:{src}")
    dest.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower()
    if suffix == ".zip":
        try:
            _safe_extract_zip(src, dest)
        except zipfile.BadZipFile as exc:
            raise ChmError(f".zip 损坏:{exc}") from exc
    elif suffix == ".chm":
        try:
            proc = subprocess.run([_sevenzip(), "x", "-y", f"-o{dest}", str(src)],
                                  capture_output=True, timeout=_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise ChmError(f"解 CHM 超时({_TIMEOUT}s):{src}") from exc
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise ChmError(f"解 CHM 失败(7-Zip 退出码 {proc.returncode}):{' / '.join(tail)}")
    else:
        raise ChmError(f"不是官方文档包:{src.name}(支持 .chm、含 .hhc 目录文件的 .zip、已解开的目录)")
    return find_root(dest)


def find_root(root: pathlib.Path) -> pathlib.Path:
    hhcs = sorted(pathlib.Path(root).rglob("*.hhc"), key=lambda p: (-p.stat().st_size, str(p)))
    if not hhcs:
        raise ChmError(f"{root} 里没有目录文件(.hhc):不是一份 CHM 结构的文档,无法按目录切分")
    return hhcs[0].parent


_TOKEN = re.compile(r'<UL>|</UL>|<OBJECT type="text/sitemap">(.*?)</OBJECT>', re.S | re.I)
_PARAM = re.compile(r'<param\s+name="(\w+)"\s+value="([^"]*)"', re.I)


def parse_hhc(text: str) -> List[TocNode]:
    """.hhc → 目录节点(按文档顺序)。<UL> 深一层,</UL> 回一层;每个 OBJECT 一个节点。"""
    depth = 0
    path: List[str] = []
    out: List[TocNode] = []
    for m in _TOKEN.finditer(text):
        tok = m.group(0).upper()
        if tok == "<UL>":
            depth += 1
            continue
        if tok == "</UL>":
            depth = max(0, depth - 1)
            continue
        params = {k.lower(): html.unescape(v) for k, v in _PARAM.findall(m.group(1))}
        name = re.sub(r"\s+", " ", params.get("name", "")).strip()
        if not name:
            continue
        path = path[:max(depth - 1, 0)] + [name]
        out.append(TocNode(path=tuple(path), local=params.get("local", "").split("#", 1)[0].strip()))
    return out


def load_toc(root: pathlib.Path) -> List[TocNode]:
    hhc = next(iter(sorted(pathlib.Path(root).glob("*.hhc"), key=lambda p: -p.stat().st_size)), None)
    if hhc is None:
        raise ChmError(f"{root} 里没有 .hhc 目录文件")
    nodes = parse_hhc(htmldoc.decode(hhc.read_bytes()))
    if not nodes:
        raise ChmError(f"目录文件 {hhc.name} 是空的或格式认不出")
    return nodes


def version_strings(root: pathlib.Path, nodes: Sequence[TocNode], sample: int = 400) -> List[Tuple[str, int]]:
    """在前若干页里数内核版本串(如 505.2.1.SPC0800):给 scan 报「这份文档是哪个版本」,不靠文件名猜。"""
    counts: dict = {}
    seen = 0
    for n in nodes:
        if not n.local or seen >= sample:
            continue
        p = pathlib.Path(root) / n.local
        if not p.is_file():
            continue
        seen += 1
        for v in re.findall(r"\b50[0-9]\.\d\.\d(?:\.SPC\d{4})?\b", htmldoc.decode(p.read_bytes())):
            counts[v] = counts.get(v, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


__all__ = ["ChmError", "TocNode", "SUFFIXES", "is_docset_source", "unpack", "find_root", "parse_hhc",
           "load_toc", "version_strings"]
