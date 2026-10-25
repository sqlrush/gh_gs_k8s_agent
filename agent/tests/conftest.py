"""技能单测的公共设置。

中间件接口路径前缀是部署配置(GRMP_PATH_PREFIX,common/grmp/paths.py),代码里没有默认值。
单测统一用样例前缀 —— 客户端与模拟中间件读同一个变量,两边一致;要测「没配」的用例自己 delenv。
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common.grmp import paths as _grmp_paths  # noqa: E402


@pytest.fixture(autouse=True)
def _grmp_path_prefix(monkeypatch):
    monkeypatch.setenv(_grmp_paths.PREFIX_ENV, _grmp_paths.EXAMPLE_PREFIX)
