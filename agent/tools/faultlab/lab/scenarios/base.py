"""场景的共同骨架与注册表。

一个场景要回答三件事:
  · start()  —— 怎么把现象造出来(开几条会话、各干什么)
  · stop()   —— 怎么收手(自己造的东西自己撤;撤不干净的要登记进 state,交给 cleanup)
  · probe()  —— **现在这个现象在库里看不看得到**(一行字)

**probe 是这套东西能不能用的关键。** 没有它,「场景生效了」和「场景悄悄失败了」
在终端上长得一模一样,而人往往要等到用 skill 查不出东西时才发现,这时已经分不清是
skill 漏报还是故障根本没造出来。所以每个场景都必须能自证。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Type

from .. import config as C
from ..db import REAL_SESSION, Conn
from ..session import Ctx, Worker, app_name, stop_workers


class Base:
    """场景基类。子类至少要实现 start() 与 probe()。"""

    code = ""
    group = ""

    def __init__(self, ctx: Ctx, meta: Optional[C.Scenario] = None):
        self.ctx = ctx
        self.meta = meta
        # 一个类可以实现多个编号(七条慢 SQL 共用一套跑法),所以编号跟着排期表走。
        if meta is not None:
            self.code = meta.code
            self.group = meta.group
        self.workers: List[Worker] = []

    # -- 子类实现 -----------------------------------------------------------

    def start(self) -> None:
        raise NotImplementedError

    def probe(self, admin: Conn) -> str:
        """一行字:现在这个现象看不看得到。查不到就如实说查不到。"""
        raise NotImplementedError

    # -- 公共 ---------------------------------------------------------------

    def worker(self, role: str, fn: Callable, **kw) -> Worker:
        """开一条会话并登记,stop() 时统一收。

        等锁上限默认按本场景的时段收紧(config.lock_guard_s):停场景那一步万一没打断它,
        数据库也会在时段结束后一分钟把这条等锁的语句掐掉。
        """
        if self.meta is not None:
            kw.setdefault("lock_guard_s", C.lock_guard_s(self.meta, self.ctx.window_s))
        w = Worker(self.ctx, app_name(self.code, role), fn, **kw)
        self.workers.append(w)
        return w.start()

    def stop(self) -> str:
        """停本场景的全部会话(置位 → 取消挂着的语句 → 终止会话,见 session.stop_workers)。
        返回一句给时间线的说明;什么都没打断时为空串。"""
        r = stop_workers(self.workers, self.ctx)
        parts = []
        if r["cancelled"]:
            parts.append("取消了 %d 条挂着的语句" % r["cancelled"])
        if r["terminated"]:
            parts.append("终止了 %d 条会话" % r["terminated"])
        if r["left_running"]:
            parts.append("%d 条让它跑完这一轮" % r["left_running"])
        return "、".join(parts)

    def log(self, msg: str) -> None:
        self.ctx.log("[%s] %s" % (self.code, msg))

    # -- 给 probe 用的小工具 -------------------------------------------------

    def mine(self, admin: Conn, role: str = "") -> int:
        """库里还有几条属于本场景的会话。"""
        like = C.APP_PREFIX + self.code + ("/" + role if role else "") + "%"
        return int(admin.one(
            "SELECT count(*) FROM pg_stat_activity "
            " WHERE application_name LIKE %%s AND %s" % REAL_SESSION, (like,)) or 0)

    def waiting(self, admin: Conn) -> int:
        """本场景里正在等锁的会话数。"""
        return int(admin.one(
            "SELECT count(*) FROM pg_stat_activity "
            " WHERE application_name LIKE %%s AND waiting AND %s" % REAL_SESSION,
            (C.APP_PREFIX + self.code + "%",)) or 0)


REGISTRY: Dict[str, Type[Base]] = {}


def register(*codes: str):
    """把一个类登记成这几个编号的实现:`@register("S1", "S2", …)`。

    编号重复会当场报错 —— 两个类抢同一个号,后注册的会悄悄覆盖前一个,
    结果是「某个场景从此再也没跑过」,而终端上什么都看不出来。
    """
    def deco(cls: Type[Base]) -> Type[Base]:
        for code in codes:
            if not code:
                raise ValueError("%s 登记了空编号" % cls.__name__)
            if code in REGISTRY:
                raise ValueError("场景编号 %s 重复:%s 与 %s"
                                 % (code, REGISTRY[code].__name__, cls.__name__))
            REGISTRY[code] = cls
        return cls
    return deco


def build(ctx: Ctx, meta: C.Scenario) -> Base:
    """按排期表里的编号取实现。缺实现就报错,不静悄悄跳过 ——
    「少跑了一个场景」必须让人看见。"""
    cls = REGISTRY.get(meta.code)
    if cls is None:
        raise KeyError("场景 %s 在排期表里,但没有实现(scenarios/ 下找不到)" % meta.code)
    return cls(ctx, meta)
