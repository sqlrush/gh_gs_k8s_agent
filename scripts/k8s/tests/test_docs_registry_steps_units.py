"""手册里「把清单指向内网镜像仓库」那几条 sed:抽出来对真清单的副本实际跑一遍,看改出来的镜像名对不对。

2026-10-15 写《从源码构建到 K8s 运行》时照着做才发现两处,三份手册都有:
  · 向量库那条 `s#image: opengauss/#image: $REG/opengauss:#` 改出来是
    `$REG/opengauss:opengauss:7.0.0-RC1` —— 不是合法镜像名,向量库起不来;
  · 对 k8s/templates/ 的 sed 改不到用户容器的镜像:模板里是 ${IMAGE},由网关拼名字。
    镜像推在内网仓库时,用户一登录就去 docker.io 拉,卡 ImagePullBackOff。现在靠 USER_IMAGE_REGISTRY。
"""
import pathlib
import re
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_REG = "registry.example/gaussdb-agent"
_DOCS = ("从源码构建到K8s运行-完整手册.md",)


def _sed_commands(doc):
    """手册里改镜像地址的命令行(含 `| xargs sed` 的续行写法)。"""
    lines = (_ROOT / "docs" / doc).read_text(encoding="utf-8").splitlines()
    cmds = []
    for i, line in enumerate(lines):
        if re.search(r'sed -i ("|\'\' ")s#\s*(image: |USER_IMAGE_REGISTRY)', line):
            cmd = line.strip()
            if cmd.startswith("|") and i > 0:          # grep -rl ... \ 的下一行
                cmd = lines[i - 1].strip().rstrip("\\") + " " + cmd
            cmds.append(cmd)
    return cmds


@pytest.mark.parametrize("doc", [d for d in _DOCS if (_ROOT / "docs" / d).is_file()])
def test_registry_steps_produce_valid_image_names(doc, tmp_path):
    cmds = _sed_commands(doc)
    assert cmds, "%s 里找不到改镜像地址的 sed" % doc
    shutil.copytree(_ROOT / "k8s", tmp_path / "k8s")
    script = "REG=%s\nTAG=t1\n" % _REG + "\n".join(c.replace("sed -i ''", "sed -i.bak").replace("sed -i ", "sed -i.bak ")
                                            for c in cmds)
    subprocess.run(["bash", "-c", script], cwd=tmp_path, check=True)

    base = tmp_path / "k8s" / "base"
    images = re.findall(r"^\s*image:\s*(\S+)", "\n".join(
        p.read_text(encoding="utf-8") for p in base.glob("*.yaml") if not p.name.endswith(".example.yaml")), re.MULTILINE)
    # base 里有镜像的只有网关(前端由前端方自建,样例清单不算;用户容器的镜像由网关拼名字)
    assert len(images) >= 1
    for img in images:
        assert img.startswith(_REG + "/"), "%s:%s 没指向内网仓库" % (doc, img)
        assert img.rsplit("/", 1)[-1].count(":") == 1, "%s:%s 不是合法的镜像名" % (doc, img)
    cm = (base / "configmap-gateway.yaml").read_text(encoding="utf-8")
    assert 'USER_IMAGE_REGISTRY: "%s"' % _REG in cm, "%s:没填 USER_IMAGE_REGISTRY,用户容器拉不到镜像" % doc


def test_no_doc_seds_the_pod_templates_for_images():
    for doc in _DOCS:
        path = _ROOT / "docs" / doc
        if path.is_file():
            assert not re.search(r"sed -i[^\n]*s#image:[^\n]*k8s/templates/", path.read_text(encoding="utf-8")), \
                "%s:模板里是 ${IMAGE},sed 改不到,要填 USER_IMAGE_REGISTRY" % doc
