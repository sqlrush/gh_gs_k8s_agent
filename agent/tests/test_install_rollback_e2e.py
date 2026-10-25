"""install-opencode.sh 的快照与回滚 —— 端到端跑真脚本，不做桩。

安装这一步是 `rm -rf` + `cp`。在补快照之前，装上去一个表现不好的版本，
唯一的退路是「翻 git 历史找出上一次装的是哪个提交再装一遍」—— 而那假设
操作的人记得上次装了什么。

回滚这类代码有个共同的坏法：**看起来跑成功了，实际什么都没换**。
所以这里每条都断言**内容真的变了**（用文件里的标记字符串区分版本），
而不只是断言退出码为 0。
"""
import os
import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALLER = _ROOT / "install-opencode.sh"

SKILLS = ("demo-alpha", "demo-beta")


def _make_src(root: pathlib.Path, marker: str) -> pathlib.Path:
    """造一棵最小源码树，SKILL.md 里埋一个版本标记。"""
    src = root / ("src-" + marker)
    for name in SKILLS:
        d = src / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: %s\nversion: 1.0.0\ndescription: d\n---\n\n"
            "MARKER=%s\n路径：{baseDir}/scripts/x.py\n知识库：{kbDir}\n"
            % (name, marker),
            encoding="utf-8")
        (d / "scripts").mkdir()
        (d / "scripts" / "x.py").write_text("# %s\n" % marker, encoding="utf-8")
    (src / "common").mkdir()
    (src / "common" / "access.py").write_text("# %s\n" % marker, encoding="utf-8")
    (src / "scripts" / "registry").mkdir(parents=True)
    (src / "scripts" / "registry" / "t.yaml").write_text("m: %s\n" % marker,
                                                         encoding="utf-8")
    (src / "requirements.txt").write_text("psycopg2\n", encoding="utf-8")
    shutil.copy2(_INSTALLER, src / "install-opencode.sh")
    return src


def _run(src: pathlib.Path, *args, expect_ok=True):
    proc = subprocess.run(
        ["bash", str(src / "install-opencode.sh"), *args],
        capture_output=True, text=True, cwd=str(src))
    if expect_ok:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in (proc.stdout + proc.stderr), proc.stdout + proc.stderr
    return proc


def _marker(dest: pathlib.Path) -> str:
    """读当前装着的是哪个版本。"""
    text = (dest / SKILLS[0] / "SKILL.md").read_text(encoding="utf-8")
    return text.split("MARKER=")[1].split("\n")[0]


def _snapshots(dest: pathlib.Path):
    return sorted(p for p in dest.parent.iterdir()
                  if p.is_dir() and p.name.startswith(dest.name + ".bak."))


@pytest.fixture()
def env(tmp_path):
    dest = tmp_path / "live" / "skills"
    return {"tmp": tmp_path, "dest": dest,
            "v1": _make_src(tmp_path, "v1"),
            "v2": _make_src(tmp_path, "v2")}


# --- 首次安装 ----------------------------------------------------------------

def test_first_install_writes_skills_and_stamp(env):
    _run(env["v1"], "--dest", str(env["dest"]))
    assert _marker(env["dest"]) == "v1"
    assert (env["dest"] / "common" / "access.py").exists()
    assert (env["dest"] / "scripts" / "registry" / "t.yaml").exists()
    stamp = (env["dest"] / ".installed-version").read_text(encoding="utf-8")
    assert "installed:" in stamp and "demo-alpha" in stamp


def test_first_install_makes_no_snapshot(env):
    """空目录没什么可备份的 —— 别造一个空快照，回滚到它等于把 skill 删光。"""
    _run(env["v1"], "--dest", str(env["dest"]))
    assert _snapshots(env["dest"]) == []


def test_placeholders_are_substituted(env):
    _run(env["v1"], "--dest", str(env["dest"]))
    text = (env["dest"] / SKILLS[0] / "SKILL.md").read_text(encoding="utf-8")
    assert "{baseDir}" not in text and "{kbDir}" not in text
    assert str(env["dest"] / SKILLS[0]) in text


# --- 覆盖安装会留下还原点 ----------------------------------------------------

def test_reinstall_snapshots_the_previous_version(env):
    """**快照里必须是「被换掉的那一版」**，不是新装的这一版。

    拷错方向的话快照照样生成、退出码照样是 0，只有真去回滚时才发现
    回滚过去还是坏的那版。
    """
    _run(env["v1"], "--dest", str(env["dest"]))
    _run(env["v2"], "--dest", str(env["dest"]))
    assert _marker(env["dest"]) == "v2"
    snaps = _snapshots(env["dest"])
    assert len(snaps) == 1
    assert _marker(snaps[0]) == "v1"


def test_no_backup_flag_skips_the_snapshot(env):
    _run(env["v1"], "--dest", str(env["dest"]))
    _run(env["v2"], "--dest", str(env["dest"]), "--no-backup")
    assert _snapshots(env["dest"]) == []


def test_two_installs_in_the_same_second_do_not_nest(env):
    """时间戳是秒级的。同一秒内两次安装，第二次的 `cp -R` 会拷进已存在的
    快照目录里，变成 skills.bak.TS/skills/... —— 快照被套一层，安装照样
    报成功，只有回滚时才炸。
    """
    _run(env["v1"], "--dest", str(env["dest"]))
    _run(env["v2"], "--dest", str(env["dest"]))
    _run(env["v1"], "--dest", str(env["dest"]))
    snaps = _snapshots(env["dest"])
    assert len(snaps) == 2, "两次覆盖安装应留下两个快照，实际 %d" % len(snaps)
    for s in snaps:
        assert not (s / env["dest"].name).exists(), "快照 %s 被套了一层" % s
        assert (s / SKILLS[0] / "SKILL.md").exists()


