"""知识库导入的收件目录,与「文件不在沙箱里」时给用户的说明。

现场(客户 2026-09-08 晚截图):用户说「文件在 D:\\AI\\…md,导入知识库」。skill 跑在 Linux 沙箱里,够不到用户电脑上的文件,
上传这件事(浏览器选文件 → 平台后端 → 写进沙箱目录)全在客户平台手里,skill 只能站在终点等文件落地。
所以脚本要做的是把终点说清楚:本机路径直接说要上传到哪个目录、命令怎么写;并按文件名在沙箱常见目录里找一遍
同名文件——前端可能已经传上来了,用户只是照着自己电脑上的路径说。

收件目录:$GSDB_KB_INBOX,没设就是 <kb>/inbox/uploads(平台把上传落到别处时用环境变量指过去)。
"""
from __future__ import annotations

import os
import pathlib
import re
from typing import List, Optional, Sequence

ENV_INBOX = "GSDB_KB_INBOX"

# 盘符 / UNC / ~ 开头 / macOS 用户目录 / Linux 桌面目录:都是「用户自己电脑上」的形状。
_LOCAL_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/Users/|/home/[^/]+/(?:Desktop|Downloads|Documents|桌面|下载|文档)/)")


def looks_local(text: str) -> bool:
    t = (text or "").strip()
    return bool(_LOCAL_RE.match(t)) or "\\" in t


def basename(text: str) -> str:
    return re.split(r"[\\/]", (text or "").strip())[-1]


def inbox_dir(kb: pathlib.Path) -> pathlib.Path:
    env = os.environ.get(ENV_INBOX)
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path(kb) / "inbox" / "uploads"


def candidate_dirs(kb: pathlib.Path, cwd: Optional[pathlib.Path] = None) -> List[pathlib.Path]:
    cwd = pathlib.Path(cwd or os.getcwd())
    out: List[pathlib.Path] = []
    for d in (inbox_dir(kb), cwd, cwd / "docs", pathlib.Path("/workspace/docs"), pathlib.Path(kb) / "inbox"):
        if d not in out:
            out.append(d)
    return out


def find_same_name(name: str, dirs: Sequence[pathlib.Path]) -> List[pathlib.Path]:
    found: List[pathlib.Path] = []
    if not name:
        return found
    for d in dirs:
        p = pathlib.Path(d) / name
        if p.is_file() and p not in found:
            found.append(p)
    return found


def missing_file_message(path_text: str, kb: pathlib.Path, cwd: Optional[pathlib.Path] = None) -> str:
    name = basename(path_text)
    box = inbox_dir(kb)
    lines = ["文件不存在:%s" % path_text]
    if looks_local(path_text):
        lines.append("本 skill 运行在沙箱里,只能读沙箱内的文件;这是你本机上的路径,沙箱访问不到。")
        lines.append("请先把文件上传到沙箱的收件目录 %s(用对话界面的上传功能,或请平台管理员放到该目录),再用沙箱内路径重跑:" % box)
    else:
        lines.append("沙箱里没有这个路径。知识库的收件目录是 %s,上传的文件放到这里后用沙箱内路径重跑:" % box)
    lines.append("  python3 kb.py ingest %s" % (box / name))
    same = find_same_name(name, candidate_dirs(kb, cwd))
    if same:
        lines.append("沙箱里找到同名文件(如果就是这一份,直接用这个路径导入):")
        lines += ["  - %s" % p for p in same]
    return "\n".join(lines)


__all__ = ["ENV_INBOX", "looks_local", "basename", "inbox_dir", "candidate_dirs", "find_same_name", "missing_file_message"]
