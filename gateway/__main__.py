"""网关入口:`python3 -m gateway`。

启动顺序有意为之:**先把配置全部校验完再碰 k8s**。配置不全时要在启动这一刻就
失败退出,而不是跑起来之后在第一个用户请求上才暴露 —— 那时候看起来像「用户进不去」,
排查方向会被带到 SSO 那边去。
"""
from __future__ import annotations

import pathlib
import signal
import sys

from . import config as gwconfig
from . import kube as gwkube
from . import pods, reaper, server


def grmp_lookup(mgr: pods.Manager):
    """固定池化按人下发 GRMP 令牌:读 Secret grmp-token-<工号>。没有就当场说清楚(与按需模式同一条规矩)。"""
    import base64

    def lookup(user_id: str) -> str:
        sec = mgr.kube.get(pods.CORE, "secrets", "grmp-token-%s" % user_id)
        raw = ((sec or {}).get("data") or {}).get("GRMP_AUTH_TOKEN")
        if not raw:
            raise pods.PrerequisiteMissing(
                "缺 Secret grmp-token-%s(或其中没有 GRMP_AUTH_TOKEN)—— 该用户本人的 GRMP 中间件令牌,"
                "由平台在开通时创建,网关不创建它。" % user_id)
        return base64.b64decode(raw).decode("utf-8")
    return lookup


def start_pool(cfg, kube, mgr, app):
    from . import poolalloc, poolclient, poolrouter, poolsweeper, pooltable
    client = poolclient.StewardClient(cfg.pool_control_key.encode("utf-8"))
    table = pooltable.PoolTable(kube, client, selector=cfg.pool_selector, log=app.log)
    policy = poolalloc.Policy(idle_seconds=cfg.pool_idle_seconds, early_idle_seconds=cfg.pool_early_idle_seconds,
                              retain_days=cfg.pool_retain_days, free_slots_per_pod=cfg.pool_free_slots)
    router = poolrouter.PoolRouter(table, client, policy, grmp_lookup(mgr), wait_s=cfg.pool_wait_seconds, log=app.log)
    sweeper = poolsweeper.PoolSweeper(table, client, router, policy, sweep_s=cfg.pool_sweep_seconds, log=app.log)
    app.pool, app.pool_admin = router, poolsweeper.PoolAdmin(table, client, router)
    sweeper.start()
    return sweeper


def agent_config_env(kube, ttl_s: float = 60.0):
    """沙箱启动时要的非秘密配置:ConfigMap agent-config 的全部键(模型、GRMP 的地址与签名口径等)。
    按需模式里它们是 Pod 的环境变量;沙箱建好后由启动器经 envd 拿到。缓存 60 秒,改了 ConfigMap 一分钟内生效。"""
    import threading
    import time
    lock, cache = threading.Lock(), {"at": 0.0, "data": {}}

    def get() -> dict:
        with lock:
            if time.time() - cache["at"] < ttl_s:
                return dict(cache["data"])
            cm = kube.get(pods.CORE, "configmaps", "agent-config") or {}
            cache["data"] = {str(k): str(v) for k, v in (cm.get("data") or {}).items()}
            cache["at"] = time.time()
            return dict(cache["data"])
    return get


def ip_url(url: str, resolve=None) -> str:
    """http://域名:端口 → http://IP:端口(网关所在的 Pod 能解析集群域名,沙箱不能)。已经是 IP 时原样返回;
    解析失败抛 OSError —— 带着域名下发下去,沙箱里只会报一个看不懂的连接失败。"""
    import socket
    from urllib.parse import urlsplit, urlunsplit
    u = urlsplit(url)
    host = u.hostname or ""
    if not host:
        raise OSError("地址里没有主机名:%r" % url)
    try:
        socket.inet_aton(host)
        return url
    except OSError:
        pass
    ip = (resolve or socket.gethostbyname)(host)
    return urlunsplit((u.scheme, "%s:%d" % (ip, u.port) if u.port else ip, u.path, u.query, u.fragment))


LOCATE_FRESH_S = 30                 # 沙箱每 5 秒报到一次(fuyao-syncloop);超过这么久没见到就不信这个地址


def state_locator(state_url: str, key: bytes, timeout: float = 3.0):
    """问状态服务:这台沙箱最近一次请求从哪个地址来(恢复后平台回的地址会过期)。只认 LOCATE_FRESH_S 内的,
    返回 (地址, 距今秒数) 或 None —— 路由器要用距今秒数判断这条记录是不是恢复之后才报上来的。"""
    import http.client
    import json
    from urllib.parse import urlsplit
    from . import sbxtoken

    def locate(sandbox_id: str, user: str, kind: str):
        u = urlsplit(ip_url(state_url))
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        try:
            conn.request("GET", "/v1/where", headers={
                "Authorization": "Bearer " + sbxtoken.issue(key, sandbox_id, user, kind)})
            r = conn.getresponse()
            body = r.read()
        finally:
            conn.close()
        if r.status != 200:
            return None
        got = json.loads(body or b"{}")
        age = float(got.get("age", 1e9))
        return (got["address"], age) if got.get("address") and age <= LOCATE_FRESH_S else None
    return locate


