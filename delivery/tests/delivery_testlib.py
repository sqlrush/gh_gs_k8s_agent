"""delivery 守卫测试的公共部分:定位仓库、载入转化脚本的各模块与交付清单。"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DELIVERY = ROOT / "delivery"


def _load(name: str):
    """按文件载入 delivery/ 的模块,名字加前缀:技能里也有叫 checks 的脚本,全量一起跑时不能串。"""
    spec = importlib.util.spec_from_file_location("delivery_t_" + name, DELIVERY / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


assemble, checks, deps, regyaml, supplement, transform = (
    _load(n) for n in ("assemble", "checks", "deps", "regyaml", "supplement", "transform"))

MANIFEST = assemble.load_manifest(DELIVERY / "manifest.py")
AGENT = ROOT / MANIFEST.SOURCE_ROOT

# 测试里的 git 提交不依赖本机的全局配置
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_NOSYSTEM="1")


def git(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True, env=GIT_ENV)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout


def copy_worktree(dest: pathlib.Path, paths=MANIFEST.EXTRACT) -> None:
    """把工作区里转化脚本会读的部分(已跟踪 + 未忽略的新文件)拷到 dest:测的是当前改动,不是上一个提交。"""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "-co", "--exclude-standard", "--"] + list(paths),
                         capture_output=True, text=True, check=True).stdout     # -z:中文文件名不转义
    for rel in filter(None, out.split("\0")):
        src = ROOT / rel
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def assembled(package: str, out: pathlib.Path) -> list:
    return assemble.assemble(MANIFEST, package, ROOT, out)
