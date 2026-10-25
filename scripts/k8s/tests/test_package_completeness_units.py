"""交付包必须装齐清单里会用到的每一个镜像。

漏打一个镜像,客户在现场才会撞到 —— 而那时往往没有外网可以补拉,整个部署卡住。
package-images.sh 原来只打了 runtime 与 kb-import 两个,网关与向量库基础镜像都没进去。

这条闸很便宜:把 k8s/ 清单里引用的镜像和打包脚本里导出的镜像对一遍。
"""
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PACKAGER = _ROOT / "scripts" / "package-images.sh"
_BUILDER = _ROOT / "scripts" / "build-images.sh"


def _images_in_manifests() -> set:
    """k8s/base 与 k8s/templates 里 image: 字段引用的镜像(去掉标签与仓库前缀)。"""
    out = set()
    for p in list((_ROOT / "k8s" / "base").glob("*.yaml")) + \
             list((_ROOT / "k8s" / "templates").glob("*.yaml")):
        for m in re.finditer(r"^\s*image:\s*([^\s#]+)", p.read_text(encoding="utf-8"), re.MULTILINE):
            ref = m.group(1).strip().strip('"')
            ref = ref.split("${")[0] or ref          # 模板占位符
            out.add(ref.rsplit(":", 1)[0].rsplit("/", 1)[-1])
    return {x for x in out if x}


def test_packager_exports_every_agent_image():
    """后端三个镜像都要进包(前端镜像由前端方自建,不在本包)。"""
    body = _PACKAGER.read_text(encoding="utf-8")
    loop = re.search(r"for T in ([a-z\- ]+); do", body)
    assert loop, "package-images.sh 里找不到镜像循环"
    packaged = set(loop.group(1).split())
    assert packaged == {"runtime", "kb-import", "gateway"}, \
        "打包的镜像与预期不符:%s —— 漏一个客户现场就装不起来" % sorted(packaged)


def test_builder_and_packager_agree():
    """构建的和打包的必须是同一组 —— 构建了没打包等于白构建。"""
    built = set(re.search(r"for T in ([a-z\- ]+); do", _BUILDER.read_text(encoding="utf-8")).group(1).split())
    built.add("gateway")          # 网关是独立 Dockerfile,不在那个循环里
    loop = re.search(r"for T in ([a-z\- ]+); do", _PACKAGER.read_text(encoding="utf-8"))
    assert built == set(loop.group(1).split())


def test_the_retired_vector_db_is_gone_from_the_package_and_the_manifests():
    """2026-10-23 知识库向量库(openGauss vectordb)下线,索引改为 NAS 上的 index/store.sqlite。

    打包脚本还带着它,客户就得为一个不再部署的组件多载一个几百 MB 的镜像;清单里还留着它,
    `kubectl apply -k` 会把它重新建出来、继续占一块 20Gi 的盘。两处都不能有。
    """
    pack = _PACKAGER.read_text(encoding="utf-8")
    assert "VECTORDB_IMAGE" not in pack and "opengauss/opengauss" not in pack
    assert not (_ROOT / "k8s" / "base" / "vectordb.yaml").exists()
    for f in list((_ROOT / "k8s").rglob("*.yaml")):
        text = f.read_text(encoding="utf-8")
        assert "app: vectordb" not in text and "vectordb-auth" not in text and "KB_PG_" not in text, f


def test_package_ships_the_whole_docs_tree_not_a_hardcoded_list():
    """文档清单写死过一次,加了两份手册却忘了加进那一行 —— 客户拿到的包里没有最重要的
    两份,而打包脚本一声不响地成功了。改成整个 docs/ 打进去;docs/ 里只放交付文档,内部材料不进本仓库。
    """
    body = _PACKAGER.read_text(encoding="utf-8")
    assert re.search(r"tar czf .*-k8s\.tar\.gz.*\\\n(.*\\\n)*\s*k8s docs\s*$", body, re.MULTILINE), \
        "k8s 包应该整个打 docs/,不要再列具体文件名"
    for internal in ("plans", "security", "specs", "prototypes"):
        assert not (_ROOT / "docs" / internal).exists(), "docs/%s 是内部材料,不进本仓库" % internal


def test_package_does_not_ship_macos_resource_forks():
    """在 Mac 上打包时 bsdtar 会给每个带扩展属性的文件塞一个 `._<原名>`。

    客户解开后 docs/ 里会多出 `._参数手册.md` 这一堆,还带着打包机的
    com.apple.provenance 属性 —— 交付给客户的包里不该有这些。
    """
    body = _PACKAGER.read_text(encoding="utf-8")
    assert "COPYFILE_DISABLE=1 tar czf" in body, "打包 tar 前要设 COPYFILE_DISABLE=1"
    for pat in ("._*", ".DS_Store"):
        assert "--exclude='%s'" % pat in body, "还要显式排除 %s" % pat


def test_customer_facing_docs_exist_where_the_package_expects_them():
    """客户照着做的那两份必须都在 docs/ 下。"""
    for name in ("从源码构建到K8s运行-完整手册.md", "前端对接接口说明.md"):
        assert (_ROOT / "docs" / name).is_file(), "缺 docs/%s" % name


def test_every_namespace_delete_in_the_docs_is_followed_by_releasing_the_pv():
    """手册里凡是让人删命名空间的地方,后面都要跟一句解除 PV 旧绑定。

    2026-09-24 客户照「清理重来」删了命名空间再装,静态 PV(Retain)变成 Released、仍记着旧 PVC,
    新 PVC 永远绑不上,所有用户容器卡在 Pending。
    """
    for name in ("从源码构建到K8s运行-完整手册.md",):
        text = (_ROOT / "docs" / name).read_text(encoding="utf-8")
        for m in re.finditer(r"kubectl delete namespace gaussdb-agent", text):
            assert "claimRef" in text[m.end():m.end() + 800], "%s:删命名空间之后没写解除 PV 旧绑定" % name


def test_the_sso_simulator_ships_with_the_package():
    """手册让客户跑 k8s/tools/sso-sim.py:它必须在包会整个打进去的 k8s/ 下(原来在 scripts/,包里没有)。"""
    assert (_ROOT / "k8s" / "tools" / "sso-sim.py").is_file()
    assert not (_ROOT / "scripts" / "k8s" / "sso-sim.py").exists(), "只留一份,别两处各改各的"
    assert "k8s/tools/sso-sim.py" in (_ROOT / "docs" / "从源码构建到K8s运行-完整手册.md").read_text(encoding="utf-8")


def test_packaged_files_are_readable_by_everyone():
    """离线包里的 k8s/ 与 docs/ 要对所有人可读(tar 原样保留权限)。

    2026-10-16 发现:在沙箱里改过的文件落到工作树上是 0600 —— runtime-doctor.sh、vectordb 的 Dockerfile
    与几份原型都是。打进包后客户解压出来,换个账号(或 sudo 之外的运维账号)就读不了。这里把包里的目录也钉住。
    """
    import os
    import stat
    import subprocess
    out = subprocess.run(["git", "-C", str(_ROOT), "ls-files", "-z", "k8s", "docs", "scripts"],
                         capture_output=True, check=True).stdout.split(b"\0")
    bad = []
    for raw in out:
        if not raw:
            continue
        p = _ROOT / os.fsdecode(raw)
        if p.is_file() and not stat.S_IMODE(p.stat().st_mode) & stat.S_IROTH:
            bad.append("%s(%o)" % (p.relative_to(_ROOT), stat.S_IMODE(p.stat().st_mode)))
    assert not bad, "这些文件 others 不可读,打包前 chmod 644(脚本 755):%s" % bad
