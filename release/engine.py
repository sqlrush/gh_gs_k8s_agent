"""执行引擎:一次发布 = 一串步骤,逐个跑;关卡、暂停、失败续跑、重启后接着来。

- 步骤函数 `fn(ctx)`:**必须幂等**(续跑会从头再跑这一步,只补做没做完的部分)。
- 关卡 `ctx.gate(kind, payload)` 持久化在状态库,阻塞到运维在界面上选了为止;发版台重启后照样停在那里。
- 暂停:在步骤之间生效;步骤里的长等待调 `ctx.check()`,在那里也能停下(这一步回到「未开始」,续跑时重跑)。
- 失败:停在这一步,运维选「从这一步续跑」或「回滚」(回滚是另一次发布,目标是上一个版本)。
同一时刻只跑一次发布。
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .store import Store


class Paused(Exception):
    pass


class Cancelled(Exception):
    pass


class StepFailed(Exception):
    """步骤里可预期的失败:消息直接给运维看(不带堆栈)。"""


@dataclass
class Ctx:
    """传给步骤函数的上下文。shared:同一次发布各步之间传数据(渲染结果路径、镜像 digest …),持久化在状态库。"""
    engine: "Engine"
    run_id: int
    idx: int
    step: str
    run: dict
    services: Dict[str, Any] = field(default_factory=dict)

    @property
    def store(self) -> Store:
        return self.engine.store

    @property
    def plan(self) -> dict:
        return self.run["plan_meta"]

    def log(self, text: str, level: str = "") -> None:
        self.store.log(self.run_id, self.step, level, text)

    def sub(self, k: int, st: str, note: Optional[str] = None) -> None:
        r = self.store.run(self.run_id)
        subs = r["steps"][self.idx]["subs"]
        if 0 <= k < len(subs):
            subs[k] = dict(subs[k], st=st, **({"note": note} if note is not None else {}))
            self.store.update_step(self.run_id, self.idx, subs=subs)

    def detail(self, text: str) -> None:
        self.store.update_step(self.run_id, self.idx, detail=text)

    def shared(self) -> Dict[str, Any]:
        return self.store.kv_get("run:%d" % self.run_id, {})

    def share(self, **kv: Any) -> None:
        self.store.kv_set("run:%d" % self.run_id, dict(self.shared(), **kv))

    def check(self) -> None:
        self.engine.checkpoint(self.run_id)

    def gate(self, kind: str, payload: Optional[dict] = None) -> str:
        """开一个关卡并等运维选择。本步骤已有同类、还没被取走的关卡(重启后续跑)就用那一个 —— 运维可能在新线程
        开始等之前就点了,那时直接取走结果,不能再开一个新的让他重点。取走即「消费」,白名单那种循环每轮各开各的。"""
        g = self.store.pending_gate(self.run_id, self.step, kind)
        gid = g["id"] if g else self.store.add_gate(self.run_id, self.step, kind, payload)
        self.store.update_run(self.run_id, status="waiting")
        self.store.update_step(self.run_id, self.idx, status="waiting")
        self.log("等待运维处理:%s" % kind, "w")
        while True:
            got = self.store.gate(gid)
            if got and got["resolved"]:
                self.store.consume_gate(gid)
                self.store.update_run(self.run_id, status="running")
                self.store.update_step(self.run_id, self.idx, status="running")
                self.store.audit("关卡 · %s" % kind, "发布 #%d %s" % (self.run_id, self.step), got["choice"])
                if got["choice"] == "cancel":
                    raise Cancelled("运维在「%s」关卡上取消了发布" % kind)
                return got["choice"]
            self.engine.checkpoint(self.run_id, waiting=True)
            self.engine.sleep(self.engine.poll_s)


StepFn = Callable[[Ctx], None]


class Engine:
    def __init__(self, store: Store, steps: Dict[str, StepFn], services: Optional[Dict[str, Any]] = None,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 1.0, clock: Callable[[], float] = time.time):
        self.store, self.steps, self.services = store, steps, services or {}
        self.sleep, self.poll_s, self.clock = sleep, poll_s, clock
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._pause: Dict[int, bool] = {}

    # -- 对外 --------------------------------------------------------------------------------------

    def start(self, kind: str, from_v: str, to_v: str, steps: List[dict], meta: dict, config_version: Optional[int]) -> int:
        with self._lock:
            if self.busy():
                raise RuntimeError("已有一次发布在进行中(#%d)" % self.store.active_run())
            rid = self.store.create_run(kind, from_v, to_v, steps, config_version)
            self.store.kv_set("run:%d:meta" % rid, meta)
            self.store.audit("开始发布", "#%d %s → %s(%s)" % (rid, from_v or "无", to_v, kind))
            self._spawn(rid)
            return rid

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def pause(self, rid: int) -> None:
        self._pause[rid] = True
        self.store.audit("暂停", "#%d" % rid)

    def resume(self, rid: int) -> None:
        """暂停 / 失败 / 被中断之后接着跑:从第一个没完成的步骤开始(失败的那步重跑)。"""
        with self._lock:
            if self.busy():
                raise RuntimeError("发布还在进行中")
            r = self.store.run(rid)
            if not r or r["status"] not in ("paused", "failed", "waiting"):
                raise RuntimeError("发布 #%d 现在是「%s」,不能续跑" % (rid, (r or {}).get("status")))
            for s in r["steps"]:
                if s["status"] in ("failed", "interrupted", "paused"):
                    self.store.update_step(rid, s["idx"], status="pending", error="")
            self._pause.pop(rid, None)
            self.store.update_run(rid, status="running", error="")
            self.store.audit("续跑", "#%d" % rid)
            self._spawn(rid)

    def resolve(self, rid: int, choice: str) -> None:
        g = self.store.open_gate(rid)
        if not g:
            raise RuntimeError("发布 #%d 没有待处理的关卡" % rid)
        self.store.resolve_gate(g["id"], choice)

    def checkpoint(self, rid: int, waiting: bool = False) -> None:
        if self._pause.get(rid):
            raise Paused()

    def wait(self, timeout: float = 30) -> None:
        if self._thread:
            self._thread.join(timeout)

    # -- 跑 ----------------------------------------------------------------------------------------

    def _spawn(self, rid: int) -> None:
        self._thread = threading.Thread(target=self._run, args=(rid,), name="release-%d" % rid, daemon=True)
        self._thread.start()

    def _run(self, rid: int) -> None:
        self.store.update_run(rid, status="running")
        r = self.store.run(rid)
        r["plan_meta"] = self.store.kv_get("run:%d:meta" % rid, {})
        for s in r["steps"]:
            if s["status"] in ("done", "skipped"):
                continue
            if self._pause.get(rid):
                return self._paused(rid, None)
            idx, sid = s["idx"], s["step"]
            fn = self.steps.get(sid)
            if fn is None:
                return self._fail(rid, idx, "发版台没有「%s」这一步的实现" % sid)
            self.store.update_step(rid, idx, status="running", started=s["started"] or self.clock(), tries=(s["tries"] or 0) + 1)
            ctx = Ctx(engine=self, run_id=rid, idx=idx, step=sid, run=r, services=self.services)
            try:
                fn(ctx)
            except Paused:
                return self._paused(rid, idx)
            except Cancelled as exc:
                self.store.update_step(rid, idx, status="failed", error=str(exc), finished=self.clock())
                self.store.update_run(rid, status="cancelled", error=str(exc), finished=self.clock())
                self.store.log(rid, sid, "e", str(exc))
                return None
            except StepFailed as exc:
                return self._fail(rid, idx, str(exc))
            except Exception as exc:                # noqa: BLE001  未预期的错:日志里留堆栈,界面只给一句
                self.store.log(rid, sid, "e", traceback.format_exc(limit=6))
                return self._fail(rid, idx, "%s:%s" % (type(exc).__name__, exc))
            self.store.update_step(rid, idx, status="done", finished=self.clock())
        self.store.update_run(rid, status="done", finished=self.clock())
        self.store.audit("发布完成", "#%d → %s" % (rid, r["to_v"]))

    def _paused(self, rid: int, idx: Optional[int]) -> None:
        if idx is not None:
            self.store.update_step(rid, idx, status="paused")
        self.store.update_run(rid, status="paused")
        self.store.log(rid, "", "w", "已暂停")

    def _fail(self, rid: int, idx: int, msg: str) -> None:
        sid = self.store.run(rid)["steps"][idx]["step"]
        self.store.update_step(rid, idx, status="failed", error=msg, finished=self.clock())
        self.store.update_run(rid, status="failed", error=msg)
        self.store.log(rid, sid, "e", "失败:" + msg)
