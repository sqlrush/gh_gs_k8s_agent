"""WDR evidence types — port of internal/probe/wdr/types.go + findings.go.

Includes JSON to_dict / from_dict so `wdr collect --format json` output can be
read back by `wdr render` (the no-DB render step). Field names match the Go json
tags exactly so a Go-produced evidence pack and a Python one are interchangeable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Finding 与 Severity 统一在 common/finding.py —— 本文件曾存一份完全相同的
# 定义，health / wdr / memanalyze 三家各一份。汇总层要跨进程解析这个形状，
# 三份定义迟早分叉，而分叉的表现是**少一条风险**，不是报错。
from common.finding import Finding, Severity, worst  # noqa: F401

# Dimension names — also the ## section titles in the evidence pack.
DIM_LOADPROFILE = "Load Profile"
DIM_DBSTAT = "Database Stat"
DIM_TOPSQL = "Top SQL"
DIM_WAITS = "Wait Events / Classes"
DIM_CHECKPOINT = "Checkpoint / BgWriter / Redo"
DIM_CACHE = "Cache / Memory"
DIM_FILEIO = "File IO"


def _finding_from_dict(d: dict) -> Finding:
    """本文件曾在 Finding 上挂一个同名 staticmethod；Finding 挪到 common 后
    那个 staticmethod 跟着没了，但 DimResult.from_dict / Evidence.from_dict
    仍要从 json 里重建 Finding —— 挪成模块级函数，逻辑照旧一字不改。"""
    return Finding(d.get("dimension", ""), d.get("code", ""),
                   Severity(int(d.get("severity", 0))), d.get("metric", ""),
                   d.get("value", ""), d.get("threshold", ""),
                   d.get("evidence", ""), d.get("sql_id", ""))


@dataclass
class DimResult:
    dimension: str
    available: bool = True
    note: str = ""
    headline: str = ""
    headers: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    findings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"dimension": self.dimension, "available": self.available,
             "headline": self.headline}
        if self.note:
            d["note"] = self.note
        if self.headers:
            d["headers"] = self.headers
        if self.rows:
            d["rows"] = self.rows
        if self.findings:
            d["findings"] = [f.to_dict() for f in self.findings]
        return d

    @staticmethod
    def from_dict(d: dict) -> "DimResult":
        return DimResult(
            dimension=d.get("dimension", ""), available=d.get("available", True),
            note=d.get("note", ""), headline=d.get("headline", ""),
            headers=d.get("headers", []) or [], rows=d.get("rows", []) or [],
            findings=[_finding_from_dict(f) for f in d.get("findings", []) or []])


@dataclass
class Window:
    begin_id: int = 0
    end_id: int = 0
    begin_ts: str = ""
    end_ts: str = ""
    duration_min: int = 0
    scope: str = ""
    node: str = ""
    wdr_enabled: bool = False

    def to_dict(self) -> dict:
        return {"begin_id": self.begin_id, "end_id": self.end_id,
                "begin_ts": self.begin_ts, "end_ts": self.end_ts,
                "duration_min": self.duration_min, "scope": self.scope,
                "node": self.node, "wdr_enabled": self.wdr_enabled}

    @staticmethod
    def from_dict(d: dict) -> "Window":
        return Window(
            begin_id=int(d.get("begin_id", 0)), end_id=int(d.get("end_id", 0)),
            begin_ts=d.get("begin_ts", ""), end_ts=d.get("end_ts", ""),
            duration_min=int(d.get("duration_min", 0)), scope=d.get("scope", ""),
            node=d.get("node", ""), wdr_enabled=bool(d.get("wdr_enabled", False)))


@dataclass
class NativeInfo:
    generated: bool = False
    saved_path: str = ""
    bytes: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        d = {"generated": self.generated, "bytes": self.bytes}
        if self.saved_path:
            d["saved_path"] = self.saved_path
        if self.note:
            d["note"] = self.note
        return d

    @staticmethod
    def from_dict(d: dict) -> "NativeInfo":
        return NativeInfo(generated=bool(d.get("generated", False)),
                          saved_path=d.get("saved_path", ""),
                          bytes=int(d.get("bytes", 0)), note=d.get("note", ""))


@dataclass
class Options:
    begin: int = 0
    end: int = 0
    scope: str = "node"
    node: str = ""
    top: int = 10
    save_html: str = ""
    thresholds: object = None
    # 采集过程中算出来、后续采集器要用的量(不是用户给的选项):
    #   db_time_us / cpu_time_us —— Load Profile 算出的窗口真实 DB time,Top SQL 拿它做分母
    #   wait_total_us            —— 等待维度的总等待时长,收尾时用来交叉校验 DB time 是否可信
    # 0 表示「还没算出来 / 不可用」,用的一方必须自己判。
    db_time_us: int = 0
    cpu_time_us: int = 0
    wait_total_us: int = 0
    window_s: int = 0           # 窗口墙钟秒数,算平均并发活跃会话用
    #   temp_bytes / stmt_spill_kb / spill_champ —— 库级溢出与语句级落盘,收尾时把溢出那条发现
    #   归到语句上(见 collectors._attribute_spill)。spill_champ = (sql_id, spill_kb, 是否在显示的前 N 名里)
    temp_bytes: int = 0
    stmt_spill_kb: int = 0
    stmt_rows_capped: bool = False   # 语句级合计只算了取回的那一批(取满了,实际可能更多)
    spill_champ: tuple = ()
    sql_texts: dict = field(default_factory=dict)   # sql_id → 全文,采集中累积


@dataclass
class Evidence:
    conn: str = ""
    target: str = ""
    window: Window = field(default_factory=Window)
    dims: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    overall: Severity = Severity.OK
    native: NativeInfo = field(default_factory=NativeInfo)
    # sql_id → **完整** SQL 文本。
    #
    # 为什么单独存一张表、不塞进 Top SQL 的行里:根因里每提到一条 SQL 都要能拿到全文,
    # 而提到它的地方不止 Top SQL 那张表(findings 里只有 sql_id)。按 sql_id 查一张表,
    # 两边都能解析,长文本也只存一份。
    #
    # 原来**全文根本没留下来**:采集时就 trunc 到 50 字符存盘,想展示也没有。
    # 现在显示仍然截断,但存档留全文 —— 大盘的 SQL 详情面板和执行计划深挖都靠它。
    sql_texts: dict = field(default_factory=dict)
    # sql_id → {"plan", "start_time", "db_time_ms", "rows"} 或 {"plan": "", "why": 取不到的原因}。
    # SQL 附录的执行计划在采集时取(窗口内最近一次真实执行计划),不再只靠模型调 explain 取回来
    sql_plans: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"conn": self.conn, "target": self.target,
                "window": self.window.to_dict(),
                "dims": [d.to_dict() for d in self.dims],
                "sql_texts": self.sql_texts,
                "sql_plans": self.sql_plans,
                "findings": [f.to_dict() for f in self.findings],
                "overall": int(self.overall),
                "native": self.native.to_dict()}

    @staticmethod
    def from_dict(d: dict) -> "Evidence":
        return Evidence(
            conn=d.get("conn", ""), target=d.get("target", ""),
            window=Window.from_dict(d.get("window", {}) or {}),
            dims=[DimResult.from_dict(x) for x in d.get("dims", []) or []],
            sql_texts=d.get("sql_texts", {}) or {},
            sql_plans=d.get("sql_plans", {}) or {},
            findings=[_finding_from_dict(x) for x in d.get("findings", []) or []],
            overall=Severity(int(d.get("overall", 0))),
            native=NativeInfo.from_dict(d.get("native", {}) or {}))


def degraded(dim: str, reason: str) -> DimResult:
    """DimResult for a collector whose query failed (missing view / counter
    reset). Not fatal: the report shows the dimension as unavailable."""
    return DimResult(dimension=dim, available=False, note=reason,
                     headline="不可用：" + reason)


def dim_severity(d: DimResult) -> Severity:
    return worst([f.severity for f in d.findings])
