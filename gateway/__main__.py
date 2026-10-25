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
                       runtime_on_demand=cfg.runtime_mode != gwconfig.RUNTIME_POOL)
    app = server.App(cfg, mgr)
    sweeper = None
    if cfg.runtime_mode == gwconfig.RUNTIME_POOL:
        sweeper = start_pool(cfg, kube, mgr, app)
    # 按需模式回收闲置的用户 Pod;固定池化下只剩知识库管理员的导入容器是按需建的,照样由它回收
    rp = reaper.Reaper(mgr, mode=cfg.reap_mode, idle_s=cfg.idle_seconds,
                       sweep_s=cfg.sweep_seconds, log=app.log)
    rp.start()

    httpd = server.serve(app)
    app.log("网关就绪:0.0.0.0:%d · 命名空间 %s · 用户镜像标签 %s · 诊断容器 %s · 回收 %s(闲置 %d 秒)· "
            "信任来源 %s · 控制 API %s"
            % (cfg.port, cfg.namespace, cfg.image_tag,
               "固定池化(%s)" % cfg.pool_selector if sweeper is not None else "按需建",
               cfg.reap_mode, cfg.idle_seconds,
               "共享密钥" + ("+网段" if cfg.trust_cidrs else "") if cfg.trust_secret else "网段",
               "已启用" if cfg.admin_token else "未启用"))

    def stop(_signum, _frame):
        app.log("收到停止信号,正在退出(不回收任何用户 Pod)")
        rp.stop()
        if sweeper is not None:
            sweeper.stop()
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