def start_fuyao(cfg, kube, mgr, app):
    from . import fuyaorouter, fuyaosweeper, opensandbox, sandboxexec
    client = opensandbox.Client(cfg.fuyao_api, api_key=cfg.fuyao_api_key)
    policy = fuyaorouter.Policy(
        templates={pods.KIND_RUNTIME: cfg.fuyao_runtime_template, pods.KIND_KB_IMPORT: cfg.fuyao_import_template},
        groups={pods.KIND_RUNTIME: cfg.fuyao_runtime_group, pods.KIND_KB_IMPORT: cfg.fuyao_import_group},
        cpu=cfg.fuyao_cpu, memory=cfg.fuyao_memory, sandbox_timeout_s=cfg.fuyao_sandbox_timeout,
        idle_s=cfg.fuyao_idle_seconds, retain_s=cfg.fuyao_retain_days * 86400, pause=cfg.fuyao_pause,
        wait_s=float(cfg.fuyao_wait_seconds))
    base_env = agent_config_env(kube)
    lookup = grmp_lookup(mgr)

    def launch_env(user_id: str, kind: str) -> dict:
        env = base_env()
        # 沙箱(虚拟机)的 DNS 解析不了集群域名(平台实测):配置里可以写 Service 域名,这里换成 ClusterIP 再下发
        try:
            env.update({"GSDB_STATE_URL": ip_url(cfg.fuyao_state_url),
                        "GSDB_CREDPROXY_URL": ip_url(cfg.fuyao_credproxy_url)})
        except OSError as exc:
            raise fuyaorouter.LaunchError("解析状态服务 / 凭据代理服务地址失败:%s" % exc)
        return env

    def prereq(user_id: str, kind: str) -> None:
        # 诊断环境要本人的 GRMP 令牌(凭据代理服务按工号读它);缺了当场说清楚,与按需 / 池化同一条规矩
        if kind == pods.KIND_RUNTIME:
            lookup(user_id)

    key = cfg.pool_control_key.encode("utf-8")
    router = fuyaorouter.FuyaoRouter(client, sandboxexec.Envd(), policy, key,
                                     password_for=mgr.ensure_password, launch_env=launch_env, prereq=prereq,
                                     log=app.log, locate=state_locator(cfg.fuyao_state_url, key))
    # 资源大盘:请求计数每 30 秒报给状态服务,清扫器每轮报运行 / 暂停台数(运维签名,沙箱调不到这几个接口)
    from . import opsreport
    ops = opsreport.StateOps(cfg.fuyao_state_url, key)
    app.state_ops, app.counter = ops, opsreport.RequestCounter()
    app.reporter = opsreport.Reporter(app.counter, ops, log=app.log)
    app.reporter.start()
    sweeper = fuyaosweeper.FuyaoSweeper(router, sweep_s=cfg.fuyao_sweep_seconds, log=app.log,
                                        report=opsreport.fleet_reporter(ops, app.log))
    app.pool, app.fuyao_admin = router, sweeper
    sweeper.start()
    return sweeper


def main(argv=None) -> int:
    import os
    try:
        cfg = gwconfig.load(os.environ)
    except gwconfig.ConfigError as exc:
        print("配置错误,网关拒绝启动:%s" % exc, file=sys.stderr)
        return 2

    try:
        kube = gwkube.Kube.in_cluster(cfg.namespace)
    except gwkube.KubeError as exc:
        print("拿不到 k8s 访问凭据:%s" % exc, file=sys.stderr)
        return 2

    templates = pathlib.Path(os.environ.get("GATEWAY_TEMPLATES") or "/opt/gateway/templates")
    missing = [k for k in ("runtime", "kb-import") if not (templates / ("%s.yaml" % k)).is_file()]
    if missing:
        print("缺 Pod 模板 %s(找的是 %s)" % (missing, templates), file=sys.stderr)
        return 2

    mgr = pods.Manager(kube, namespace=cfg.namespace, image_tag=cfg.image_tag,
                       pull_policy=cfg.pull_policy, templates=templates,
                       activity_interval_s=cfg.activity_interval_s, image_registry=cfg.image_registry,
                       runtime_on_demand=cfg.runtime_mode == gwconfig.RUNTIME_ONDEMAND)
    app = server.App(cfg, mgr)
    sweeper = None
    if cfg.runtime_mode == gwconfig.RUNTIME_POOL:
        sweeper = start_pool(cfg, kube, mgr, app)
    elif cfg.runtime_mode == gwconfig.RUNTIME_FUYAO:
        sweeper = start_fuyao(cfg, kube, mgr, app)
    from . import dashdata
    app.dash = dashdata.Collector(app, ops=app.state_ops)      # 资源大盘(/dash/api/resources);非 Fuyao 模式只有组件那几块
    # 按需模式回收闲置的用户 Pod;固定池化下只剩知识库管理员的导入容器是按需建的,照样由它回收
    rp = reaper.Reaper(mgr, mode=cfg.reap_mode, idle_s=cfg.idle_seconds,
                       sweep_s=cfg.sweep_seconds, log=app.log)
    rp.start()

    httpd = server.serve(app)
    app.log("网关就绪:0.0.0.0:%d · 命名空间 %s · 用户镜像标签 %s · 诊断容器 %s · 回收 %s(闲置 %d 秒)· "
            "信任来源 %s · 控制 API %s"
            % (cfg.port, cfg.namespace, cfg.image_tag,
               ("Fuyao 沙箱(%s / %s)" % (cfg.fuyao_runtime_group, cfg.fuyao_import_group)
                if cfg.runtime_mode == gwconfig.RUNTIME_FUYAO else
                "固定池化(%s)" % cfg.pool_selector if sweeper is not None else "按需建"),
               cfg.reap_mode, cfg.idle_seconds,
               "共享密钥" + ("+网段" if cfg.trust_cidrs else "") if cfg.trust_secret else "网段",
               "已启用" if cfg.admin_token else "未启用"))

    def stop(_signum, _frame):
        app.log("收到停止信号,正在退出(不回收任何用户 Pod)")
        rp.stop()
        if sweeper is not None:
            sweeper.stop()
        if getattr(app, "reporter", None) is not None:
            app.reporter.stop()                     # 把最后攒下的请求计数报出去
        # 另起一个线程关,避免在信号处理里 join 自己
        import threading
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
