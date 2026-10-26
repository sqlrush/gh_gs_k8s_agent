"""《代码结构说明》(docs/代码结构说明.md)的守卫:后端每个文件在说明里都有一行,说明里列的文件都真实存在。

加、删、改名文件而没有同步说明,这里就红 —— 说明是给两方(含各自的编码智能体)找代码、改代码用的,过时的说明比没有更糟。
规则与说明开头写的一致:标题里的 `路径/` 是这一节的目录;表头第一列是「文件」的表格,每行第一列是相对这个目录的文件名。

只要求后端目录(agent/ docker/ gateway/ k8s/ scripts/)逐个列全;样例知识库(gaussdb-kb-import/testdata/)是测试数据,
在说明里按目录整体介绍。前端目录(frontend/)由前端方维护,不在这里管。
"""
import pathlib
import re
import subprocess

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DOC = _ROOT / "docs" / "代码结构说明.md"
_BACKEND = ("agent/", "docker/", "gateway/", "k8s/", "scripts/")
_DATA = ("agent/skills/gaussdb-kb-import/testdata/",)


def _tracked():
    out = subprocess.run(["git", "-C", str(_ROOT), "-c", "core.quotepath=off", "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return {f for f in out.splitlines() if f}


def _listed():
    """说明里的文件表 → 仓库相对路径的集合。"""
    files, cur, in_table = set(), None, False
    for line in _DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            dirs = [d for d in re.findall(r"`([^`]+)`", line) if d.endswith("/")]
            if dirs:
                cur = "" if dirs[-1] == "./" else dirs[-1]
            in_table = False
            continue
        if line.startswith("| 文件 |"):
            in_table = True
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        if in_table and cur is not None:
            m = re.match(r"\|\s*`([^`]+)`\s*\|", line)
            if m:
                files.add(cur + m.group(1))
    return files


def test_every_backend_file_is_described():
    want = {f for f in _tracked() if f.startswith(_BACKEND) and not f.startswith(_DATA)}
    missing = sorted(want - _listed())
    assert not missing, "这些文件在 docs/代码结构说明.md 里没有说明(在对应目录的表里加一行):\n  " + "\n  ".join(missing)


def test_every_described_file_exists():
    stale = sorted(f for f in _listed() if not (_ROOT / f).exists())
    assert not stale, "docs/代码结构说明.md 列了已经不存在的文件(删掉或改名):\n  " + "\n  ".join(stale)


def test_the_guard_actually_parses_the_doc():
    """守卫本身要能红:解析出来的表要覆盖几个已知文件,不能因为格式变了解析成空集而永远绿。"""
    listed = _listed()
    for f in ("gateway/server.py", "agent/common/grmp/paths.py", "agent/scripts/registry/health/overview.yaml",
              "agent/skills/gaussdb-health/SKILL.md", "docker/credproxy.py", "README.md"):
        assert f in listed, f
    assert len(listed) > 500
