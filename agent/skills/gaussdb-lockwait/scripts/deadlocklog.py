"""死锁日志解析 —— 纯函数,不连库。

**为什么读日志、而且是用户给的日志。** 死锁的明细(环上是谁、各自在跑什么、谁被牺牲)数据库里不留:
pg_stat_database.deadlocks 只有次数;lockwait 抓的是正在发生的环,而 deadlock_timeout(og5 是 1 秒)
一到内核就杀掉牺牲者、解开环,等命令跑起来环早没了。明细只写在数据库日志里,而读日志的
pg_read_file 要 initial account —— 客户不会为此给权限(用户 2026-10-18 定:需要改客户数据库的一律不做)。
所以这里解析**用户贴过来的日志段**:用户从日志里把 `deadlock detected` 那几段复制给我们即可。

**不依赖 log_line_prefix。** 各现场的前缀不同(og5 是「时间 用户 库 地址 线程号 会话[…] 事务号 [BACKEND]」),
贴进对话时 tab 也可能丢了。所以只认三样东西:
  · 标记行:含 `deadlock detected`(中文环境 `检测到死锁`)的那一行,一次死锁从这里开始;
  · 等待边:`Process A waits for <锁> on <对象>; blocked by process B.`(中文 `进程A等待在<对象>上的<锁>; 由进程B阻塞`);
  · 语句:`Process A: <SQL>`(中文 `进程A: <SQL>`)与 `STATEMENT:  <SQL>`(牺牲者自己的那条)。

**牺牲者是第一条等待边的那个进程。** 内核的 DeadLockReport 从检测到死锁的那个进程开始沿环列出
(它就是报 ERROR、被回滚的那个)。og5 2026-10-17 那份日志实测:27 次死锁,ERROR 行前缀里的线程号
与第一条等待边的进程 27 次全部一致,STATEMENT 行也都是它的语句;27 个环全部首尾闭合。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_BYTES = 64 << 20        # 一次最多读这么多;更大的日志让用户只截死锁前后那几段

_MARK = re.compile(r"deadlock detected|检测到死锁", re.I)
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?")
_WAIT_EN = re.compile(r"Process\s+(\d+)\s+waits\s+for\s+(\S+)\s+on\s+(.+?);\s*blocked\s+by\s+process\s+(\d+)", re.I)
_WAIT_ZH = re.compile(r"进程\s*(\d+)\s*等待在\s*(.+?)\s*上的\s*(\S+?)\s*[;；]\s*由进程\s*(\d+)\s*阻塞")
_SQL_EN = re.compile(r"^\s*Process\s+(\d+):\s?(.*)$", re.I)
_SQL_ZH = re.compile(r"^\s*进程\s*(\d+)\s*[:：]\s?(.*)$")
_STMT = re.compile(r"STATEMENT:\s+(.*)$|语句:\s*(.*)$")
# 一行新日志的开头(有级别标签或以时间戳开头):SQL 的续行到这里为止
_NEW_LINE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|\b(LOG|ERROR|DETAIL|HINT|STATEMENT|CONTEXT|WARNING|FATAL|"
                       r"日志|错误|详细信息|提示|语句|上下文|警告):\s")

ROW, TABLE, ADVISORY, OTHER = "行锁", "表锁", "咨询锁", "其他锁"


@dataclass(frozen=True)
class Wait:
    pid: str
    mode: str           # ShareLock / ExclusiveLock …
    target: str         # transaction 148019356 / relation 16384 of database 13000 …
    blocker: str

    @property
    def kind(self) -> str:
        t = self.target.lower()
        if t.startswith(("transaction", "tuple")) or t.startswith(("事务", "元组")):
            return ROW
        if t.startswith("relation") or t.startswith("关系"):
            return TABLE
        if "advisory" in t or "咨询" in t:
            return ADVISORY
        return OTHER


@dataclass(frozen=True)
class Deadlock:
    at: str                         # 时间戳(取不到为空)
    waits: Tuple[Wait, ...]         # 日志顺序;第一条的进程是牺牲者
    sql: Dict[str, str] = field(default_factory=dict)      # 进程 → 它正在执行的语句
    victim_sql: str = ""            # STATEMENT: 那一行

    @property
    def victim(self) -> str:
        return self.waits[0].pid if self.waits else ""

    @property
    def closed(self) -> bool:
        """等待边首尾相接成环。不成环 = 日志段不完整(截断、或中间夹了别的行)。"""
        n = len(self.waits)
        return n >= 2 and all(self.waits[i].blocker == self.waits[(i + 1) % n].pid for i in range(n))


@dataclass(frozen=True)
class Parsed:
    deadlocks: Tuple[Deadlock, ...]
    bare: int               # 只有「deadlock detected」、没有等待边的段(日志级别不够或截取不全)
    truncated: bool         # 输入超过 MAX_BYTES,只读了前面一部分


# ---------------------------------------------------------------- 解析

def _normalize_text(text: str) -> str:
    # syslog 把 tab 记成 #011;Windows 换行
    return text.replace("#011", "\t").replace("\r\n", "\n").replace("\r", "\n")


def _blocks(lines: List[str]) -> List[List[str]]:
    """按标记行切段:每段从一行 `deadlock detected` 开始,到下一行标记之前。"""
    out, cur = [], None
    for line in lines:
        if _MARK.search(line):
            if cur is not None:
                out.append(cur)
            cur = [line]
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        out.append(cur)
    return out


def _continued(lines: List[str], i: int, first: str) -> Tuple[str, int]:
    """多行 SQL:从 first 开始,把后面不是新日志行、也不是新进程行的续行接上。返回 (SQL, 下一行下标)。"""
    parts = [first.strip()]
    j = i + 1
    while j < len(lines):
        ln = lines[j]
        if not ln.strip() or _NEW_LINE.search(ln) or _SQL_EN.match(ln) or _SQL_ZH.match(ln) \
                or _WAIT_EN.search(ln) or _WAIT_ZH.search(ln) or _MARK.search(ln):
            break
        parts.append(ln.strip())
        j += 1
    return " ".join(p for p in parts if p), j


def _waits(text: str) -> List[Wait]:
    out = [Wait(pid=m.group(1), mode=m.group(2), target=m.group(3).strip(), blocker=m.group(4))
           for m in _WAIT_EN.finditer(text)]
    out += [Wait(pid=m.group(1), mode=m.group(3), target=m.group(2).strip(), blocker=m.group(4))
            for m in _WAIT_ZH.finditer(text)]
    return out


def _parse_block(block: List[str]) -> Optional[Deadlock]:
    waits = _waits("\n".join(block))
    if not waits:
        return None
    ts = _TS.search(block[0])
    sql: Dict[str, str] = {}
    victim_sql = ""
    i = 0
    while i < len(block):
        ln = block[i]
        m = (_SQL_EN.match(ln) or _SQL_ZH.match(ln)) if not (_WAIT_EN.search(ln) or _WAIT_ZH.search(ln)) else None
        if m and m.group(1) not in sql:
            sql[m.group(1)], i = _continued(block, i, m.group(2))
            continue
        s = _STMT.search(ln)
        if s and not victim_sql:
            victim_sql, i = _continued(block, i, s.group(1) or s.group(2) or "")
            continue
        i += 1
    return Deadlock(at=ts.group(0) if ts else "", waits=tuple(waits), sql=sql, victim_sql=victim_sql)


def parse(text: str) -> Parsed:
    truncated = len(text) > MAX_BYTES
    lines = _normalize_text(text[:MAX_BYTES]).split("\n")
    found, bare = [], 0
    for block in _blocks(lines):
        d = _parse_block(block)
        if d is None:
            bare += 1
        else:
            found.append(d)
    return Parsed(deadlocks=tuple(found), bare=bare, truncated=truncated)


# ---------------------------------------------------------------- 归类

_LIT_STR = re.compile(r"'(?:[^']|'')*'")
_LIT_NUM = re.compile(r"(?<![\w$.])-?\d+(?:\.\d+)?(?![\w.])")


def shape(sql: str) -> str:
    """语句的形状:常量换成 ?、空白压成一个。同一条业务语句改不同的行,形状相同。"""
    s = _LIT_STR.sub("?", sql or "")
    s = _LIT_NUM.sub("?", s)
    return re.sub(r"\s+", " ", s).strip()


def stmt_of(d: Deadlock, pid: str) -> str:
    if pid in d.sql:
        return d.sql[pid]
    return d.victim_sql if pid == d.victim and d.victim_sql else ""


@dataclass(frozen=True)
class Pattern:
    size: int
    kinds: Tuple[str, ...]
    shapes: Tuple[str, ...]          # 环上各进程语句的形状,排过序(环从哪个进程开始不影响归类)
    events: Tuple[Deadlock, ...]     # 时间顺序

    @property
    def latest(self) -> Deadlock:
        return self.events[-1]


def group(deadlocks) -> List[Pattern]:
    """按(环的大小、锁类别、各进程语句的形状)归类,出现次数多的在前。"""
    buckets: Dict[tuple, List[Deadlock]] = {}
    for d in deadlocks:
        key = (len(d.waits), tuple(sorted({w.kind for w in d.waits})),
               tuple(sorted(shape(stmt_of(d, w.pid)) or "（日志里没有这条语句）" for w in d.waits)))
        buckets.setdefault(key, []).append(d)
    out = [Pattern(size=k[0], kinds=k[1], shapes=k[2], events=tuple(sorted(v, key=lambda x: x.at)))
           for k, v in buckets.items()]
    return sorted(out, key=lambda p: (-len(p.events), p.latest.at))


# ---------------------------------------------------------------- 建议

RETRY = ("被牺牲的事务已整笔回滚，应用收到的是 `deadlock detected`（SQLSTATE 40P01）。应用应捕获 40P01 "
         "并**重试整个事务**（不是只重试那一条语句）；只改顺序、不加重试，偶发的死锁仍会以报错的形式到达用户。")


def advice(p: Pattern) -> str:
    kinds = set(p.kinds)
    same = len(set(p.shapes)) == 1
    if kinds <= {ROW}:
        if same and p.shapes[0].upper().startswith("INSERT"):
            return ("**%d 个事务执行同一条 INSERT，互相等对方还没提交的同键插入** —— 唯一索引的冲突检查要等"
                    "对方事务结束，而各事务插入键的顺序不一致。改法：按同一顺序插入（例如批量插入前在应用里按键排序）；"
                    "键是随机生成的（如 `random()`）就换成序列或在应用里先去重；事务尽早提交。" % p.size)
        if same:
            return ("**%d 个事务执行同一条语句、改同一批行，但改的顺序不一致** —— 每个事务都先改了一行，"
                    "再去改另一个事务已经改过的行。改法：让这些事务**按同一顺序**改行（例如按主键升序）；"
                    "或在事务开头用 `SELECT … FOR UPDATE … ORDER BY <主键>` 一次锁住要改的全部行；"
                    "事务里尽量少持有行锁、尽早提交。" % p.size)
        return ("**不同的语句交叉加行锁** —— 一个事务先改 A 再改 B，另一个反过来。改法：统一这几条语句在事务里的"
                "执行顺序（同一业务流程里按固定的表顺序、表内按主键顺序）；能合并成一条语句的合并。")
    if kinds <= {TABLE}:
        return ("**表级锁环** —— 几个事务以不同顺序锁表（显式 LOCK TABLE、先读后升级成写锁、或 DDL 与 DML 交叉）。"
                "改法：统一锁表顺序；需要表锁时一开始就拿最终需要的那一级；DDL 放到低峰单独执行。")
    if kinds <= {ADVISORY}:
        return ("**应用自己加的咨询锁顺序不一致**。改法：统一 `pg_advisory_lock` 的加锁顺序（例如按 key 升序），"
                "或改用 `pg_try_advisory_lock`，拿不到就放掉已持有的锁再重试。")
    return ("**行锁与表锁混在一个环里** —— 先按上面两类分别统一加锁顺序；混合的环通常说明有一条语句在事务中途"
            "升级了表锁（如 LOCK TABLE、ALTER、TRUNCATE），把它挪到事务开头或单独执行。")


def explain_wait(w: Wait) -> str:
    """一条等待边的白话。transaction 锁的含义最常被误读：它是在等对方事务结束，原因是对方锁着这一行。"""
    t = w.target.lower()
    if t.startswith("transaction") or t.startswith("事务"):
        return "等 %s 的事务结束（对方已锁住它要改的行）" % w.blocker
    if t.startswith("tuple") or t.startswith("元组"):
        return "等行锁 %s（对方正排在它前面改这一行）" % w.target
    return "等 %s 上的 %s" % (w.target, w.mode)
