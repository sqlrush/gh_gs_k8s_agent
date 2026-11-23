"""按交付清单把一个包的文件从测试仓库挑出来,放进输出目录。只挑,不改内容。

交付包的布局(与 install-opencode.sh 装好后的布局一致,common 能被技能脚本按上级目录找到):
  AGENTS.md                  根提示词(导入包由 agent/AGENTS-kb-import.md 改名)
  common/                    清单里 shared + 本包的文件
  skills/<技能>/              本包技能的全部文件(testdata 等测试数据除外)
  scripts/registry/<ns>/     诊断包:技能实际调用的白名单脚本定义(运行时按它执行)
  whitelist/                 诊断包:给中间件团队的 DML(scripts.sql、历次增量、清单)
  rootspec/search_docs.py    根规范检索脚本(清单的 extra)
  补充说明.md、.delivery.json  由 supplement.py / transform.py 生成
"""
from __future__ import annotations

import fnmatch
import importlib.util
import pathlib
import shutil
from typing import Dict, List, Tuple

Pair = Tuple[pathlib.Path, str]          # (源文件, 交付包里的相对路径)


def load_manifest(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location("delivery_manifest_%d" % abs(hash(str(path))), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if getattr(mod, "FORMAT", None) != 1:
        raise ValueError("交付清单格式版本 %r 与转化脚本(1)不符:%s" % (getattr(mod, "FORMAT", None), path))
    return mod


def _common_files(agent: pathlib.Path) -> List[str]:
    return sorted(str(p.relative_to(agent / "common")) for p in (agent / "common").rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"))


def common_owner(manifest, agent: pathlib.Path) -> Dict[str, List[str]]:
    """文件 → 归属;一个文件对应多个归属或没有归属都原样返回,由守卫测试和自检报错。"""
    owners: Dict[str, List[str]] = {}
    for rel in _common_files(agent):
        for cat, pats in manifest.COMMON.items():
            if any(fnmatch.fnmatchcase(rel, p) if "*" in p else rel == p for p in pats):
                owners.setdefault(rel, []).append(cat)
        owners.setdefault(rel, [])
    return owners


def _skill_files(agent: pathlib.Path, skill: str, exclude) -> List[pathlib.Path]:
    base = agent / "skills" / skill
    if not (base / "SKILL.md").is_file():
        raise FileNotFoundError("清单里的技能 %s 不存在或没有 SKILL.md" % skill)
    out = []
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).parts
        if p.is_file() and not any(part in exclude for part in rel) and not p.name.endswith(".pyc"):
            out.append(p)
    return out


def referenced_scripts(agent: pathlib.Path, skills) -> List[str]:
    """交付技能实际调用的白名单脚本:脚本全名出现在这些技能的 .py 里(与 grmp_register 的交付闸门同一判据)。"""
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for s in skills for p in (agent / "skills" / s).rglob("*.py") if "__pycache__" not in p.parts)
    names = ["%s.%s" % (f.parent.name, f.stem) for f in sorted((agent / "scripts" / "registry").glob("*/*.yaml"))]
    return [n for n in names if n in text]


def select(manifest, package: str, repo: pathlib.Path) -> List[Pair]:
    pkg = manifest.PACKAGES[package]
    agent = repo / manifest.SOURCE_ROOT
    pairs: List[Pair] = [(agent / pkg["agents_md"], "AGENTS.md")]
    owners = common_owner(manifest, agent)
    for rel, cats in owners.items():
        if len(cats) != 1:
            raise ValueError("common/%s 的归属是 %s,应恰好一个(交付清单 COMMON)" % (rel, cats or "空"))
        if cats[0] in ("shared", package):
            pairs.append((agent / "common" / rel, "common/" + rel))
    for skill in pkg["skills"]:
        base = agent / "skills" / skill
        for p in _skill_files(agent, skill, manifest.SKILL_EXCLUDE_DIRS):
            pairs.append((p, "skills/%s/%s" % (skill, p.relative_to(base).as_posix())))
    if pkg["whitelist"]:
        for name in referenced_scripts(agent, pkg["skills"]):
            ns, short = name.split(".", 1)
            pairs.append((agent / "scripts" / "registry" / ns / (short + ".yaml"), "scripts/registry/%s/%s.yaml" % (ns, short)))
        wl = repo / manifest.WHITELIST_DIR
        for p in sorted(wl.glob("scripts*.sql")) + [wl / "白名单脚本清单.md"]:
            if not p.is_file():
                raise FileNotFoundError("白名单交付文件缺失:%s" % p)
            pairs.append((p, "whitelist/" + p.name))
    for src, dst, _ in pkg.get("extra", ()):
        if not (repo / src).is_file():
            raise FileNotFoundError("清单 extra 里的文件不存在:%s" % src)
        pairs.append((repo / src, dst))
    return pairs


def assemble(manifest, package: str, repo: pathlib.Path, out: pathlib.Path) -> List[str]:
    """把 package 的文件拷进 out(out 必须不存在或为空)。返回交付包里的相对路径(已排序)。"""
    if out.exists() and any(out.iterdir()):
        raise FileExistsError("输出目录不是空的:%s" % out)
    out.mkdir(parents=True, exist_ok=True)
    rels = []
    for src, rel in select(manifest, package, repo):
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if src.stat().st_mode & 0o111:
            dst.chmod(0o755)
        rels.append(rel)
    return sorted(rels)