# --- 回滚 --------------------------------------------------------------------

def test_rollback_restores_the_previous_content(env):
    """核心断言：回滚之后**装着的确实是老版本**。"""
    _run(env["v1"], "--dest", str(env["dest"]))
    _run(env["v2"], "--dest", str(env["dest"]))
    assert _marker(env["dest"]) == "v2"

    _run(env["v2"], "--dest", str(env["dest"]), "--rollback")
    assert _marker(env["dest"]) == "v1"
    assert (env["dest"] / "common" / "access.py").read_text() .strip() == "# v1"


def test_rollback_is_itself_reversible(env):
    """回滚回错了还能滚回来 —— 被替换掉的那版也要存成快照。"""
    _run(env["v1"], "--dest", str(env["dest"]))
    _run(env["v2"], "--dest", str(env["dest"]))
    _run(env["v2"], "--dest", str(env["dest"]), "--rollback")
    assert _marker(env["dest"]) == "v1"
    _run(env["v2"], "--dest", str(env["dest"]), "--rollback")
    assert _marker(env["dest"]) == "v2", "第二次回滚应该回到 v2"


def test_rollback_to_a_named_snapshot(env):
    _run(env["v1"], "--dest", str(env["dest"]))
    _run(env["v2"], "--dest", str(env["dest"]))
    snap = _snapshots(env["dest"])[0]
    _run(env["v2"], "--dest", str(env["dest"]), "--rollback", str(snap))
    assert _marker(env["dest"]) == "v1"


def test_rollback_without_any_snapshot_fails_loudly(env):
    """**没有还原点时必须报错。**

    静默返回 0 最糟：操作的人以为坏版本已经撤下去了，实际还在线上跑。
    """
    _run(env["v1"], "--dest", str(env["dest"]))
    proc = _run(env["v1"], "--dest", str(env["dest"]), "--rollback",
                expect_ok=False)
    assert proc.returncode != 0
    assert "no snapshot" in (proc.stdout + proc.stderr)
    assert _marker(env["dest"]) == "v1", "失败的回滚不该动现场"


def test_rollback_refuses_a_directory_without_skills(env):
    """指错目录（比如指到家目录）时拒绝，别把一堆无关文件铺进安装目录。"""
    _run(env["v1"], "--dest", str(env["dest"]))
    junk = env["tmp"] / "junk"
    junk.mkdir()
    (junk / "readme.txt").write_text("x", encoding="utf-8")
    proc = _run(env["v1"], "--dest", str(env["dest"]), "--rollback", str(junk),
                expect_ok=False)
    assert proc.returncode != 0
    assert _marker(env["dest"]) == "v1"


def test_rollback_to_nonexistent_path_fails(env):
    _run(env["v1"], "--dest", str(env["dest"]))
    proc = _run(env["v1"], "--dest", str(env["dest"]), "--rollback",
                str(env["tmp"] / "nope"), expect_ok=False)
    assert proc.returncode != 0


# --- 保留策略 ----------------------------------------------------------------

def test_keep_prunes_oldest_and_spares_unrelated_dirs(env):
    """删除是这里唯一不可逆的一步 —— 只能碰自己命名规则里的目录。"""
    bystander = env["dest"].parent / "skills.bak.NOT-OURS-please-keep"
    _run(env["v1"], "--dest", str(env["dest"]))
    bystander.mkdir(parents=True, exist_ok=True)
    (bystander / "keep.txt").write_text("x", encoding="utf-8")

    for _ in range(4):
        _run(env["v2"], "--dest", str(env["dest"]), "--keep", "2")

    snaps = [s for s in _snapshots(env["dest"]) if s != bystander]
    assert len(snaps) <= 2, "--keep 2 之后还剩 %d 个快照" % len(snaps)
    assert bystander.exists() and (bystander / "keep.txt").exists(), \
        "剪掉了不属于快照命名规则的目录"


# --- dry-run 与 --versions ---------------------------------------------------

def test_dry_run_writes_nothing(env):
    proc = _run(env["v1"], "--dest", str(env["dest"]), "--dry-run")
    assert not env["dest"].exists(), "dry-run 建了目录"
    assert "dry-run" in proc.stdout


def test_dry_run_on_existing_install_changes_nothing(env):
    _run(env["v1"], "--dest", str(env["dest"]))
    _run(env["v2"], "--dest", str(env["dest"]), "--dry-run")
    assert _marker(env["dest"]) == "v1"
    assert _snapshots(env["dest"]) == [], "dry-run 造了快照"


def test_versions_on_empty_dest_does_not_crash(env):
    proc = _run(env["v1"], "--dest", str(env["dest"]), "--versions")
    assert "nothing installed" in proc.stdout


def test_versions_reports_stamp_and_snapshots(env):
    _run(env["v1"], "--dest", str(env["dest"]))
    _run(env["v2"], "--dest", str(env["dest"]))
    proc = _run(env["v2"], "--dest", str(env["dest"]), "--versions")
    assert "demo-alpha" in proc.stdout
    assert ".bak." in proc.stdout


def test_partial_install_is_recorded_as_partial(env):
    """只装一部分 skill 时要写进版本戳 —— 否则快照看起来像一次完整安装，
    回滚过去会缺 skill 而没人知道为什么。"""
    _run(env["v1"], "--dest", str(env["dest"]), "demo-alpha")
    stamp = (env["dest"] / ".installed-version").read_text(encoding="utf-8")
    assert "partial:" in stamp and "demo-alpha" in stamp
    assert not (env["dest"] / "demo-beta").exists()
