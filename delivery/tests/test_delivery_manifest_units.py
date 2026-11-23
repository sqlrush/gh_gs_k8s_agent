"""交付清单的守卫:common 归属、依赖闭包、技能覆盖、依赖与环境变量登记、与 Dockerfile / requirements 一致。"""
from __future__ import annotations

import fnmatch
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))   # 根 pytest.ini 用 importlib 模式
from delivery_testlib import AGENT, MANIFEST, ROOT, assemble, deps  # noqa: E402

M = MANIFEST


def _mods_of(categories):
    """归属在 categories 里的 common 模块名。"""
    owners = assemble.common_owner(M, AGENT)
    return {deps.module_name(pathlib.PurePosixPath("common", rel))
            for rel, cats in owners.items() if rel.endswith(".py") and cats and cats[0] in categories}


def test_every_common_file_has_exactly_one_owner():
    bad = {rel: cats for rel, cats in assemble.common_owner(M, AGENT).items() if len(cats) != 1}
    assert not bad, "common 文件的归属应恰好一个(在 delivery/manifest.py 的 COMMON 里登记):%s" % bad


def test_manifest_has_no_stale_common_entries():
    files = list(assemble.common_owner(M, AGENT))
    stale = [p for pats in M.COMMON.values() for p in pats
             if not any(fnmatch.fnmatchcase(f, p) if "*" in p else f == p for f in files)]
    assert not stale, "清单里登记了不存在的 common 文件:%s" % stale


def test_shared_modules_only_depend_on_shared():
    mods = deps.common_modules(AGENT)
    shared = _mods_of(("shared",))
    leak = {m: sorted(deps.closure([m], AGENT, mods) - shared) for m in sorted(shared)}
    leak = {k: v for k, v in leak.items() if v}
    assert not leak, "shared 模块依赖了只属于某个包的模块(要么挪进 shared,要么去掉依赖):%s" % leak


@pytest.mark.parametrize("package", sorted(M.PACKAGES))
def test_skill_dependencies_stay_inside_package(package):
    mods = deps.common_modules(AGENT)
    allowed = _mods_of(("shared", package))
    bad = {}
    for skill in M.PACKAGES[package]["skills"]:
        extra = deps.skill_closure(AGENT, skill, mods) - allowed
        if extra:
            bad[skill] = sorted(extra)
    assert not bad, "%s 包的技能用到了包外的 common 模块:%s" % (package, bad)


def test_every_skill_is_delivered():
    skills = {p.name for p in (AGENT / "skills").iterdir() if (p / "SKILL.md").is_file()}
    delivered = {s for pkg in M.PACKAGES.values() for s in pkg["skills"]}
    assert skills == delivered, "没进任何交付包的技能 %s;清单里有但仓库没有的 %s" % (
        sorted(skills - delivered), sorted(delivered - skills))


def test_sql_py_locator_only_in_diag():
    """13 个诊断技能按「common/sql.py 存在」找 common 目录:这些技能只能在带 sql.py 的诊断包里。"""
    assert "sql.py" in M.COMMON["diag"]
    users = sorted({p.relative_to(AGENT / "skills").parts[0] for p in (AGENT / "skills").rglob("*.py")
                    if '"sql.py"' in p.read_text(encoding="utf-8")})
    assert users and not set(users) - set(M.DIAG_SKILLS) and not set(users) & set(M.KBIMPORT_SKILLS)


def test_excluded_skill_dirs_not_read_at_runtime():
    """技能里不进交付的目录(testdata 等),运行时代码与提示词不能引用。"""
    hits = []
    for skill in sorted({s for pkg in M.PACKAGES.values() for s in pkg["skills"]}):
        for p in assemble._skill_files(AGENT, skill, M.SKILL_EXCLUDE_DIRS):
            if p.suffix in (".py", ".md", ".sh", ".yaml") and re.search(r"\btestdata\b", p.read_text(encoding="utf-8")):
                hits.append(str(p.relative_to(AGENT)))
    assert not hits


