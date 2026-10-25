"""固定池化部署说明的守卫(2026-10-23 起;说明在《从源码构建到 K8s 运行》第 5 章)。

参数加了、改了而手册没跟上,客户照着手册配就是「配了没用」或「漏配了起不来」。这里核对:手册列全了网关的池化配置项
与渲染脚本的全部参数;已经删掉的 v1.2.0 池化(平台管理的沙箱)在现行文档里不再出现。
"""
import importlib.util
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOC = (_ROOT / "docs" / "从源码构建到K8s运行-完整手册.md").read_text(encoding="utf-8")

_spec = importlib.util.spec_from_file_location("render_pool", _ROOT / "k8s" / "tools" / "render-pool.py")
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


def test_every_gateway_pool_key_is_documented_with_its_default():
    cm = (_ROOT / "k8s" / "base" / "configmap-gateway.yaml").read_text(encoding="utf-8")
    keys = dict(re.findall(r'^\s{2}((?:RUNTIME_MODE|POOL_[A-Z_]+)):\s*"([^"]*)"', cm, re.M))
    assert len(keys) == 8, keys
    for k, v in keys.items():
        row = next((l for l in _DOC.splitlines() if l.startswith("| `%s`" % k)), "")
        assert row, "手册的池化配置表没写 %s" % k
        assert v in row, "手册里 %s 的默认值不是 ConfigMap 里的 %r" % (k, v)


def test_every_render_flag_is_documented():
    flags = {a.option_strings[0] for a in rp._parser()._actions if a.option_strings and a.option_strings[0] != "-h"}
    missing = sorted(f for f in flags if "`%s`" % f not in _DOC)
    assert not missing, "手册没写这些渲染参数:%s" % missing


_HISTORY = {"版本说明.md"}          # 历史版本的说明照实保留


@pytest.mark.parametrize("word", ["池化接入手册", "sandbox.yaml", "pool-sim", "e2e-pool.sh", "poolctl", "X-Sandbox-Key",
                                  "pool-serve", "pool-run"])
def test_the_platform_managed_pool_is_gone_from_current_docs(word):
    hits = [p.name for p in (_ROOT / "docs").glob("*.md") if p.name not in _HISTORY
            and word in p.read_text(encoding="utf-8")]
    assert not hits, "%s 还提到已删的 %s" % (hits, word)
