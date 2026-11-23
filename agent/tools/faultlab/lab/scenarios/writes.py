"""写入(W1):大字段批量写入 → xlog 涨得快(2026-11-05 加,行方原话里的例子)。

应用把文档正文(几 KB 的随机文本)一批批插进来,每批即提交。随机内容压缩不了,每行都写进 TOAST,xlog 每秒几十 MB,
checkpoint 被频繁强制触发 —— **这不是数据库的问题,是业务写入引起的指标报警**。诊断技能应当:
  · 健康检查的 WAL 维度报 WAL_RATE_HIGH,并落到正在写的会话(应用 fl:W1/load)、写入最多的表 fl.doc_blob(大字段写入单列);
  · WDR 的写入来源一节给出这段时间写入最多的表与 redo 量,强制 checkpoint 那条指向它;
  · 处置写应用侧的(分批、控制大字段、移到低峰),不建议调 checkpoint / 缓冲区参数。

**体量有上限:** 表(连 TOAST)长到 W1_MAX_BYTES 就停写,只空转到时段结束 —— 测试库是共用的,不能把盘写满。
"""
from __future__ import annotations

import threading

from .. import config as C
from ..db import Conn
from ..session import until
from .base import Base, register

BODY = ("(SELECT string_agg(md5(random()::text || g::text || x::text), '') "
        "FROM generate_series(1, 150) x)")               # 150 × 32 = 4800 个随机十六进制字符


@register("W1")
class LobBulkLoad(Base):
    group = "writes"

    def start(self) -> None:
        self.worker("load", self._loop, expect_errors=True)

    def _loop(self, c: Conn, stop: threading.Event) -> None:
        state = {"n": 0, "full": False}

        def step() -> None:
            if state["full"]:
                stop.wait(1.0)
                return
            c.exec("INSERT INTO %s.doc_blob(who, body) SELECT 'W1', %s FROM generate_series(1, %d) g"
                   % (C.SCHEMA, BODY, C.W1_BATCH_ROWS))
            state["n"] += 1
            if state["n"] % 20 == 0:
                size = c.one("SELECT pg_total_relation_size('%s.doc_blob')" % C.SCHEMA) or 0
                if int(size) >= C.W1_MAX_BYTES:
                    state["full"] = True
                    self.log("doc_blob 已到 %d MB,停写" % (int(size) >> 20))

        until(stop, step, period_s=0.0)

    def probe(self, admin: Conn) -> str:
        rows = admin.query(
            "SELECT count(*), pg_total_relation_size('%s.doc_blob') FROM %s.doc_blob" % (C.SCHEMA, C.SCHEMA))
        n, size = rows[0] if rows else (0, 0)
        return "fl.doc_blob %s 行、连 TOAST %d MB;写入会话 %d 条" % (n, int(size or 0) >> 20, self.mine(admin))
