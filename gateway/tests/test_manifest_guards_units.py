"""清单里两条容易回退的约束,都是 2026-09-21 真部署时踩出来的。

① **enableServiceLinks 必须显式关掉。** k8s 默认把命名空间里每个 Service 注入成环境变量。
   后果有两层:
   · 撞名:Service 叫 gateway → 注入 GATEWAY_PORT=`tcp://10.x.x.x:80`,和网关自己的
     配置项撞名,网关拒绝启动(fail closed 救了场,但本来不该发生);
   · **跨用户泄露**:用户 Pod 的 env 里会有 RUNTIME_<别人工号>_SERVICE_HOST,
     用户让模型打印环境变量就知道还有谁在用 —— 与「本对话看不到别人的会话」直接矛盾。
     u9003 的 Pod 里实测到了 u9001 与 u9002。

② **emptyDir 必须有 sizeLimit 且不能是 tmpfs。** /data 现在装本地态(库 + git 快照):
   没有 sizeLimit 会把节点磁盘写满;写 medium: Memory 会让用户的历史吃进内存配额;
   而撑爆 sizeLimit 会**驱逐** Pod,驱逐是非优雅终止 = 丢一个同步周期。
"""
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_POD_MANIFESTS = [
    _ROOT / "k8s" / "templates" / "runtime.yaml",
    _ROOT / "k8s" / "templates" / "kb-import.yaml",
    _ROOT / "k8s" / "base" / "gateway.yaml",
    _ROOT / "k8s" / "base" / "frontend.example.yaml",
]


def test_frontend_slot_is_isolated_and_reachable_only_from_gateway():
    """前端由前端方自建,本仓库只给接入位:样例清单不进 kustomization(否则 apply 出一个拉不到镜像的 Pod);
    Service 叫 frontend(网关默认把 /dash/* 代理到它);网络策略只让网关到前端 8080,前端出站全拒、不挂 NAS。
    这些是前端「拿不到任何人报告」的物理保证,不是代码约定。"""
    fe = (_ROOT / "k8s" / "base" / "frontend.example.yaml").read_text(encoding="utf-8")
    assert "persistentVolumeClaim" not in fe and "hostPath" not in fe, "前端不该挂任何卷"
    assert "runAsNonRoot: true" in fe and "app: gaussdb-frontend" in fe and "containerPort: 8080" in fe
    assert re.search(r"name:\s*frontend\s*#", fe) or re.search(r"name:\s*frontend\s*\n\s*namespace", fe), \
        "Service 必须叫 frontend(网关默认代理到它)"
    for path in (_ROOT / "k8s" / "base" / "networkpolicy.yaml", _ROOT / "k8s" / "local" / "networkpolicy-local.yaml"):
        np = path.read_text(encoding="utf-8")
        gw = np[np.index("name: gateway-policy"):]
        gw = gw[:gw.index("\n---")] if "\n---" in gw else gw
        assert "gaussdb-frontend" in gw.split("egress:")[1] and "8080" in gw.split("egress:")[1], \
            "%s:gateway-policy 出站没到前端 8080" % path.name
    np = (_ROOT / "k8s" / "base" / "networkpolicy.yaml").read_text(encoding="utf-8")
    fp = np[np.index("name: frontend-policy"):]
    assert "egress: []" in fp, "前端出站要全拒(egress: [])"
    assert "agent-gateway" in fp.split("egress:")[0], "前端入站只放行网关"
    kus = (_ROOT / "k8s" / "base" / "kustomization.yaml").read_text(encoding="utf-8")
    assert "frontend" not in "\n".join(l for l in kus.splitlines() if l.startswith("  - ")), \
        "前端样例不进 kustomization"


@pytest.mark.parametrize("path", _POD_MANIFESTS, ids=lambda p: p.name)
def test_service_links_are_disabled(path):
    text = path.read_text(encoding="utf-8")
    assert re.search(r"^\s*enableServiceLinks:\s*false\s*$", text, re.MULTILINE), (
        "%s 没关 enableServiceLinks —— 会把其他用户的 Service 注入成环境变量" % path.name)


