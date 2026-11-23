"""《Fuyao沙箱部署手册》的守卫(2026-11-18)。

配置项、Secret、模板脚本参数加了或改了而部署手册没跟上,现场照着手册配就是「配了没用」或「漏配了起不来」。这里核对:
手册列全了 gateway-config 的 FUYAO_* 键与默认值、清单引用的每个 Secret 与键、模板注册脚本的全部参数;
写了现场实测的容量规则与 64K 页补丁;其他交付文档指向它。
"""
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOC = (_ROOT / "docs" / "Fuyao沙箱部署手册.md").read_text(encoding="utf-8")


def _row(key: str) -> str:
    return next((line for line in _DOC.splitlines() if line.startswith("| `%s`" % key)), "")


def test_every_fuyao_gateway_key_is_documented_with_its_default():
    cm = (_ROOT / "k8s" / "base" / "configmap-gateway.yaml").read_text(encoding="utf-8")
    keys = dict(re.findall(r'^\s{2}(FUYAO_[A-Z_]+|PRODUCT_NAME|OPS_ADMIN_ROLES|RUNTIME_MODE):\s*"([^"]*)"', cm, re.M))
    assert len(keys) >= 18, keys
    for k, v in keys.items():
        row = _row(k)
        assert row, "部署手册 §6.2 没写 %s" % k
        if k != "RUNTIME_MODE":
            assert not v or "`%s`" % v in row, "部署手册里 %s 的默认值不是 ConfigMap 里的 %r" % (k, v)


def test_every_secret_the_manifests_reference_is_documented():
    texts = [(_ROOT / "k8s" / "base" / "gateway.yaml").read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8") for p in sorted((_ROOT / "k8s" / "fuyao").glob("*.yaml"))]
    refs = set()
    for t in texts:
        refs |= set(re.findall(r"secretKeyRef:\s*\{name:\s*([a-z0-9-]+),\s*key:\s*([A-Z0-9_]+)", t))
    assert ("sandbox-control", "KEY") in refs and ("fuyao-api", "API_KEY") in refs, refs
    for name, key in sorted(refs):
        rows = [line for line in _DOC.splitlines() if line.startswith("| `%s` | `%s`" % (name, key))]
        assert rows, "部署手册 §6.1 的 Secret 表没写 %s / %s" % (name, key)


def test_every_template_script_flag_is_documented():
    src = (_ROOT / "scripts" / "build-e2b-template.py").read_text(encoding="utf-8")
    flags = set(re.findall(r'add_argument\("(--[a-z0-9-]+)"', src))
    assert "--from-oci" in flags and "--cpu" in flags, flags
    missing = sorted(f for f in flags if "`%s`" % f not in _DOC)
    assert not missing, "部署手册 §4.1 没写这些模板脚本参数:%s" % missing


def test_the_manual_carries_the_field_facts_and_the_other_docs_point_to_it():
    for must in ("暂停的沙箱不占名额", "agentPodMin", "SubSandboxGroup", "1.5.0 起不需要现场补丁", "512Mi",
                 "120 秒", "--from-oci", "force", "gaussdb-agent-sandbox-read", "GATEWAY_NAMESPACE"):
        assert must in _DOC, "部署手册缺:%s" % must
    for name in ("部署手册-从零到上线.md", "Fuyao沙箱手册.md", "仓库结构说明.md", "版本说明.md"):
        assert "Fuyao沙箱部署手册" in (_ROOT / "docs" / name).read_text(encoding="utf-8"), "%s 没指向部署手册" % name
    old = (_ROOT / "docs" / "Fuyao沙箱手册.md").read_text(encoding="utf-8")
    assert "kubectl apply -k k8s/overlays/fuyao" not in old, "部署步骤只留在部署手册一处,运维手册不再重复"
