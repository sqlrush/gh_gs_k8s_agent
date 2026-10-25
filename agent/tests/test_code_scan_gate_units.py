"""客户代码扫描(SonarQube 规则集,CERT 规范)会扫我们的 Python 源码——这里把已知会被报的写法钉成闸门。

现场(客户 2026-09-09 截图):S1764「Correct one of the identical sub-expressions on both sides of operator "!="」
报在 calibrate.py / costmodel.py / selectivity.py 的 `x != x` 判 NaN 写法上。写法本身没错(NaN 是唯一不等于自己的值),
但扫描器不认,而且 `math.isnan` 本来就更清楚。行为不能变:NaN 该抛的照抛、该退化的照退化。
"""
import math
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))

_SHIPPED = ("skills", "common", "grmp_middleware")
# 左操作数前面不能是 . 或字母(c.app == app 不算),右操作数后面不能接 . ( [ 或字母(schema == schema.lower() 不算)
_SELF_CMP = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*(?:!=|==)\s*\1(?![\w.(\[])")


def _code_lines(path: pathlib.Path):
    """去掉注释与字符串字面量后的每一行(粗糙但够用:这类比较不会写在字符串里)。"""
    for no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        code = re.sub(r"(\"\"\"|''').*?\1", "", line)
        code = re.sub(r"\"[^\"]*\"|'[^']*'", "''", code).split("#", 1)[0]
        yield no, code


def test_no_self_comparison_in_shipped_python():
    """S1764:`x != x` / `x == x` 一律不许出现在交付的 Python 里;判 NaN 用 math.isnan。"""
    hits = []
    for top in _SHIPPED:
        for path in sorted((_ROOT / top).rglob("*.py")):
            for no, code in _code_lines(path):
                if _SELF_CMP.search(code):
                    hits.append("%s:%d: %s" % (path.relative_to(_ROOT), no, code.strip()))
    assert not hits, "客户扫描规则 S1764 会报这些行(判 NaN 请用 math.isnan):\n" + "\n".join(hits)


def test_nan_handling_unchanged_after_rewrite():
    import calibrate
    import costmodel
    import selectivity
    nan = float("nan")
    assert math.isinf(calibrate.relative_deviation(nan, 1.0))
    assert math.isinf(calibrate.relative_deviation(1.0, nan))
    assert calibrate.relative_deviation(2.0, 0.0) == 2.0
    assert calibrate.relative_deviation(3.0, 2.0) == 0.5
    with pytest.raises(costmodel.ModelError):
        costmodel.clamp_row_est(nan)
    assert costmodel.clamp_row_est(0.3) == 1.0 and costmodel.clamp_row_est(2.5) == 2.0
    with pytest.raises(costmodel.ModelError):
        costmodel._require_range("x", nan, 0.0, 1.0)
    with pytest.raises(costmodel.ModelError):
        costmodel._require_range("x", None, 0.0, 1.0)
    costmodel._require_range("x", 1, 0.0, 1.0)            # 整数也要能过
    with pytest.raises(selectivity.SelectivityError):
        selectivity._clamp(nan)
    assert selectivity._clamp(1.7) == 1.0 and selectivity._clamp(-2.0) == 0.0