@pytest.mark.parametrize("path", _POD_MANIFESTS[:2], ids=lambda p: p.name)
def test_emptydir_has_a_size_limit_and_is_not_tmpfs(path):
    text = path.read_text(encoding="utf-8")
    empties = re.findall(r"emptyDir:\s*\{([^}]*)\}", text)
    assert empties, "%s 里没有 emptyDir?" % path.name
    for body in empties:
        assert "sizeLimit" in body, "%s 有 emptyDir 没设 sizeLimit:{%s}" % (path.name, body)
        assert "medium" not in body, (
            "%s 的 emptyDir 指定了 medium(tmpfs)——用户的库会吃进内存配额:{%s}" % (path.name, body))


def test_gateway_rbac_cannot_list_secrets():
    """网关能建别人的口令 Secret,但**不能 list** —— 给了 list 就等于一次请求
    拿到全体用户的口令。这条权限边界要有人守着。"""
    text = (_ROOT / "k8s" / "base" / "gateway.yaml").read_text(encoding="utf-8")
    block = text[text.index('resources: ["secrets"]'):]
    verbs = re.search(r'verbs:\s*\[([^\]]*)\]', block).group(1)
    assert '"list"' not in verbs, "网关对 secrets 有 list 权限:%s" % verbs
    assert '"get"' in verbs, "要保留 get —— 「已有口令就沿用」靠它"


def test_every_tunable_the_code_reads_is_reachable_from_the_configmap():
    """**配置项必须真的能配到。**

    gateway/config.py 读一堆 GATEWAY_* 变量,但容器里的 env 是 gateway.yaml 一条条
    映射进去的 —— 代码读了、清单没映射的那些,运维在 ConfigMap 里写上去是**静默无效**:
    值收下了,行为一点不变,而且没有任何报错。写参数手册时才发现有四个是这样。

    监听端口是唯一的例外:改它还要同步改 containerPort 与 Service 的 targetPort,
    不是「配一下」的事,所以刻意不给出口,在手册里写明。
    """
    cfg = (_ROOT / "gateway" / "config.py").read_text(encoding="utf-8")
    read_by_code = set(re.findall(r'env\.get\("(GATEWAY_[A-Z_]+)"\)', cfg))
    read_by_code |= set(re.findall(r'_int\(env,\s*"(GATEWAY_[A-Z_]+)"', cfg))
    assert len(read_by_code) >= 10, "变量名提取失掉了?只找到 %r" % (read_by_code,)

    manifest = (_ROOT / "k8s" / "base" / "gateway.yaml").read_text(encoding="utf-8")
    provided = set(re.findall(r"\{name:\s*(GATEWAY_[A-Z_]+)", manifest))

    missing = read_by_code - provided - {"GATEWAY_LISTEN_PORT"}
    assert not missing, (
        "gateway.yaml 没把这些变量映射进容器,在 ConfigMap 里配它们是静默无效的:%s"
        % ", ".join(sorted(missing)))


def test_configmap_keys_are_all_consumed_by_the_deployment():
    """反向:ConfigMap 里有键而清单没引用,同样是「配了没用」。"""
    cm = (_ROOT / "k8s" / "base" / "configmap-gateway.yaml").read_text(encoding="utf-8")
    body = cm[cm.index("data:"):]
    keys = {m.group(1) for m in re.finditer(r"^\s{2}([A-Z][A-Z0-9_]*):", body, re.MULTILINE)}
    assert keys, "configmap-gateway.yaml 的 data 解析不出键?"

    manifest = (_ROOT / "k8s" / "base" / "gateway.yaml").read_text(encoding="utf-8")
    referenced = set(re.findall(r"configMapKeyRef:\s*\{name:\s*gateway-config,\s*key:\s*([A-Z0-9_]+)",
                                manifest))
    orphans = keys - referenced
    assert not orphans, "ConfigMap 里这些键没人读:%s" % ", ".join(sorted(orphans))


