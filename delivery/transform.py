#!/usr/bin/env python3
"""测试仓库 → 交付分支的转化脚本(交付执行人运行;只需要 git + Python 3.9+)。

    # 预演:生成、自检、打印与交付分支的差异,不写任何东西
    python3 delivery/transform.py --ref agent-v1.4.7 --package diag --target ~/gh_gs_k8s_agent --dry-run
    # 正式:生成、自检、在交付仓库的 diag 分支上提交,并打标签 diag-v1.4.7(只在本地,推送命令会打印出来)
    python3 delivery/transform.py --ref agent-v1.4.7 --package diag --target ~/gh_gs_k8s_agent --tag
    # 只生成到一个空目录(看内容、做别的用途)
    python3 delivery/transform.py --ref agent-v1.4.7 --package kbimport --out /tmp/kbimport

做法:用 git archive 取标签里的代码(不读工作区;取哪些路径由清单的 EXTRACT 定),清单、装配、自检、补充说明也都用标签里那一版
(delivery/manifest.py 等)——同一个标签任何时候生成的结果都一样。交付分支只允许本脚本写:分支上有手工改动
(内容与它记录的摘要对不上)就停下,不覆盖。内容没变就不提交。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Dict, List, Optional

META = ".delivery.json"


class Stop(Exception):
    """预期内的停止(自检失败、分支被手改、参数不对):打印原因,退出码 2。"""


def git(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True)
    if proc.returncode != 0:
        raise Stop("git %s 失败:%s" % (" ".join(args[:3]), (proc.stderr or proc.stdout).strip()[:300]))
    return proc.stdout


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location("deliv_%s" % name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def digest(root: pathlib.Path) -> str:
    """交付内容的摘要:文件路径 + 是否可执行 + 内容哈希(不含 .git 与 .delivery.json)。"""
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file() and ".git" not in x.relative_to(root).parts):
        rel = p.relative_to(root).as_posix()
        if rel == META:
            continue
        mode = "x" if p.stat().st_mode & 0o111 else "-"
        h.update(("%s\t%s\t%s\n" % (rel, mode, hashlib.sha256(p.read_bytes()).hexdigest())).encode("utf-8"))
    return h.hexdigest()


def extract(repo: pathlib.Path, commit: str, dest: pathlib.Path, paths: List[str]) -> None:
    for p in paths:
        if not git(repo, "ls-tree", "--name-only", commit, p).strip():
            if p == "delivery":
                raise Stop("%s 里没有 delivery/:这个提交早于转化脚本(agent-v1.4.7 起才有交付清单)" % commit[:10])
            raise Stop("%s 里没有交付清单要取的 %s" % (commit[:10], p))
    proc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit] + paths, capture_output=True)
    if proc.returncode != 0:
        raise Stop("git archive 失败:%s" % proc.stderr.decode("utf-8", "replace")[:300])
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
        if hasattr(tarfile, "data_filter"):      # 3.12+:只解普通文件与目录,不跟链接出界
            tf.extractall(dest, filter="data")
        else:
            tf.extractall(dest)


def precheck(src: pathlib.Path, manifest) -> List[str]:
    """装配前在完整源码上核对注入是否一致。缺第三方库跑不了的,记为提示。"""
    notes, agent = [], src / manifest.SOURCE_ROOT
    for label, cmd in manifest.PRECHECKS:
        args = [sys.executable, "-I"] + [c.replace("{agent}", str(agent)) for c in cmd]
        proc = subprocess.run(args, cwd=agent, capture_output=True, text=True)
        out = (proc.stdout + proc.stderr).strip()
        # 缺第三方库:Python 自己的报错,或脚本自己的提示(kb.py 写「缺少依赖 PyYAML」)
        miss = re.search(r"ModuleNotFoundError: No module named '([^'.]+)|缺少依赖 ([A-Za-z0-9_.-]+)", out)
        if proc.returncode != 0 and miss:
            notes.append("%s:本机缺 %s,没能核对" % (label, miss.group(1) or miss.group(2)))
        elif proc.returncode != 0:
            raise Stop("%s不一致(测试仓库里先修好再生成):\n%s" % (label, out[-800:]))
    return notes


def previous(target: pathlib.Path, branch: str) -> Optional[Dict]:
    if not git(target, "branch", "--list", branch).strip():
        return None
    raw = subprocess.run(["git", "-C", str(target), "show", "%s:%s" % (branch, META)], capture_output=True, text=True)
    if raw.returncode != 0:
        raise Stop("交付分支 %s 上没有 %s:不是本脚本生成的分支,不往上写" % (branch, META))
    return json.loads(raw.stdout)


def source_paths(pairs, repo: pathlib.Path) -> List[str]:
    return sorted({str(src.relative_to(repo)) for src, _ in pairs})


def history(repo: pathlib.Path, prev: Optional[Dict], commit: str, paths: List[str]) -> Optional[List[str]]:
    if not prev:
        return None
    out = git(repo, "log", "--no-merges", "--format=%h %s", "%s..%s" % (prev["source"]["commit"], commit), "--", *paths)
    return [l for l in out.splitlines() if l.strip()]


def whitelist_diff(repo: pathlib.Path, prev: Optional[Dict], commit: str, checks, wl_dir: str) -> Optional[Dict]:
    if not prev:
        return None
    old = checks._dml_rows(git(repo, "show", "%s:%s/scripts.sql" % (prev["source"]["commit"], wl_dir)))
    new = checks._dml_rows(git(repo, "show", "%s:%s/scripts.sql" % (commit, wl_dir)))
    return {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
            "changed": sorted(k for k in set(old) & set(new) if old[k] != new[k])}


def build(repo: pathlib.Path, ref: str, package: str, work: pathlib.Path, prev: Optional[Dict]):
    commit = git(repo, "rev-parse", "%s^{commit}" % ref).strip()
    date = git(repo, "show", "-s", "--format=%cI", commit).strip()
    # 比对的基准:上一次交付。对同一个来源提交重新生成时沿用当时记下的基准,结果才逐字相同(否则「上一次」就成了它自己)
    base = prev.get("base") if prev and prev["source"]["commit"] == commit else prev
    src = work / "src"
    src.mkdir()
    extract(repo, commit, src, ["delivery"])            # 先取清单,再按清单取其余路径
    d = src / "delivery"
    sys.path.insert(0, str(d))
    manifest = _load("manifest", d / "manifest.py")
    extract(repo, commit, src, [p for p in getattr(manifest, "EXTRACT", ("agent", "docs/delivery")) if p != "delivery"])
    assemble, checks, supplement = (_load(n, d / (n + ".py")) for n in ("assemble", "checks", "supplement"))
    if package not in manifest.PACKAGES:
        raise Stop("没有这个包:%s(清单里有 %s)" % (package, "、".join(manifest.PACKAGES)))
    notes = precheck(src, manifest)
    out = work / "out"
    try:
        pairs = assemble.select(manifest, package, src)
        rels = assemble.assemble(manifest, package, src, out)
    except (OSError, ValueError) as exc:        # 清单与代码对不上:归属缺失、技能或白名单文件不存在
        raise Stop("装配失败:%s" % exc)
    rep = checks.run_all(out, manifest, package, rels)
    if rep.errors:
        raise Stop("自检没通过(%d 项):\n  " % len(rep.errors) + "\n  ".join(rep.errors[:40]))
    m = re.fullmatch(r"agent-(v[\d.]+)", ref)
    version = "%s-%s" % (package, m.group(1) if m else commit[:10])
    source = {"repo": manifest.TEST_REPO, "ref": ref, "commit": commit, "date": date}
    paths = source_paths(pairs, src)
    wl = whitelist_diff(repo, base, commit, checks, manifest.WHITELIST_DIR) if manifest.PACKAGES[package]["whitelist"] else None
    text = supplement.render(manifest, package, out, source, version, wl, history(repo, base, commit, paths),
                             {"version": base["version"], "ref": base["source"]["ref"]} if base else None)
    (out / "补充说明.md").write_text(text, encoding="utf-8")
    meta = {"format": 1, "package": package, "version": version, "source": source,
            "base": {"version": base["version"], "source": base["source"]} if base else None,
            "files": len(rels) + 1, "digest": digest(out)}
    (out / META).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out, meta, notes + rep.notes


def diff_summary(old: Optional[pathlib.Path], new: pathlib.Path) -> List[str]:
    def files(root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(root).parts and p.name != META} if root else {}
    a, b = files(old), files(new)
    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    out = ["新增 %d、删除 %d、修改 %d 个文件" % (len(added), len(removed), len(changed))]
    for label, items in (("新增", added), ("删除", removed), ("修改", changed)):
        out += ["  %s %s" % (label, x) for x in items[:30]] + (["  %s …(另有 %d 个)" % (label, len(items) - 30)] if len(items) > 30 else [])
    return out


def _worktree(target: pathlib.Path, branch: str, exists: bool, wt: pathlib.Path) -> None:
    if exists:
        git(target, "worktree", "add", "--quiet", str(wt), branch)
    else:
        git(target, "worktree", "add", "--quiet", "--detach", str(wt))
        git(wt, "checkout", "--quiet", "--orphan", branch)
        git(wt, "rm", "-rfq", "--ignore-unmatch", ".")


def publish(target: pathlib.Path, branch: str, out: pathlib.Path, meta: Dict, prev: Optional[Dict],
            tag: bool, dry_run: bool) -> List[str]:
    wt = pathlib.Path(tempfile.mkdtemp(prefix="delivery-wt-"))
    wt.rmdir()
    _worktree(target, branch, prev is not None, wt)
    try:
        if prev is not None and digest(wt) != prev.get("digest"):
            raise Stop("交付分支 %s 上有手工改动(内容与 %s 记录的摘要对不上),不覆盖。请先查清是谁改的、"
                       "需要的改动挪回测试仓库,再把分支恢复到上一次生成的提交。" % (branch, META))
        lines = diff_summary(wt if prev else None, out)
        if prev is not None and prev.get("digest") == meta["digest"]:
            return ["内容与交付分支 %s 现有的完全一样(来源 %s),不提交。" % (branch, prev["source"]["ref"])] + lines
        if dry_run:
            return ["预演:会在交付分支 %s 上提交 %s。" % (branch, meta["version"])] + lines
        for p in wt.iterdir():
            if p.name != ".git":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copytree(out, wt, dirs_exist_ok=True)
        git(wt, "add", "-A")
        msg = "chore(delivery): %s ← %s %s(%s)\n\n由 delivery/transform.py 自动生成,不要手工修改。\n文件 %d 个;摘要 %s\n" % (
            meta["version"], meta["source"]["repo"], meta["source"]["ref"], meta["source"]["commit"][:10],
            meta["files"], meta["digest"][:16])
        git(wt, "commit", "--quiet", "-m", msg)
        if tag:
            git(wt, "tag", "-a", meta["version"], "-m", msg.splitlines()[0])
        head = git(wt, "rev-parse", "--short", "HEAD").strip()
        push = "git -C %s push origin %s%s" % (target, branch, (" " + meta["version"]) if tag else "")
        return ["已在交付分支 %s 上提交 %s%s。推送:%s" % (branch, head, ",标签 " + meta["version"] if tag else "", push)] + lines
    finally:
        git(target, "worktree", "remove", "--force", str(wt))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="测试仓库 → 交付分支")
    ap.add_argument("--ref", required=True, help="测试仓库的标签或提交,如 agent-v1.4.7")
    ap.add_argument("--package", required=True, help="diag(诊断技能包)或 kbimport(知识库导入技能包)")
    ap.add_argument("--repo", default=str(pathlib.Path(__file__).resolve().parents[1]), help="测试仓库(默认本脚本所在仓库)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", help="只生成到这个目录(不存在或为空)")
    g.add_argument("--target", help="交付仓库的本地克隆(联合仓库 gh_gs_k8s_agent)")
    ap.add_argument("--branch", help="交付分支名,默认与包名相同")
    ap.add_argument("--tag", action="store_true", help="提交后打标签(如 diag-v1.4.7)")
    ap.add_argument("--dry-run", action="store_true", help="预演:不写任何东西")
    a = ap.parse_args(argv)
    repo = pathlib.Path(a.repo).expanduser().resolve()
    try:
        target = pathlib.Path(a.target).expanduser().resolve() if a.target else None
        branch = a.branch or a.package
        prev = previous(target, branch) if target else None
        with tempfile.TemporaryDirectory(prefix="delivery-") as tmp:
            out, meta, notes = build(repo, a.ref, a.package, pathlib.Path(tmp), prev)
            print("生成 %s:%d 个文件,摘要 %s" % (meta["version"], meta["files"], meta["digest"][:16]))
            for n in notes:
                print("  提示:" + n)
            if target is None:
                dest = pathlib.Path(a.out).expanduser()
                if dest.exists() and any(dest.iterdir()):
                    raise Stop("输出目录不是空的:%s" % dest)
                if not a.dry_run:
                    shutil.copytree(out, dest, dirs_exist_ok=True)
                    print("已写到 %s" % dest)
                return 0
            for line in publish(target, branch, out, meta, prev, a.tag, a.dry_run):
                print(line)
        return 0
    except Stop as exc:
        print("停止:%s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