def test_layout_constants_consistent():
    for name, pkg in M.PACKAGES.items():
        top = set(M.TOP_LEVEL[name])
        assert {"AGENTS.md", "common", "skills", "补充说明.md", ".delivery.json"} <= top
        assert ({"scripts", "whitelist"} <= top) == pkg["whitelist"]
        assert (AGENT / pkg["agents_md"]).is_file()
    for _, cmd in M.PRECHECKS:
        assert (AGENT / cmd[0]).is_file(), cmd


# ---------------------------------------------------------------- 依赖与环境变量

_ENV_NAME = re.compile(r"""["']((?:GSDB|GDAA|GRMP|KB|OPENCODE|ROOTSPEC|G)_[A-Z0-9_]+)["']""")
_ENV_REF = re.compile(r"\$\{?((?:GSDB|GDAA|GRMP|KB|OPENCODE|ROOTSPEC)_[A-Z0-9_]+)")     # 提示词与技能文档里写的 $变量


def _env_names_in(package, kinds=(".py", ".sh", ".md")):
    names = set()
    for src, _ in assemble.select(M, package, ROOT):
        if src.suffix not in kinds:
            continue
        text = src.read_text(encoding="utf-8")
        names |= set(_ENV_REF.findall(text) if src.suffix == ".md" else _ENV_NAME.findall(text))
    return names


@pytest.mark.parametrize("package", sorted(M.PACKAGES))
def test_env_vars_documented(package):
    """交付代码里出现的变量名、提示词与技能文档里写的 $变量,都在清单 ENV 里登记(补充说明照它生成);
    登记给本包的变量本包确实用到。"""
    found = _env_names_in(package)
    assert not found - set(M.ENV), "%s 包代码里有没登记的环境变量:%s" % (package, sorted(found - set(M.ENV)))
    claimed = {k for k, v in M.ENV.items() if package in v[0]}
    assert not claimed - found, "清单说 %s 包读这些变量,代码里却没有:%s" % (package, sorted(claimed - found))
    # 提示词与技能文档里让模型写的 $变量,本包部署时一定要有:必须登记在本包名下
    prompts = _env_names_in(package, (".md",))
    assert not prompts - claimed, "%s 包的提示词里写了没登记给本包的变量:%s" % (package, sorted(prompts - claimed))


@pytest.mark.parametrize("package", sorted(M.PACKAGES))
def test_rootspec_script_delivered_when_prompt_uses_it(package):
    """根提示词执行 $GSDB_ROOTSPEC_HOME/search_docs.py:用到它的包必须带上检索脚本。"""
    pkg = M.PACKAGES[package]
    prompt = (AGENT / pkg["agents_md"]).read_text(encoding="utf-8")
    extras = {dst for _, dst, _ in pkg.get("extra", ())}
    assert ("search_docs.py" in prompt) == ("rootspec/search_docs.py" in extras)
    for src, _, desc in pkg.get("extra", ()):
        assert (ROOT / src).is_file() and desc
        assert any(src == p or src.startswith(p + "/") for p in M.EXTRACT), "%s 不在 EXTRACT 里,转化脚本取不到" % src


def test_env_entries_well_formed():
    for k, (pkgs, required, desc) in M.ENV.items():
        assert pkgs and set(pkgs) <= set(M.PACKAGES), k
        assert required and desc, k


def test_python_deps_match_dockerfile_and_requirements():
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG OPENCODE_VERSION=%s\n" % M.OPENCODE_VERSION in dockerfile
    for name, (spec, _, pkgs) in M.PY_DEPS.items():
        assert '"%s%s"' % (name, spec) in dockerfile, "Dockerfile 里没有 %s%s" % (name, spec)
        assert pkgs and set(pkgs) <= set(M.PACKAGES)
    for line in (AGENT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*([=<>!~]=?[^\s#]+)", line)
        if not m:
            continue
        name = "psycopg2-binary" if m.group(1) == "psycopg2" else m.group(1)
        assert name in M.PY_DEPS and M.PY_DEPS[name][0] == m.group(2), line
        assert set(M.PY_DEPS[name][2]) == set(M.PACKAGES), "requirements.txt 里的依赖两个包都要:%s" % name


def test_system_deps_match_dockerfile():
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    for pkg in ("ripgrep", "7zip", "poppler-utils"):
        assert re.search(r"apt-get install[^\n]*\b%s\b" % re.escape(pkg), dockerfile), pkg