def test_nfs_address_lives_in_the_pv_example_not_in_the_pvc():
    """**NFS 地址在 PV 里,不在 PVC 里;文档不能让人去 PVC 里找 nfs:。**

    2026-09-22 客户问「nas-pvc.yaml 是配 NAS 的吗」时翻出来:两份手册都写着
    「编辑 k8s/base/nas-pvc.yaml,把 nfs: 那几行改成…」,而那个文件从头到尾只有一个
    PersistentVolumeClaim,根本没有 nfs: —— PVC 是申请,供给在 PV / StorageClass。
    仓库里给客户用的 PV 样例当时也不存在(只有 Mac 用的 hostPath 那份)。
    客户照着做要么找不到那几行,要么把 nfs: 塞进 PVC 让 apply 报未知字段。
    """
    pvc = (_ROOT / "k8s" / "base" / "nas-pvc.yaml").read_text(encoding="utf-8")
    assert "kind: PersistentVolumeClaim" in pvc and "nfs:" not in pvc.replace("nas-pv-nfs", "")

    pv = _ROOT / "k8s" / "base" / "nas-pv-nfs.example.yaml"
    assert pv.is_file(), "给客户用的静态 PV 样例不存在"
    body = pv.read_text(encoding="utf-8")
    assert "kind: PersistentVolume\n" in body and "\n  nfs:\n" in body
    assert "local_lock=all" in body, "mountOptions 少了 local_lock=all —— 目录独占靠它"
    assert "persistentVolumeReclaimPolicy: Retain" in body, "回收策略必须 Retain,那是用户数据"
    assert "storageClassName: nas" in body and "storageClassName: nas" in pvc, \
        "PV 与 PVC 的 storageClassName 必须一致,否则绑不上"

    # 凡是教读者填 nfs: 的手册,都必须指向真有 nfs: 的那个文件
    for name in ("从源码构建到K8s运行-完整手册.md",):
        doc = (_ROOT / "docs" / name).read_text(encoding="utf-8")
        if "nfs:" in doc:
            assert "nas-pv-nfs.example.yaml" in doc, \
                "%s 教人填 nfs: 却没指向 nas-pv-nfs.example.yaml" % name


def test_runtime_exposes_reports_port_and_policies_allow_it():
    """报告只读端口 4097:容器要暴露、Service 要有、runtime 入站要从网关放行、网关出站要到它。
    四处漏一处的表现都是「大盘无数据」,而 Pod 本身健康。"""
    rt = (_ROOT / "k8s" / "templates" / "runtime.yaml").read_text(encoding="utf-8")
    assert re.search(r"containerPort:\s*4097", rt), "runtime.yaml 容器没暴露 4097"
    assert re.search(r"\{port:\s*4097,\s*targetPort:\s*4097", rt), "runtime Service 没有 4097"
    np = (_ROOT / "k8s" / "base" / "networkpolicy.yaml").read_text(encoding="utf-8")
    runtime_pol = np[np.index("name: runtime-policy"):np.index("name: kb-import-policy")]
    assert "4097" in runtime_pol.split("egress:")[0], "runtime-policy 入站没放行 4097"
    gw_pol = np[np.index("name: gateway-policy"):np.index("name: frontend-policy")]
    assert "4097" in gw_pol.split("egress:")[1], "gateway-policy 出站没到 4097"
    ki = (_ROOT / "k8s" / "templates" / "kb-import.yaml").read_text(encoding="utf-8")
    assert "4097" not in ki, "kb-import 没有大盘,不开报告端口"
    # 本地覆盖层按名字**整个替换** runtime-policy 与 gateway-policy —— base 加了端口它不会跟着有。
    # 2026-09-22 e2e 前发现:base 改完本地 apply 后网关照样到不了 4097。
    lp = (_ROOT / "k8s" / "local" / "networkpolicy-local.yaml").read_text(encoding="utf-8")
    l_runtime = lp[lp.index("name: runtime-policy"):lp.index("name: kb-import-policy")]
    assert "4097" in l_runtime.split("egress:")[0], "local 覆盖层的 runtime-policy 入站没放行 4097"
    l_gw = lp[lp.index("name: gateway-policy"):]
    assert "4097" in l_gw.split("egress:")[1], "local 覆盖层的 gateway-policy 出站没到 4097"


def test_gateway_rbac_has_no_exec():
    """网关没有进用户容器的理由。

    **要剔掉注释再查**:清单里有一行注释写着「刻意不给 pods/exec」,
    直接对全文做子串搜索会把那行当成权限(第一版就这么红了)。
    """
    lines = [ln for ln in (_ROOT / "k8s" / "base" / "gateway.yaml")
             .read_text(encoding="utf-8").splitlines()
             if not ln.lstrip().startswith("#")]
    body = "\n".join(lines)
    for forbidden in ("pods/exec", "pods/attach", "pods/portforward"):
        assert forbidden not in body, "网关 RBAC 里出现了 %s" % forbidden


