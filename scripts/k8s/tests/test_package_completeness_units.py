"""交付包必须装齐清单里会用到的每一个镜像。

漏打一个镜像,行方在现场才会撞到 —— 而那时往往没有外网可以补拉,整个部署卡住。
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


def _packaged() -> set:
    body = _PACKAGER.read_text(encoding="utf-8")
    loop = re.search(r"for T in ([a-z\- ]+)( \$KBSTD)?; do", body)
    assert loop, "package-images.sh 里找不到镜像循环"
    out = set(loop.group(1).split())
    if loop.group(2):
        assert 'KBSTD="kbstd"' in body, "标准知识库镜像默认要进包(只有 --without-kbstd 才不进)"
        out.add("kbstd")
    return out


def test_packager_exports_every_agent_image():
    """我们发布的五个镜像都要进包(标准知识库 2026-10-27 起)。"""
    packaged = _packaged()
    assert packaged == {"runtime", "kb-import", "gateway", "frontend", "kbstd"}, \
        "打包的镜像与预期不符:%s —— 漏一个现场就装不起来" % sorted(packaged)


def test_every_image_the_manifests_use_is_in_the_package():
    """清单里引用的每个镜像都要在离线包里:标准知识库安装 Job 引用 kbstd,包里没有它,Job 就卡在拉镜像。"""
    used = {x.replace("gaussdb-agent-", "") for x in _images_in_manifests() if x.startswith("gaussdb-agent-")}
    assert used and used <= _packaged(), "清单用到、包里没有:%s" % sorted(used - _packaged())


def test_builder_and_packager_agree():
    """构建的和打包的必须是同一组 —— 构建了没打包等于白构建。"""
    built = set(re.search(r"for T in ([a-z\- ]+); do", _BUILDER.read_text(encoding="utf-8")).group(1).split())
    built.add("gateway")          # 网关是独立 Dockerfile,不在那个循环里
    built.add("frontend")         # 前端同上(纯静态 + nginx)
    if "Dockerfile.kbstd" in _BUILDER.read_text(encoding="utf-8"):
        built.add("kbstd")        # 标准知识库:给 --kbstd 才构建(包不在代码仓库里)
    assert built == _packaged()


def test_the_retired_vector_db_is_gone_from_the_package_and_the_manifests():
    """2026-10-23 知识库向量库(openGauss vectordb)下线,索引改为 NAS 上的 index/store.sqlite。

    打包脚本还带着它,行方就得为一个不再部署的组件多载一个几百 MB 的镜像;清单里还留着它,
    `kubectl apply -k` 会把它重新建出来、继续占一块 20Gi 的盘。两处都不能有。
    """
    pack = _PACKAGER.read_text(encoding="utf-8")
    assert "VECTORDB_IMAGE" not in pack and "opengauss/opengauss" not in pack
    assert not (_ROOT / "k8s" / "base" / "vectordb.yaml").exists()
    for f in list((_ROOT / "k8s").rglob("*.yaml")):
        text = f.read_text(encoding="utf-8")
        assert "app: vectordb" not in text and "vectordb-auth" not in text and "KB_PG_" not in text, f


def test_package_ships_the_whole_docs_tree_not_a_hardcoded_list():
    """文档清单写死过一次,加了两份手册却忘了加进那一行 —— 行方拿到的包里没有最重要的
    两份,而打包脚本一声不响地成功了。改成整个 docs/ 打进去,只排除内部用的。
    """
    body = _PACKAGER.read_text(encoding="utf-8")
    assert re.search(r"tar czf .*-k8s\.tar\.gz.*\\\n(.*\\\n)*\s*k8s docs\s*$", body, re.MULTILINE), \
        "k8s 包应该整个打 docs/,不要再列具体文件名"
    # specs/ 里是备选方案、退路、残余风险与未定项 —— 交付文档不写我们的讨论过程,
    # 而那一份从头到尾都是讨论过程。它随包发出去过,发现时补上这条守卫。
    # prototypes/ 是大盘设计稿:示例数字、给 user 拍板的方案对比、我们的建议 —— 全是讨论过程
    for internal in ("docs/plans", "docs/security", "docs/specs", "docs/prototypes"):
        assert "--exclude='%s'" % internal in body, "内部文档 %s 应该排除在交付包外" % internal
    # 这三份是早期文档:版本号停在 v0.4.1 / v0.7,还指向不随包发出的 specs/ 与 plans/。
    # 仓库结构说明把它们列为「内部件,不随包发出」,而 v1.0.4 的包里其实带着 —— 验 v1.1.0 包时发现。
    for internal in ("docs/env-contract.md", "docs/k8s-deploy.md", "docs/delivery-容器化交付手册.md"):
        assert "--exclude='%s'" % internal in body, "早期内部文档 %s 应该排除在交付包外" % internal


def test_repo_guide_internal_table_matches_what_the_package_excludes():
    """仓库结构说明「内部件(不随包发出)」表里列的每一项,打包脚本都必须真的排除。

    表是写给行方看的承诺,打包脚本是事实。两边各改各的,就会出现「文档说不发、包里却有」。
    """
    guide = (_ROOT / "docs" / "仓库结构说明.md").read_text(encoding="utf-8")
    section = guide[guide.index("### 内部件"):]
    section = section[:section.index("\n---")]
    listed = set(re.findall(r"`([^`]+)`", section))
    body = _PACKAGER.read_text(encoding="utf-8")
    for name in listed:
        path = "docs/" + name.rstrip("/")
        assert "--exclude='%s'" % path in body, \
            "仓库结构说明说 %s 不随包发出,package-images.sh 却没排除它" % path


def test_package_does_not_ship_macos_resource_forks():
    """在 Mac 上打包时 bsdtar 会给每个带扩展属性的文件塞一个 `._<原名>`。

    行方解开后 docs/ 里会多出 `._参数手册.md` 这一堆,还带着打包机的
    com.apple.provenance 属性 —— 交付给行方的包里不该有这些。
    """
    body = _PACKAGER.read_text(encoding="utf-8")
    assert "COPYFILE_DISABLE=1 tar czf" in body, "打包 tar 前要设 COPYFILE_DISABLE=1"
    for pat in ("._*", ".DS_Store"):
        assert "--exclude='%s'" % pat in body, "还要显式排除 %s" % pat


def test_deploy_doc_handles_the_arch_suffix_the_packager_actually_writes():
    """**离线包里的标签带不带 `-<arch>` 后缀,取决于在哪台机器上打的包。**

    package-images.sh 里 `SUFFIX=""; [ "$ARCH" != "$HOST_ARCH" ] && SUFFIX="-$ARCH"` ——
    在 Mac(arm64)上打包时,arm64 那份没有后缀、amd64 那份是
    `gaussdb-agent-runtime:<TAG>-amd64`。而部署手册原来写的期望输出与重打标签
    命令都用不带后缀的标签:行方在 x86 集群上照着做,到 `docker tag` 那一步
    就是 "No such image",而前一步 `docker load` 是成功的。
    """
    pkg = _PACKAGER.read_text(encoding="utf-8")
    assert 'SUFFIX="-$ARCH"' in pkg, "打包脚本的架构后缀规则变了,这条守卫要跟着改"

    doc = (_ROOT / "docs" / "部署手册-从零到上线.md").read_text(encoding="utf-8")
    load = doc[doc.index("### 1.2"):doc.index("### 1.4")]
    assert "-amd64" in load, "部署手册没提离线包标签带架构后缀"
    assert "MANIFEST.txt" in load, "应指向 MANIFEST.txt —— 那里写的是实际标签"
    assert re.search(r"docker tag gaussdb-agent-\$img:\$TAG\$SFX\s+gaussdb-agent-\$img:\$TAG",
                     load), "缺「先去掉架构后缀」那一步"


def test_customer_facing_docs_exist_where_the_package_expects_them():
    """行方照着做的那几份必须都在 docs/ 下。"""
    for name in ("接入手册-SSO与K8s.md", "部署手册-从零到上线.md",
                 "命令卡-测试环境搭建.md", "快速搭建-K8s.md", "单机演示-不用K8s.md",
                 "参数手册.md", "对接清单-各方要做什么.md", "功能清单-容器版.md",
                 "仓库结构说明.md", "固定池化手册.md", "现场测试-模拟SSO.md", "从源码构建到K8s运行-完整手册.md",
                 "发版台-运维手册.md", "Fuyao沙箱手册.md", "Fuyao沙箱部署手册.md"):
        assert (_ROOT / "docs" / name).is_file(), "缺 docs/%s" % name


def test_every_namespace_delete_in_the_docs_is_followed_by_releasing_the_pv():
    """手册里凡是让人删命名空间的地方,后面都要跟一句解除 PV 旧绑定。

    2026-09-24 行方照「清理重来」删了命名空间再装,静态 PV(Retain)变成 Released、仍记着旧 PVC,
    新 PVC 永远绑不上,所有用户容器卡在 Pending。
    """
    for name in ("命令卡-测试环境搭建.md", "部署手册-从零到上线.md"):
        text = (_ROOT / "docs" / name).read_text(encoding="utf-8")
        for m in re.finditer(r"kubectl delete namespace gaussdb-agent", text):
            assert "claimRef" in text[m.end():m.end() + 800], "%s:删命名空间之后没写解除 PV 旧绑定" % name


def test_the_sso_simulator_ships_with_the_package():
    """现场测试手册让行方跑 k8s/tools/sso-sim.py:它必须在包会整个打进去的 k8s/ 下(原来在 scripts/,包里没有)。"""
    assert (_ROOT / "k8s" / "tools" / "sso-sim.py").is_file()
    assert not (_ROOT / "scripts" / "k8s" / "sso-sim.py").exists(), "只留一份,别两处各改各的"
    assert "k8s/tools/sso-sim.py" in (_ROOT / "docs" / "现场测试-模拟SSO.md").read_text(encoding="utf-8")


def test_packaged_files_are_readable_by_everyone():
    """离线包里的 k8s/ 与 docs/ 要对所有人可读(tar 原样保留权限)。

    2026-10-16 发现:在沙箱里改过的文件落到工作树上是 0600 —— runtime-doctor.sh、vectordb 的 Dockerfile
    与几份原型都是。打进包后行方解压出来,换个账号(或 sudo 之外的运维账号)就读不了;前端那份已经因此
    让 nginx 回过 403(docker/tests 有守卫)。这里把包里的两个目录也钉住。
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
