"""regyaml(只用标准库的白名单定义解析器)与 PyYAML 结果一致;认不出的写法报错,不猜。"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))   # 根 pytest.ini 用 importlib 模式
from delivery_testlib import AGENT, regyaml  # noqa: E402

yaml = pytest.importorskip("yaml")

FILES = sorted((AGENT / "scripts" / "registry").glob("*/*.yaml"))


def test_registry_present():
    assert len(FILES) > 100


@pytest.mark.parametrize("path", FILES, ids=lambda p: "%s/%s" % (p.parent.name, p.stem))
def test_same_as_pyyaml(path):
    assert regyaml.load(path) == yaml.safe_load(path.read_text(encoding="utf-8"))


def test_block_and_params():
    text = ("name: a.b\ndescription: '说明 ''引号'''\nreadonly: true\nsql: |\n  SELECT 1\n\n    FROM t\n"
            "params:\n  - key: n\n    type: integer  # 注释\n    description: 条数\n")
    want = yaml.safe_load(text)
    assert regyaml.parse(text) == want
    assert want["sql"] == "SELECT 1\n\n  FROM t\n"


@pytest.mark.parametrize("text", [
    "name: a.b\nsql: >\n  SELECT 1\n",                  # 折叠块
    "name: a.b\nsql: |\n  SELECT 1\nx: [1, 2]\n",        # 流式列表
    "name: a.b\nsql: |\n  SELECT 1\nx: &anchor 1\n",     # 锚点
    "name: a.b\nsql: |\n  SELECT 1\nparams:\n  key: n\n",  # params 不是列表
    "name: 'a.b\nsql: |\n  SELECT 1\n",                  # 引号没闭合
    "name: a.b\n",                                       # 缺 sql
    "  name: a.b\nsql: |\n  SELECT 1\n",                 # 顶层缩进
])
def test_unknown_constructs_rejected(text):
    with pytest.raises(regyaml.FormatError):
        regyaml.parse(text)
