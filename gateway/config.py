"""网关的环境变量契约。

**这一层的核心是 fail closed。** 网关的职责是「凭工号建 Pod 并代理过去」,如果信任
配置漏了,它就是一个「任填工号即可进入任何人环境」的接口 —— 而且从外面看它工作得
好好的,没有任何报错。所以:没配信任来源就**拒绝启动**,而不是警告后继续跑。

同样的道理用在回收模式与闲置时长上:值不认识就拒绝启动,不要悄悄用默认值 ——
运维以为自己配的是 delete,实际跑的是 scale,Pod 一个都没回收,过一周才发现。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Mapping, Tuple
from urllib.parse import urlsplit

from . import reaper

DEFAULT_USER_HEADER = "X-Agent-User"
DEFAULT_ROLES_HEADER = "X-Agent-Roles"
DEFAULT_PORT = 8080

# 监听端口的变量名**刻意不叫 GATEWAY_PORT**:k8s 会给命名空间里每个 Service 自动注入
# 一组发现变量 `<SVCNAME>_PORT` / `<SVCNAME>_SERVICE_PORT` / `<SVCNAME>_SERVICE_HOST`。
# Service 叫 gateway,于是 GATEWAY_PORT 被注入成 `tcp://10.x.x.x:80` —— 2026-09-21 真部署
# 时网关因此拒绝启动(fail closed 在这里救了场:它没有悄悄用错端口)。
# Pod 里还额外设了 enableServiceLinks: false 把那组变量整个关掉,这里是第二道。
ENV_PORT = "GATEWAY_LISTEN_PORT"

# 用户的诊断容器怎么来(2026-10-23):ondemand = 按人现建 Pod(v1.3.x 的做法,默认);pool = 固定池化
# (部署时建好的 StatefulSet,按槽位分配,docs/plans/2026-10-23-fixed-pool-steward.md)。
# 知识库管理员的导入容器两种模式下都按需建。
RUNTIME_ONDEMAND, RUNTIME_POOL = "ondemand", "pool"
# fuyao(2026-11-17)= 每人每种环境(诊断 / 导入)一台 openFuyao 沙箱(OpenSandbox + E2B 微型虚拟机),
# 导入环境也是沙箱;设计见 docs/specs/2026-11-17-fuyao-sandbox-design.md
RUNTIME_FUYAO = "fuyao"
RUNTIME_MODES = (RUNTIME_ONDEMAND, RUNTIME_POOL, RUNTIME_FUYAO)
_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_QUANTITY_RE = re.compile(r"^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti)?$")
# 只给测试集群:把固定池化各时长的下限降到 5 秒(e2e 要在几分钟里走完闲置 → 保留 → 腾挪)。
# **刻意不叫 GATEWAY_***,正式清单(gateway.yaml / ConfigMap)里不许出现它(守卫测试核对)。
TEST_SHORT_TIMERS = "POOL_TEST_SHORT_TIMERS"
# 网页剪藏单次上限(MB)的天花板:网关单请求上限 64 MB,图片按 base64 传要多三分之一
KB_CLIP_MAX_MB_CEILING = 45
# 产品名(大盘标题、logo、日报报头):交付给不同客户时改这一项,代码里不写死客户称谓
DEFAULT_PRODUCT_NAME = "GaussDB 智能体"
PRODUCT_NAME_MAX = 40


class ConfigError(Exception):
    """配置不完整或不合法。启动时抛,不要留到运行时。"""


@dataclass(frozen=True)
class Config:
    namespace: str
    image_tag: str
    # 用户 Pod 的镜像仓库前缀(如 registry.example/gaussdb-agent)。空 = 不带前缀,只认节点上已载入的镜像。
    image_registry: str = ""
    pull_policy: str = "IfNotPresent"
    port: int = DEFAULT_PORT
    user_header: str = DEFAULT_USER_HEADER
    roles_header: str = DEFAULT_ROLES_HEADER
    trust_secret: str = ""
    trust_cidrs: Tuple[str, ...] = ()
    reap_mode: str = reaper.MODE_SCALE
    idle_seconds: int = reaper.DEFAULT_IDLE_SECONDS
    sweep_seconds: int = reaper.DEFAULT_SWEEP_SECONDS
    activity_interval_s: int = 60
    ready_timeout_s: int = 120
    kb_admin_roles: Tuple[str, ...] = ("kb-admin",)
    # 运维管理员(资源大盘):角色头里带其中之一,或工号登记在 ConfigMap agent-roles 的 ops-admins 里
    ops_admin_roles: Tuple[str, ...] = ("ops-admin",)
    product_name: str = DEFAULT_PRODUCT_NAME
    # 控制 API 的独立密钥。空 = 控制 API 关闭(只提供代理入口)。
    admin_token: str = ""
    # 大盘前端的 Service 名:/dash/* 代理到 <frontend_service>.<namespace>.svc:80。
    # 前端镜像没部署时这条路 502,不影响对话与报告两条路。
    frontend_service: str = "frontend"
    runtime_mode: str = RUNTIME_ONDEMAND
    pool_selector: str = "app=gaussdb-agent,role=pool"
    pool_control_key: str = ""
    pool_idle_seconds: int = 1800           # 在用多久没有交互(也没有会话忙)就转保留
    pool_early_idle_seconds: int = 600      # 满员时,闲置超过这么久的在用者可以被提前转保留
    pool_retain_days: int = 7
    pool_free_slots: int = 1                # 每个 Pod 后台至少留几个空闲槽位给新人
    pool_sweep_seconds: int = 30
    pool_wait_seconds: int = 60             # 一次请求最多为分配等多久;等不到回等待页(30 秒自动重试)
    # 网页剪藏(2026-10-27):允许剪藏的内网网站(源);空 = 关闭。与导入容器读同一份 agent-config
    kb_clip_origins: Tuple[str, ...] = ()
    kb_clip_max_mb: int = 30
    # Fuyao 运行模式(RUNTIME_MODE=fuyao)
    fuyao_api: str = ""                     # OpenSandbox 地址,如 http://opensandbox-server.flux-system.svc:80
    fuyao_api_key: str = ""                 # 平台开认证后的 API Key(Secret,不进 ConfigMap)
    fuyao_runtime_template: str = ""
    fuyao_runtime_group: str = ""
    fuyao_import_template: str = ""
    fuyao_import_group: str = ""
    fuyao_cpu: str = "2"
    fuyao_memory: str = "4Gi"
    fuyao_sandbox_timeout: int = 0          # 平台的沙箱寿命;0 = 不设(平台不自动删)。>0 时清扫器在到期前收尾后删除
    fuyao_idle_seconds: int = 1800          # 闲置多久收尾并暂停
    fuyao_retain_days: int = 7              # 暂停多久后删除
    fuyao_pause: bool = True                # 平台不支持暂停时设 0:闲置即收尾后删除
    fuyao_sweep_seconds: int = 30
    fuyao_wait_seconds: int = 90
    # 沙箱里访问的两个集群服务:虚拟机的 DNS 解析不了集群域名(平台实测),网关下发前解析成 ClusterIP
    fuyao_state_url: str = ""
    fuyao_credproxy_url: str = ""
    # 对话页注入老浏览器兼容补丁(gateway/legacy.py;v1.5.3,现场 Edge 109 / Chromium 108 行内代码黑块)。默认开,新浏览器上不起作用
    legacy_compat: bool = True

    @property
    def trust_configured(self) -> bool:
        return bool(self.trust_secret or self.trust_cidrs)


def _int(env: Mapping[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError("%s=%r 不是整数" % (key, raw))
    if v < minimum:
        raise ConfigError("%s=%d 太小,至少 %d" % (key, v, minimum))
    return v


def _clip_origins(raw: str) -> Tuple[str, ...]:
    out: List[str] = []
    for item in re.split(r"[,\s]+", raw or ""):
        item = item.strip().rstrip("/")
        if not item:
            continue
        s = urlsplit(item)
        if s.scheme not in ("http", "https") or not s.hostname or s.path or s.query or s.fragment or "@" in s.netloc:
            raise ConfigError("KB_CLIP_ORIGINS(agent-config)里的 %r 不对:每项只写「协议://主机[:端口]」,"
                              "如 https://wiki.example.internal,不带路径" % item)
        o = ("%s://%s" % (s.scheme, s.netloc)).lower()
        if o not in out:
            out.append(o)
    return tuple(out)


def load(env: Mapping[str, str]) -> Config:
    ns = (env.get("GATEWAY_NAMESPACE") or "").strip()
    if not ns:
        raise ConfigError("缺 GATEWAY_NAMESPACE(网关要在哪个命名空间里建 Pod)")
    tag = (env.get("GATEWAY_IMAGE_TAG") or "").strip()
    if not tag:
        raise ConfigError("缺 GATEWAY_IMAGE_TAG(给用户 Pod 用哪个版本的镜像)——"
                          "不给默认值:默认成 latest 会让升级悄悄发生在下一次登录")

    # 镜像仓库前缀:原来没有这一项,用户 Pod 的镜像名固定是 gaussdb-agent-runtime:<TAG>。
    # 镜像推在内网仓库里时,节点会去 docker.io 拉这个名字 —— 用户一登录就卡 ImagePullBackOff(504),
    # 只有把镜像直接载到节点上的环境(kind load / docker load)才正常,所以一直没暴露。
    registry = (env.get("GATEWAY_IMAGE_REGISTRY") or "").strip().rstrip("/")
    if registry and ("://" in registry or any(c.isspace() for c in registry)):
        raise ConfigError("GATEWAY_IMAGE_REGISTRY=%r 不对:只写仓库前缀,不带 http:// 与空格,"
                          "例如 registry.example/gaussdb-agent" % registry)

    secret = (env.get("GATEWAY_TRUST_SECRET") or "").strip()
    cidrs = tuple(c.strip() for c in (env.get("GATEWAY_TRUST_CIDRS") or "").split(",") if c.strip())
    if not secret and not cidrs:
        raise ConfigError(
            "GATEWAY_TRUST_SECRET 与 GATEWAY_TRUST_CIDRS 一个都没配。"
            "网关按请求头里的工号决定进谁的环境,不限制来源就等于「任填工号即可进入任何人的环境」,"
            "而且从外面看不出异常。至少配一个:"
            "GATEWAY_TRUST_SECRET=<与上游 SSO 约定的共享密钥>,"
            "或 GATEWAY_TRUST_CIDRS=<上游 SSO / Ingress 的网段>。")
    if secret and len(secret) < 16:
        raise ConfigError("GATEWAY_TRUST_SECRET 太短(%d 字符),至少 16" % len(secret))

    mode = (env.get("GATEWAY_REAP_MODE") or reaper.MODE_SCALE).strip()
    if mode not in (reaper.MODE_DELETE, reaper.MODE_SCALE):
        raise ConfigError("GATEWAY_REAP_MODE=%r 不认识,只能是 %s / %s"
                          % (mode, reaper.MODE_DELETE, reaper.MODE_SCALE))

    # 闲置时长下限 300 秒:模型跑一个长任务几分钟不产生流量是正常的,
    # 配得比这还短会在用户等结果的时候把 Pod 收掉。
    idle = _int(env, "GATEWAY_IDLE_SECONDS", reaper.DEFAULT_IDLE_SECONDS, minimum=300)

    runtime_mode = (env.get("GATEWAY_RUNTIME_MODE") or RUNTIME_ONDEMAND).strip().lower()
    if runtime_mode not in RUNTIME_MODES:
        raise ConfigError("GATEWAY_RUNTIME_MODE=%r 不认识,只能是 %s" % (runtime_mode, " / ".join(RUNTIME_MODES)))
    pool_key = (env.get("GATEWAY_POOL_CONTROL_KEY") or "").strip()
    if runtime_mode == RUNTIME_POOL and len(pool_key) < 16:
        raise ConfigError("固定池化模式要 GATEWAY_POOL_CONTROL_KEY(Secret sandbox-control 的 KEY,与各 Pod 管家同一把),"
                          "没配或不足 16 个字符")
    if runtime_mode == RUNTIME_FUYAO and len(pool_key) < 16:
        raise ConfigError("Fuyao 模式要 GATEWAY_POOL_CONTROL_KEY(Secret sandbox-control 的 KEY,与状态服务、"
                          "凭据代理服务同一把,用来签发沙箱令牌),没配或不足 16 个字符")
    if runtime_mode == RUNTIME_FUYAO and not (env.get("GATEWAY_ADMIN_TOKEN") or "").strip():
        # 运维接口 /admin/fuyao(回写、暂停、删除,发版台读换版进度与删冒烟沙箱)全靠它;没有它接口整体 404,
        # 出了问题只能去平台上直接删沙箱(不回写、丢数据)—— 宁可不启动(2026-11-18)
        raise ConfigError("Fuyao 模式要 GATEWAY_ADMIN_TOKEN(Secret gateway-auth 的 ADMIN_TOKEN,运维接口 /admin/fuyao 与"
                          "发版台都要用),没配或为空:往 Secret gateway-auth 加 ADMIN_TOKEN(openssl rand -hex 24)后重启网关")
    short = (env.get(TEST_SHORT_TIMERS) or "").strip() == "1"
    clip_mb = _int(env, "GATEWAY_KB_CLIP_MAX_MB", 30, minimum=1)
    if clip_mb > KB_CLIP_MAX_MB_CEILING:
        raise ConfigError("KB_CLIP_MAX_MB=%d 太大,最多 %d(网关单请求上限 64 MB,图片按 base64 传要多三分之一)"
                          % (clip_mb, KB_CLIP_MAX_MB_CEILING))

    def low(n: int) -> int:
        return 5 if short else n
    idle = _int(env, "GATEWAY_POOL_IDLE_SECONDS", 1800, minimum=low(300))
    early = _int(env, "GATEWAY_POOL_EARLY_IDLE_SECONDS", 600, minimum=low(60))
    if early > idle:
        raise ConfigError("GATEWAY_POOL_EARLY_IDLE_SECONDS=%d 比 GATEWAY_POOL_IDLE_SECONDS=%d 还长:"
                          "满员时提前转保留就永远轮不到" % (early, idle))

    fy = _fuyao(env, runtime_mode == RUNTIME_FUYAO, low, ns)
    compat = (env.get("GATEWAY_LEGACY_COMPAT") or "1").strip().lower()
    if compat not in ("1", "0", "true", "false", "yes", "no"):
        raise ConfigError("GATEWAY_LEGACY_COMPAT=%r 不认识,只能是 1 / 0" % compat)
    product = (env.get("GATEWAY_PRODUCT_NAME") or "").strip() or DEFAULT_PRODUCT_NAME
    if len(product) > PRODUCT_NAME_MAX or any(ord(c) < 32 or c in "<>\"'&" for c in product):
        raise ConfigError("PRODUCT_NAME=%r 不对:至多 %d 个字符,不含控制字符与 < > \" ' &" % (product, PRODUCT_NAME_MAX))

    return Config(
        namespace=ns, image_tag=tag, image_registry=registry,
        pull_policy=(env.get("GATEWAY_PULL_POLICY") or "IfNotPresent").strip(),
        port=_int(env, ENV_PORT, DEFAULT_PORT, minimum=1),
        user_header=(env.get("GATEWAY_USER_HEADER") or DEFAULT_USER_HEADER).strip(),
        roles_header=(env.get("GATEWAY_ROLES_HEADER") or DEFAULT_ROLES_HEADER).strip(),
        trust_secret=secret, trust_cidrs=cidrs,
        reap_mode=mode, idle_seconds=idle,
        sweep_seconds=_int(env, "GATEWAY_SWEEP_SECONDS", reaper.DEFAULT_SWEEP_SECONDS, minimum=10),
        activity_interval_s=_int(env, "GATEWAY_ACTIVITY_INTERVAL_SECONDS", 60, minimum=5),
        ready_timeout_s=_int(env, "GATEWAY_READY_TIMEOUT_SECONDS", 120, minimum=10),
        kb_admin_roles=tuple(r.strip().lower() for r in
                             (env.get("GATEWAY_KB_ADMIN_ROLES") or "kb-admin").split(",") if r.strip()),
        ops_admin_roles=tuple(r.strip().lower() for r in
                              (env.get("GATEWAY_OPS_ADMIN_ROLES") or "ops-admin").split(",") if r.strip()),
        product_name=product,
        admin_token=(env.get("GATEWAY_ADMIN_TOKEN") or "").strip(),
        frontend_service=(env.get("GATEWAY_FRONTEND_SERVICE") or "frontend").strip(),
        runtime_mode=runtime_mode,
        pool_selector=(env.get("GATEWAY_POOL_SELECTOR") or "app=gaussdb-agent,role=pool").strip(),
        pool_control_key=pool_key,
        # 闲置转保留下限 300 秒,理由与按需模式的闲置回收相同:模型跑长任务时几分钟没有交互是正常的
        pool_idle_seconds=idle, pool_early_idle_seconds=early,
        pool_retain_days=_int(env, "GATEWAY_POOL_RETAIN_DAYS", 7, minimum=1),
        pool_free_slots=_int(env, "GATEWAY_POOL_FREE_SLOTS", 1, minimum=0),
        pool_sweep_seconds=_int(env, "GATEWAY_POOL_SWEEP_SECONDS", 30, minimum=5),
        pool_wait_seconds=_int(env, "GATEWAY_POOL_WAIT_SECONDS", 60, minimum=low(10)),
        kb_clip_origins=_clip_origins(env.get("GATEWAY_KB_CLIP_ORIGINS") or ""),
        kb_clip_max_mb=clip_mb,
        legacy_compat=compat in ("1", "true", "yes"),
        **fy,
    )


def _http_url(key: str, raw: str) -> str:
    u = urlsplit(raw)
    if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment or "@" in u.netloc:
        raise ConfigError("%s=%r 不对:要写成 http(s)://主机[:端口]" % (key, raw))
    return raw.rstrip("/")


# 两个集群服务的默认地址:按网关所在的命名空间推导(k8s/fuyao 的 Service 与网关在同一个命名空间)。
# 换命名空间部署时不用再手工改这两项;显式填了的照用(2026-11-18)
STATE_URL_DEFAULT = "http://statesvc.%s.svc:8090"
CREDPROXY_URL_DEFAULT = "http://credproxy.%s.svc:8091"


def _fuyao(env: Mapping[str, str], on: bool, low, ns: str = "gaussdb-agent") -> dict:
    """Fuyao 模式的配置。不在这个模式时只解析、不强制;在这个模式时缺一项都拒绝启动。
    状态服务、凭据代理服务的地址没填时按命名空间 ns 推导。"""
    out = {
        "fuyao_api": (env.get("GATEWAY_FUYAO_API") or "").strip(), "fuyao_api_key": (env.get("GATEWAY_FUYAO_API_KEY") or "").strip(),
        "fuyao_runtime_template": (env.get("GATEWAY_FUYAO_RUNTIME_TEMPLATE") or "").strip(), "fuyao_runtime_group": (env.get("GATEWAY_FUYAO_RUNTIME_GROUP") or "").strip(),
        "fuyao_import_template": (env.get("GATEWAY_FUYAO_IMPORT_TEMPLATE") or "").strip(), "fuyao_import_group": (env.get("GATEWAY_FUYAO_IMPORT_GROUP") or "").strip(),
        "fuyao_cpu": (env.get("GATEWAY_FUYAO_CPU") or "2").strip(), "fuyao_memory": (env.get("GATEWAY_FUYAO_MEMORY") or "4Gi").strip(),
        "fuyao_state_url": (env.get("GATEWAY_FUYAO_STATE_URL") or "").strip() or STATE_URL_DEFAULT % ns,
        "fuyao_credproxy_url": (env.get("GATEWAY_FUYAO_CREDPROXY_URL") or "").strip() or CREDPROXY_URL_DEFAULT % ns,
    }
    pause = (env.get("GATEWAY_FUYAO_PAUSE") or "1").strip().lower()
    if pause not in ("1", "0", "true", "false", "yes", "no"):
        raise ConfigError("GATEWAY_FUYAO_PAUSE=%r 不认识,只能是 1 / 0" % pause)
    out["fuyao_pause"] = pause in ("1", "true", "yes")
    out["fuyao_sandbox_timeout"] = _int(env, "GATEWAY_FUYAO_SANDBOX_TIMEOUT", 0)
    if 0 < out["fuyao_sandbox_timeout"] < 1800:
        raise ConfigError("GATEWAY_FUYAO_SANDBOX_TIMEOUT=%d 太小:0 表示不设,否则至少 1800(到期前 15 分钟要收尾)"
                          % out["fuyao_sandbox_timeout"])
    out["fuyao_idle_seconds"] = _int(env, "GATEWAY_FUYAO_IDLE_SECONDS", 1800, minimum=low(300))
    out["fuyao_retain_days"] = _int(env, "GATEWAY_FUYAO_RETAIN_DAYS", 7, minimum=1)
    out["fuyao_sweep_seconds"] = _int(env, "GATEWAY_FUYAO_SWEEP_SECONDS", 30, minimum=5)
    out["fuyao_wait_seconds"] = _int(env, "GATEWAY_FUYAO_WAIT_SECONDS", 90, minimum=low(10))
    if not on:
        return out
    missing = [k for k, v in (("GATEWAY_FUYAO_API", out["fuyao_api"]),
                              ("GATEWAY_FUYAO_RUNTIME_TEMPLATE", out["fuyao_runtime_template"]),
                              ("GATEWAY_FUYAO_RUNTIME_GROUP", out["fuyao_runtime_group"]),
                              ("GATEWAY_FUYAO_IMPORT_TEMPLATE", out["fuyao_import_template"]),
                              ("GATEWAY_FUYAO_IMPORT_GROUP", out["fuyao_import_group"])) if not v]
    if missing:
        raise ConfigError("Fuyao 模式缺配置:%s(见《参数手册》Fuyao 一节)" % "、".join(missing))
    out["fuyao_api"] = _http_url("GATEWAY_FUYAO_API", out["fuyao_api"])
    out["fuyao_state_url"] = _http_url("GATEWAY_FUYAO_STATE_URL", out["fuyao_state_url"])
    out["fuyao_credproxy_url"] = _http_url("GATEWAY_FUYAO_CREDPROXY_URL", out["fuyao_credproxy_url"])
    for k in ("fuyao_runtime_template", "fuyao_import_template"):
        if not _TEMPLATE_RE.match(out[k]):
            raise ConfigError("%s=%r 不像模板名" % ("GATEWAY_" + k.upper(), out[k]))
    for k in ("fuyao_runtime_group", "fuyao_import_group"):
        if not _K8S_NAME_RE.match(out[k]):
            raise ConfigError("%s=%r 不是合法的沙箱组名(小写字母、数字、连字符)" % ("GATEWAY_" + k.upper(), out[k]))
    for k, name in (("fuyao_cpu", "GATEWAY_FUYAO_CPU"), ("fuyao_memory", "GATEWAY_FUYAO_MEMORY")):
        if not _QUANTITY_RE.match(out[k]):
            raise ConfigError("%s=%r 不是合法的资源量(如 2、500m、4Gi)" % (name, out[k]))
    return out
