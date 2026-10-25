"""闲置回收 —— 网关最容易做错的一块。

「闲置」的定义不直观,三个坑都是查出来或踩过的:

① **不能按「无新请求」判。** opencode 的事件流(SSE)每 10 秒发一次 server.heartbeat
   (2026-09-21 实测,连续 75 秒零遗漏)。用户开着页面发呆时有字节流动但没有新请求,
   按请求计数判闲置会把正在用的人的 Pod 收掉。所以活动时间由代理层在**转发字节时**
   更新(pods.Manager.touch),这里只读结果。
② **刚建好的 Pod 没有活动记录。** 注解是空的,不能当成「1970 年就没动过」立刻收掉,
   否则用户登录后 Pod 刚起来就被杀。没有记录时按 creationTimestamp 算。
③ **时间读不懂时不回收。** 误留一个 Pod 只是占资源;误杀会打断正在干活的人。

宽限期默认 30 分钟:模型跑一个长任务可能几分钟不产生任何流量(它在等 LLM 回包),
宽限必须明显大于单次回合的时长。

两种模式(k8s-deploy.md §4):
  delete  删 Deployment/Service/Secret,下次登录重建。NAS 数据在,不需要「还原」。
  scale   缩到 0 副本,重连再拉起来。省掉镜像拉取与冷启动,代价是占着 Deployment。
"""
from __future__ import annotations

import calendar
import threading
import time
from typing import Any, Dict, Optional, Tuple

DEFAULT_IDLE_SECONDS = 1800          # 30 分钟
DEFAULT_SWEEP_SECONDS = 120

ANNOTATION_ACTIVITY = "gaussdb-agent/last-activity"
MODE_DELETE = "delete"
MODE_SCALE = "scale"

_PREFIXES = ("runtime-", "kb-import-")


def parse(deployment_name: str) -> Optional[Tuple[str, str]]:
    """Deployment 名 → (形态, 工号)。不是用户 Pod(gateway / frontend 之类)返回 None。"""
    for p in _PREFIXES:
        if deployment_name.startswith(p) and len(deployment_name) > len(p):
            return p[:-1], deployment_name[len(p):]
    return None


def user_of(deployment_name: str) -> Optional[str]:
    """从 Deployment 名反推工号。不是用户 Pod 返回 None。"""
    got = parse(deployment_name)
    return got[1] if got else None


def _parse_rfc3339(text: str) -> Optional[float]:
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def last_seen(dep: Dict[str, Any]) -> Optional[float]:
    """这个 Pod 最后一次「有动静」是什么时候。读不出来返回 None(调用方据此保守处理)。"""
    meta = dep.get("metadata") or {}
    raw = (meta.get("annotations") or {}).get(ANNOTATION_ACTIVITY)
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass                      # 注解坏了 → 退到创建时间,不要直接判成很久没动
    return _parse_rfc3339(meta.get("creationTimestamp") or "")


def should_reap(dep: Dict[str, Any], now: float, idle_s: int) -> bool:
    if int((dep.get("spec") or {}).get("replicas") or 0) <= 0:
        return False                  # scale 模式下已经缩到 0,不用再处理
    seen = last_seen(dep)
    if seen is None:
        return False                  # 时间读不懂 → 留着。误杀比误留贵得多
    return (now - seen) > idle_s


class Reaper:
    """后台线程:每隔一段时间扫一遍,把闲置的收掉。

    状态全在 k8s 对象上,所以网关 2 副本同时扫也只是重复做同一件幂等操作;
    不需要选主。
    """

    def __init__(self, manager, mode: str = MODE_SCALE, idle_s: int = DEFAULT_IDLE_SECONDS,
                 sweep_s: int = DEFAULT_SWEEP_SECONDS, log=print):
        if mode not in (MODE_DELETE, MODE_SCALE):
            raise ValueError("回收模式只能是 %s / %s,拿到 %r" % (MODE_DELETE, MODE_SCALE, mode))
        self.m = manager
        self.mode = mode
        self.idle_s = idle_s
        self.sweep_s = sweep_s
        self._log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def sweep(self, now: Optional[float] = None) -> int:
        """扫一遍,返回处理掉的人数。异常不外抛 —— 一个用户的问题不能让整个回收停摆。"""
        now = time.time() if now is None else now
        done = 0
        try:
            items = (self.m.kube.list("/apis/apps/v1", "deployments").get("items") or [])
        except Exception as exc:                        # noqa: BLE001
            self._log("回收扫描失败(下一轮再试):%s" % exc)
            return 0
        for dep in items:
            name = ((dep.get("metadata") or {}).get("name") or "")
            got = parse(name)
            if not got or not should_reap(dep, now, self.idle_s):
                continue
            kind, uid = got
            try:
                if self.mode == MODE_DELETE:
                    # **只删闲置的这一个**(Deployment + Service)。以前调 remove(工号):两个容器连同口令与
                    # 平台签发的 grmp-token 一起删 —— 管理员的导入容器一闲置,正在用的诊断容器跟着没了,
                    # 下次登录还因缺令牌报「尚未开通」(2026-10-16 抓到)。
                    self.m.retire(uid, kind)
                    self._log("闲置回收:已删除 %s(NAS 数据、口令与令牌保留,下次登录重建)" % name)
                else:
                    self.m.kube.patch("/apis/apps/v1", "deployments", name, {"spec": {"replicas": 0}})
                    self._log("闲置回收:已缩容 %s 到 0 副本" % name)
                done += 1
            except Exception as exc:                    # noqa: BLE001
                self._log("回收 %s 失败(下一轮再试):%s" % (name, exc))
        return done

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.sweep_s):
                self.sweep()
        self._thread = threading.Thread(target=loop, name="reaper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