def test_user_image_tag_matches_the_gateway_image():
    """清单里两处版本必须是同一个:网关镜像、给用户建 Pod 的 USER_IMAGE_TAG。

    2026-09-24 客户现场:网关的标签每次发版都跟着改,USER_IMAGE_TAG 却一直停在 v0.9.3,
    客户照清单部署,网关就在给用户建一个旧版本的 Pod。
    """
    import re
    base = _ROOT / "k8s" / "base"
    user = re.search(r'USER_IMAGE_TAG:\s*"([^"]+)"', (base / "configmap-gateway.yaml").read_text(encoding="utf-8")).group(1)
    gw = re.search(r"image:\s*\S*gaussdb-agent-[a-z-]+:(\S+)", (base / "gateway.yaml").read_text(encoding="utf-8")).group(1)
    assert gw == user, "USER_IMAGE_TAG=%s,网关镜像标签 %s —— 两处要一致" % (user, gw)


def test_import_container_exposes_the_clip_receiver_and_policies_allow_only_the_gateway():
    """网页剪藏接收 4095(2026-10-27):容器端口、Service、开关变量、kb-import 入站、网关出站,base 与本地覆盖层各一份。
    漏一处的表现都是剪藏页报「连不上剪藏接收服务」,而导入容器本身健康。"""
    ki = (_ROOT / "k8s" / "templates" / "kb-import.yaml").read_text(encoding="utf-8")
    assert re.search(r"containerPort:\s*4095", ki) and re.search(r"\{port:\s*4095,\s*targetPort:\s*4095", ki)
    assert re.search(r'\{name:\s*KB_CLIP_RECEIVER,\s*value:\s*"1"\}', ki), "导入容器要打开剪藏接收(entrypoint 按它拉起 serve-clip)"
    rt = (_ROOT / "k8s" / "templates" / "runtime.yaml").read_text(encoding="utf-8")
    assert "4095" not in rt and "KB_CLIP_RECEIVER" not in rt, "诊断容器不收剪藏"
    for path in (_ROOT / "k8s" / "base" / "networkpolicy.yaml", _ROOT / "k8s" / "local" / "networkpolicy-local.yaml"):
        np = path.read_text(encoding="utf-8")
        ki_pol = np[np.index("name: kb-import-policy"):np.index("name: gateway-policy")]
        assert "4095" in ki_pol.split("egress:")[0] and "agent-gateway" in ki_pol.split("egress:")[0], \
            "%s:kb-import-policy 入站没从网关放行 4095" % path.name
        gw_pol = np[np.index("name: gateway-policy"):]
        assert "4095" in gw_pol.split("egress:")[1].split("gaussdb-frontend")[0], "%s:gateway-policy 出站没到 4095" % path.name
        rt_pol = np[np.index("name: runtime-policy"):np.index("name: kb-import-policy")]
        assert "4095" not in rt_pol


def test_clip_settings_live_once_in_agent_config_and_reach_the_gateway():
    """名单只在 agent-config 里配一处:导入容器 envFrom 整份读,网关按键映射。"""
    cm = (_ROOT / "k8s" / "base" / "configmap-agent.yaml").read_text(encoding="utf-8")
    assert re.search(r'^  KB_CLIP_ORIGINS: ""$', cm, re.M), "默认关闭:名单为空"
    assert re.search(r'^  KB_CLIP_MAX_MB: "30"$', cm, re.M)
    gw = (_ROOT / "k8s" / "base" / "gateway.yaml").read_text(encoding="utf-8")
    for env, key in (("GATEWAY_KB_CLIP_ORIGINS", "KB_CLIP_ORIGINS"), ("GATEWAY_KB_CLIP_MAX_MB", "KB_CLIP_MAX_MB")):
        assert re.search(r"\{name:\s*%s,\s*valueFrom:\s*\{configMapKeyRef:\s*\{name:\s*agent-config,\s*key:\s*%s,\s*optional:\s*true\}"
                         % (env, key), gw), "gateway.yaml 没从 agent-config 映射 %s" % key
