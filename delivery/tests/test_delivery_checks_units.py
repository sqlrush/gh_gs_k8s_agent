"""装配 + 自检:当前工作区装出的两个包全部检查通过;各项检查能拦住典型问题。"""
from __future__ import annotations

import pathlib
import shutil
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))   # 根 pytest.ini 用 importlib 模式
from delivery_testlib import MANIFEST, ROOT, assembled, checks, supplement  # noqa: E402

M = MANIFEST


@pytest.fixture(scope="module")
def packages(tmp_path_factory):
    out = {}
    for name in M.PACKAGES:
        root = tmp_path_factory.mktemp(name) / "pkg"
        out[name] = (root, assembled(name, root))
    return out


@pytest.fixture()
def diag_copy(packages, tmp_path):
    root, rels = packages["diag"]
    dst = tmp_path / "pkg"
    shutil.copytree(root, dst)
    return dst, rels


@pytest.mark.parametrize("package", sorted(M.PACKAGES))
def test_working_tree_passes_all_checks(packages, package):
    root, rels = packages[package]
    rep = checks.run_all(root, M, package, rels)
    assert not rep.errors, "\n".join(rep.errors)
    assert sorted(p.name for p in root.iterdir()) == sorted(set(M.TOP_LEVEL[package]) - {"补充说明.md", ".delivery.json"})


def test_packages_contents(packages):
    diag, _ = packages["diag"]
    kbi, _ = packages["kbimport"]
    assert (diag / "common" / "grmp" / "client.py").is_file() and not (kbi / "common" / "grmp").exists()
    assert (kbi / "common" / "kb" / "ingest.py").is_file() and not (diag / "common" / "kb" / "ingest.py").exists()
    assert not (kbi / "skills" / "gaussdb-kb-import" / "testdata").exists()
    assert "知识库导入" in (kbi / "AGENTS.md").read_text(encoding="utf-8")
    script = (ROOT / M.ROOTSPEC_SCRIPT[0]).read_bytes()
    assert (diag / "rootspec" / "search_docs.py").read_bytes() == script == (kbi / "rootspec" / "search_docs.py").read_bytes()
    regs = {"%s.%s" % (p.parent.name, p.stem) for p in (diag / "scripts" / "registry").glob("*/*.yaml")}
    assert regs and not any(n.startswith(("perf.", "session.")) for n in regs)


def test_whitelist_matches(packages):
    rep = checks.check_whitelist(packages["diag"][0])
    assert not rep.errors and rep.notes and "逐条一致" in rep.notes[0]


def test_layout_rejects_extra_top_level(diag_copy):
    root, rels = diag_copy
    (root / "tools").mkdir()
    (root / "tools" / "x.py").write_text("", encoding="utf-8")
    errs = checks.check_layout(root, M, "diag", rels).errors
    assert any("多了顶层条目 tools" in e for e in errs)


def test_static_imports_catch_missing_common_file(diag_copy):
    root, _ = diag_copy
    (root / "common" / "kbsection.py").unlink()
    errs = checks.check_imports_static(root).errors
    assert any("common.kbsection" in e for e in errs)


@pytest.mark.parametrize("text,label", [
    ("见 /Users/sqlrush/x", "测试环境的路径"),
    ("连 10.1.2.3 试试", "内网 IP"),
    ("from grmp_middleware import app", "对模拟组件的调用"),
    ("令牌 ghp_" + "a" * 30, "密钥"),
])
def test_content_rejects(diag_copy, text, label):
    root, _ = diag_copy
    p = root / "skills" / "gaussdb-health" / "SKILL.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n" + text + "\n", encoding="utf-8")
    errs = checks.check_content(root).errors
    assert any(label in e and "gaussdb-health/SKILL.md" in e for e in errs), errs


def test_content_rejects_bank_name(diag_copy, monkeypatch):
    """行名只存哈希:用一个示例词的哈希代替,验证中文片段与英文单词两种比对都生效、报错不回显原文。"""
    monkeypatch.setattr(checks, "NAME_SHA256", {checks._sha("示例行"), checks._sha("demobank")})
    root, _ = diag_copy
    p = root / "skills" / "gaussdb-health" / "SKILL.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n这是某示例行的规范\n", encoding="utf-8")
    q = root / "skills" / "gaussdb-config" / "SKILL.md"
    q.write_text(q.read_text(encoding="utf-8") + "\nsee DemoBank docs\n", encoding="utf-8")
    errs = [e for e in checks.check_content(root).errors if "行名" in e]
    assert len(errs) == 2 and not any("示例行" in e or "DemoBank" in e for e in errs), errs


def test_content_allows_documentation_ips(diag_copy):
    root, _ = diag_copy
    p = root / "skills" / "gaussdb-health" / "SKILL.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n例:192.0.2.10、127.0.0.1、198.51.100.7\n", encoding="utf-8")
    assert not [e for e in checks.check_content(root).errors if "gaussdb-health" in e]


def test_whitelist_detects_sql_drift(diag_copy):
    root, _ = diag_copy
    f = sorted((root / "scripts" / "registry").glob("*/*.yaml"))[0]
    f.write_text(f.read_text(encoding="utf-8").replace("SELECT", "SELECT /* 改过 */", 1), encoding="utf-8")
    errs = checks.check_whitelist(root).errors
    assert any("%s.%s" % (f.parent.name, f.stem) in e and "SQL" in e for e in errs), errs


def test_junk_rejected(diag_copy):
    root, _ = diag_copy
    (root / "common" / "__pycache__").mkdir()
    (root / "common" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"")
    assert checks.check_junk(root).errors


def test_dynamic_import_catches_broken_skill(diag_copy):
    root, _ = diag_copy
    (root / "skills" / "gaussdb-config" / "scripts" / "broken.py").write_text("import common.no_such\n", encoding="utf-8")
    errs = checks.check_imports_dynamic(root, ["gaussdb-config"]).errors
    assert any("broken.py" in e for e in errs), errs


def test_supplement_sections(packages):
    root, _ = packages["diag"]
    source = {"repo": M.TEST_REPO, "ref": "agent-v9.9.9", "commit": "0" * 40, "date": "2026-11-05T00:00:00+08:00"}
    text = supplement.render(M, "diag", root, source, "diag-v9.9.9", None, None, None)
    for head in ("## 1. 版本对应", "## 3. 依赖", "## 4. 环境变量", "## 5. SQL 白名单", "## 6. 根规范目录约定", "## 7. 变更摘要"):
        assert head in text
    assert "gaussdb-agent-runtime:agent-v9.9.9-oc%s" % M.OPENCODE_VERSION in text
    assert "GRMP_PATH_PREFIX" in text and "KB_CLIP_ORIGINS" not in text
    kbi = supplement.render(M, "kbimport", packages["kbimport"][0], source, "kbimport-v9.9.9", None, None, None)
    assert "SQL 白名单" not in kbi and "KB_CLIP_ORIGINS" in kbi and "GRMP_PATH_PREFIX" not in kbi
    assert "`rootspec/search_docs.py`:根规范检索脚本" in text and "`rootspec/search_docs.py`:根规范检索脚本" in kbi
