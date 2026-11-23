"""转化脚本端到端:临时的测试仓库(当前工作区的拷贝)+ 临时的交付仓库,走一遍真实用法。

预演不写东西 → 首次提交并打标签 → 同一标签再生成不提交 → 新标签只提交变化(补充说明带上一次交付与变更摘要)
→ 新标签再生成仍不提交 → 交付分支被手改时停下。另核对:同一标签两次生成摘要相同、交付包里没有字节码缓存。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))   # 根 pytest.ini 用 importlib 模式
from delivery_testlib import GIT_ENV, MANIFEST, copy_worktree, git, transform  # noqa: E402

M = MANIFEST


def _run(test: pathlib.Path, *args: str):
    proc = subprocess.run([sys.executable, str(test / "delivery" / "transform.py"), "--repo", str(test)] + list(args),
                          capture_output=True, text=True, env=GIT_ENV, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    base = tmp_path_factory.mktemp("dlv")
    test, target = base / "test", base / "delivery-repo"
    copy_worktree(test)
    git(test, "init", "-q")
    git(test, "add", "-A")
    git(test, "commit", "-qm", "feat: 演练起点")
    git(test, "tag", "agent-v9.9.9")
    target.mkdir()
    git(target, "init", "-q")
    git(target, "commit", "-q", "--allow-empty", "-m", "init")
    return test, target


def _meta(target, ref):
    return json.loads(git(target, "show", "%s:.delivery.json" % ref))


def test_out_is_deterministic(repos, tmp_path):
    test, _ = repos
    digests = []
    for i in range(2):
        out = tmp_path / str(i)
        rc, log = _run(test, "--ref", "agent-v9.9.9", "--package", "kbimport", "--out", str(out))
        assert rc == 0, log
        assert not [p for p in out.rglob("*") if p.name == "__pycache__" or p.suffix == ".pyc"]
        meta = json.loads((out / ".delivery.json").read_text(encoding="utf-8"))
        assert meta["digest"] == transform.digest(out)
        digests.append(meta["digest"])
    assert digests[0] == digests[1]
    assert sorted(p.name for p in out.iterdir()) == sorted(M.TOP_LEVEL["kbimport"])


def test_publish_flow(repos):
    test, target = repos
    # 预演:不建分支
    rc, log = _run(test, "--ref", "agent-v9.9.9", "--package", "diag", "--target", str(target), "--dry-run")
    assert rc == 0 and "预演:会在交付分支 diag 上提交 diag-v9.9.9" in log, log
    assert not git(target, "branch", "--list", "diag").strip()

    # 首次提交 + 标签
    rc, log = _run(test, "--ref", "agent-v9.9.9", "--package", "diag", "--target", str(target), "--tag")
    assert rc == 0 and "已在交付分支 diag 上提交" in log, log
    assert git(target, "tag", "--list", "diag-v9.9.9").strip() == "diag-v9.9.9"
    top = sorted(filter(None, git(target, "ls-tree", "-z", "--name-only", "diag").split("\0")))
    assert top == sorted(M.TOP_LEVEL["diag"])
    first = git(target, "rev-parse", "diag").strip()
    meta = _meta(target, "diag")
    assert meta["version"] == "diag-v9.9.9" and meta["base"] is None
    assert "首次交付" in git(target, "show", "diag:补充说明.md")

    # 同一标签再生成:不提交
    rc, log = _run(test, "--ref", "agent-v9.9.9", "--package", "diag", "--target", str(target), "--tag")
    assert rc == 0 and "不提交" in log, log
    assert git(target, "rev-parse", "diag").strip() == first

    # 新的来源提交:只提交变化的文件
    skill = test / "agent" / "skills" / "gaussdb-health" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\n<!-- 演练改动 -->\n", encoding="utf-8")
    git(test, "commit", "-qam", "fix(health): 演练改动")
    git(test, "tag", "agent-v9.9.10")
    rc, log = _run(test, "--ref", "agent-v9.9.10", "--package", "diag", "--target", str(target), "--tag")
    assert rc == 0 and "新增 0、删除 0、修改 2 个文件" in log, log
    note = git(target, "show", "diag:补充说明.md")
    assert "`diag-v9.9.9`(来源 `agent-v9.9.9`)" in note and "fix(health): 演练改动" in note
    assert _meta(target, "diag")["base"]["version"] == "diag-v9.9.9"
    second = git(target, "rev-parse", "diag").strip()

    # 新标签再生成:沿用当时的比对基准,仍然逐字相同
    rc, log = _run(test, "--ref", "agent-v9.9.10", "--package", "diag", "--target", str(target), "--tag")
    assert rc == 0 and "不提交" in log, log
    assert git(target, "rev-parse", "diag").strip() == second

    # 交付分支被手改:停下,不覆盖
    git(target, "checkout", "-q", "diag")
    (target / "AGENTS.md").write_text("手改\n", encoding="utf-8")
    git(target, "commit", "-qam", "手改")
    git(target, "checkout", "-q", "--detach")
    rc, log = _run(test, "--ref", "agent-v9.9.10", "--package", "diag", "--target", str(target))
    assert rc == 2 and "手工改动" in log, log


def test_rejects_unknown_package(repos, tmp_path):
    test, _ = repos
    rc, log = _run(test, "--ref", "agent-v9.9.9", "--package", "nope", "--out", str(tmp_path / "x"))
    assert rc == 2 and "没有这个包" in log
