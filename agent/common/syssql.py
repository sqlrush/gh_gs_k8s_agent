"""慢 SQL / Top SQL 排行里排除 GaussDB 自己的系统 SQL —— 慢 SQL、Top SQL、健康检查、WDR 共用。

要求(用户 2026-10-22 定):所有技能定位慢 SQL 时,排除「高斯自己的系统 SQL」,报告里写明排除了几条,
不悄悄藏掉。

**判定只能看语句正文。** 排行取自 dbe_perf.statement / snapshot.snap_summary_statement,这两张视图
**没有应用名列**(内核后台线程 WDRXdb、statement flush thread 等的名字只在 statement_history 与
pg_stat_activity 里),所以「按应用名认内核线程」在排行里用不上。好在内核线程发的语句只碰系统对象
(og5 实测 WDRXdb 的语句全是 `select ?, ?, t.* from dbe_perf.summary_statio_all_indexes t` 一类),
按正文判同样能认出来;诊断工具(含我方智能体)查系统视图的语句也一并排除 —— 它们同样会污染慢 SQL 榜。

命中任一即为系统 SQL:
  ① 引用的表 / 视图**全部**是系统对象(系统 schema、pg_ / gs_ 前缀、statement_history 等);
  ② 不引用任何表,只调用系统函数(generate_wdr_report、pg_* / gs_* 等;pg_sleep 除外 —— 那是业务在等)。

判定**保守**,与 sqltune 的系统对象策略同一方向:拿不准一律当业务 SQL 保留。
误保留只是榜上多一行;误排除会把客户真正的慢 SQL 藏起来。
  - 排行里的正文是截断的(slowsql 180 字、topsql 80 字),FROM 可能在截掉的那一半里 —— 截断且只看到系统
    对象时**不下结论**,按 sql_id 补取全文(白名单里已有的 sqlfetch.from_statement / from_history,
    不改白名单、客户不用重导)再判;补不到就保留,并在说明里写明有几条是这样保留的。
  - WITH 子句的名字不是表;`extract(epoch from now())` 里的 now 是函数不是表。两者都不算用户对象。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from common import sqlrefs, sysobjects

SYSTEM, USER, UNSURE = "system", "user", "unsure"

# 未限定 schema 时:openGauss 系统表 / 系统视图统一以 pg_ / gs_ 开头(与 sqltune/systables.py 同一口径)
_SYSTEM_NAME_PREFIXES = ("pg_", "gs_")
_SYSTEM_NAMES = frozenset({"dual", "sys_dummy", "statement_history"})

# 不在 pg_ / gs_ 前缀里的内核函数。og5(openGauss-lite 5.0.3)pg_proc 里逐个核对过都在 pg_catalog 下。
KERNEL_FUNCTIONS = frozenset({
    "generate_wdr_report", "create_wdr_snapshot", "kill_snapshot", "wdr_xdb_query",
    "reset_unique_sql", "get_instr_unique_sql", "get_instr_wait_event", "get_local_active_session",
})
# 业务语句里常见的「系统函数」,但耗时属于业务:pg_sleep 是应用在等,不是内核在干活
_NOT_KERNEL_FUNCTIONS = frozenset({"pg_sleep", "pg_sleep_for", "pg_sleep_until"})

# 排行要多取的行数(调用方用):不多取的话,排除掉系统 SQL 之后榜就短了。
# og5 实测(2026-10-23):智能体用了几天之后,慢 SQL 按平均耗时排的前 110 条**全是**系统 SQL(多是它自己查
# 系统视图的语句),余量 30、100 都不够。取数脚本只有 LIMIT、没法翻页,只能一次取宽;500 与 topsql 的
# 采样宽度(delta.SAMPLE_WIDTH)一样,是中间件上已经在用的量。
SCREEN_MARGIN = 500
# 补取全文的上限:每条一次中间件往返。理由同上 —— 截断的系统 SQL 可能有上百条排在业务 SQL 前面。
# 判过的进缓存(VerdictCache),所以只有第一次跑会补这么多次;补得多时会在 stderr 说一声,不让人以为卡住了。
MAX_LOOKUPS = 200
_LOOKUP_NOTICE_AT = 10
# 判定缓存:sql_id → system / user(只存靠全文判出来的)。sql_id 是归一化语句的哈希,同一个 id 就是同一条
# 语句,判一次就够 —— 不然每次跑都要按 sql_id 一条条补全文。放在状态目录(容器里是持久卷 /nas/me/gdaa)。
CACHE_NAME = "syssql-verdicts.json"
CACHE_MAX = 5000
# 补取全文用的白名单脚本,按顺序试。前者是统计视图(多数情况都在),后者是 SQL 历史(实例重启后统计清零时还有)
FULL_TEXT_SCRIPTS = ("sqlfetch.from_statement", "sqlfetch.from_history")
# 说明里列出的 sql_id 个数
_NOTE_IDS = 5

_COMMENT_RE = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_CTE_RE = re.compile(r"(?is)(?:\bwith\s+(?:recursive\s+)?|,\s*)([a-z_]\w*)\s*(?:\([^()]*\)\s*)?"
                     r"as\s*(?:not\s+)?(?:materialized\s*)?\(")
_CALL_RE = re.compile(r"(?i)\b([a-z_]\w*(?:\.[a-z_]\w*)?)\s*\(")
# ANALYZE / VACUUM 的对象不在 FROM 后面:`analyze snapshot.tables_snap_timestamp` 是内核 WDR 自己的维护语句
_MAINT_RE = re.compile(r"(?is)^\s*(?:analy[sz]e|vacuum)\b(?:\s*\([^)]*\))?"
                       r"(?:\s+(?:full|freeze|verbose|analy[sz]e))*\s+([a-z_][\w.]*(?:\s*,\s*[a-z_][\w.]*)*)")
# 紧跟括号但不是函数调用的关键字
_NOT_CALLS = frozenset({
    "in", "exists", "any", "all", "some", "values", "as", "and", "or", "not", "on", "using", "over",
    "filter", "within", "select", "from", "where", "join", "into", "with", "recursive", "materialized",
    "case", "when", "then", "else", "is", "like", "between", "returning", "group", "order", "by",
})
# 正文停在这些词上 = 被截断了(语句不可能这样结尾)
_DANGLING = frozenset({
    "select", "from", "where", "and", "or", "in", "not", "join", "on", "by", "group", "order",
    "having", "union", "as", "exists", "between", "like", "limit", "offset", "case", "when",
    "then", "else", "left", "right", "inner", "outer", "full", "cross", "using", "with",
})


def is_system_relation(ref: str) -> bool:
    """表引用(`s.t` 或 `t`)是不是数据库自带的。schema 限定的只按 sysobjects 那张名单判。"""
    if "." in ref:
        return sysobjects.is_system_schema(ref[:ref.index(".")])
    return ref.startswith(_SYSTEM_NAME_PREFIXES) or ref in _SYSTEM_NAMES


def is_system_function(name: str) -> bool:
    name = name.lower()
    if "." in name:
        return sysobjects.is_system_schema(name[:name.index(".")])
    if name in _NOT_KERNEL_FUNCTIONS:
        return False
    return name.startswith(_SYSTEM_NAME_PREFIXES) or name in KERNEL_FUNCTIONS


def _scrub(sql_text: str) -> str:
    """去掉注释与字符串常量 —— `'… from orders …'` 这类常量不能当成表引用。"""
    return _LITERAL_RE.sub("''", _COMMENT_RE.sub(" ", sql_text or ""))


def looks_cut(sql_text: str) -> bool:
    """全文是不是被内核截断了(track_activity_query_size)。括号不配对、或停在不可能结尾的词上。"""
    s = _scrub(sql_text).strip().rstrip(";").strip()
    if not s:
        return False
    if s.count("(") != s.count(")") or (sql_text or "").count("'") % 2:
        return True
    # 最后一个记号把占位符 / 数字也算上:归一化的语句常以 `LIMIT ?` 结尾,只看英文单词会误判成停在 limit 上
    tokens = re.findall(r"[\w?$]+|[^\s\w]", s.lower())
    return bool(tokens) and tokens[-1] in _DANGLING


def classify(sql_text: str, complete: bool = True) -> str:
    """SYSTEM / USER / UNSURE。complete=False 表示正文是截断的:只能判出 USER,判不出 SYSTEM。"""
    text = _scrub(sql_text)
    if not complete:
        # 截断处的最后一个词多半是半截(`JOIN dbe_pe`):当成表名会被判成业务对象,这一条就永远排除不掉
        text = re.sub(r"[\w.]+\s*$", "", text)
    ctes = {m.group(1).lower() for m in _CTE_RE.finditer(text)}
    relations, calls = [], set()
    maint = _MAINT_RE.match(text)
    if maint:
        relations += [n.strip().lower() for n in maint.group(1).split(",")]
    for ref in sqlrefs.extract_table_refs(text):
        if ref in ctes:
            continue
        # FROM 后面紧跟括号的是函数:FROM create_wdr_snapshot()、extract(epoch from now())
        if re.search(r"(?i)\b%s\s*\(" % re.escape(ref), text):
            calls.add(ref)
        else:
            relations.append(ref)
    calls |= {m.group(1).lower() for m in _CALL_RE.finditer(text)
              if m.group(1).lower() not in _NOT_CALLS}
    if any(not is_system_relation(r) for r in relations):
        return USER
    # 带 schema 的业务函数(fl.calc_bonus(...))说明它在做业务的事;不带 schema 的多是内置函数,不据此判
    if any("." in f and not is_system_function(f) for f in calls):
        return USER
    if not complete:
        return UNSURE
    if relations:
        return SYSTEM
    if calls and all(is_system_function(f) for f in calls):
        return SYSTEM
    return USER          # SELECT ? / SET / COMMIT / 只调内置函数:不是内核的,留着


@dataclass(frozen=True)
class Screening:
    """一张排行按「排除系统 SQL」过一遍的结果。kept 保持原来的排名顺序。"""
    kept: list
    excluded: list = field(default_factory=list)
    excluded_ids: List[str] = field(default_factory=list)
    excluded_seconds: float = 0.0
    unresolved: int = 0          # 正文截断、全文补取不到 → 按业务 SQL 保留的条数

    def note(self) -> str:
        """给报告用的一句话。什么都没排除、也没有判不了的 → 空串(不加噪音)。"""
        parts = []
        if self.excluded_ids:
            ids = "、".join(self.excluded_ids[:_NOTE_IDS]) + ("等" if len(self.excluded_ids) > _NOTE_IDS else "")
            cost = ",合计耗时 %.1f 秒" % self.excluded_seconds if self.excluded_seconds > 0 else ""
            parts.append("已排除 %d 条内核系统 SQL(只访问系统表 / 系统视图 / 系统函数的语句,"
                         "如 WDR 快照、监控采集、诊断工具自己的查询%s):sql_id %s"
                         % (len(self.excluded_ids), cost, ids))
        if self.unresolved:
            parts.append("另有 %d 条正文被截断、补取全文没成功,判断不了是不是系统 SQL,按业务 SQL 保留在榜上"
                         % self.unresolved)
        return ";".join(parts) + ("。" if parts else "")

    def all_excluded(self) -> bool:
        """取回来的全是系统 SQL:榜为空不是因为没有慢 SQL / 没开统计,要这样说。"""
        return not self.kept and bool(self.excluded_ids)

    def all_excluded_text(self, fetched_cap: int, what: str) -> str:
        """「全是系统 SQL」时的那句话。取满了上限 = 后面可能还有业务 SQL,只是没取到,要说清楚。"""
        n = len(self.excluded_ids)
        if n >= fetched_cap:
            return ("%s前 %d 条全是内核系统 SQL,没有业务语句;再往后的没取(一次最多取 %d 条),"
                    "要看就加 `--include-system`" % (what, n, fetched_cap))
        return "%s %d 条全是内核系统 SQL,没有业务语句" % (what, n)

    def brief(self) -> str:
        """标题行用的短句(健康检查、WDR 的维度标题)。"""
        parts = []
        if self.excluded_ids:
            cost = ",合计 %.1f 秒" % self.excluded_seconds if self.excluded_seconds > 0 else ""
            parts.append("已排除 %d 条内核系统 SQL(%s%s)"
                         % (len(self.excluded_ids), "、".join(self.excluded_ids[:3])
                            + ("等" if len(self.excluded_ids) > 3 else ""), cost))
        if self.unresolved:
            parts.append("%d 条正文截断判断不了、按业务 SQL 保留" % self.unresolved)
        return ";".join(parts)

    def as_dict(self) -> dict:
        """存档用(大盘与健康检查读)。"""
        return {"excluded": list(self.excluded_ids), "excluded_seconds": round(self.excluded_seconds, 3),
                "unresolved": self.unresolved, "note": self.note(), "brief": self.brief()}


def screen(rows: Iterable, *, sql_id: Callable, text: Callable, want: Optional[int] = None,
           shown_chars: Optional[int] = None, full_text: Optional[Callable[[str], Optional[str]]] = None,
           seconds: Optional[Callable] = None, max_lookups: int = MAX_LOOKUPS,
           cache: Optional["VerdictCache"] = None) -> Screening:
    """按排名顺序走一遍,留下前 want 条非系统 SQL;走到的范围里的系统 SQL 记为排除。

    shown_chars:正文在 SQL 里被截到多少字(LEFT(…, n));None = 给的就是全文。
    full_text:sql_id → 全文(见 full_text_lookup);None = 不补取,截断的一律保留。
    seconds:行 → 耗时秒数,只用于说明里的「合计耗时」。
    cache:判定缓存(见 VerdictCache);先查缓存,查不到才补全文,补全文判出来的写回并在结束时落盘。
    调用方要**多取一些行**(want 之外留 SCREEN_MARGIN),否则排除掉几条之后榜就短了。
    """
    kept, excluded, ids = [], [], []
    cost, unresolved, lookups = 0.0, 0, 0
    for row in rows:
        if want is not None and len(kept) >= want:
            break
        body = str(text(row) or "")
        # 没有正文(老基线里只存了数字)也算「不知道」,要补取
        complete = bool(body.strip()) and ((len(body) < shown_chars) if shown_chars else not looks_cut(body))
        verdict = classify(body, complete)
        sid = str(sql_id(row))
        if verdict == UNSURE and cache is not None:
            verdict = cache.get(sid) or UNSURE
        if verdict == UNSURE and full_text is not None and lookups < max_lookups:
            lookups += 1
            if lookups == _LOOKUP_NOTICE_AT:
                print("按 sql_id 补取全文、判断是不是内核系统 SQL(第一次会慢一些,判过的会记住)…",
                      file=sys.stderr)
            whole = full_text(sid)
            if whole:
                verdict = classify(whole, not looks_cut(whole))
                if verdict != UNSURE and cache is not None:
                    cache.put(sid, verdict)
        if verdict == SYSTEM:
            excluded.append(row)
            ids.append(sid)
            cost += float(seconds(row) or 0) if seconds else 0.0
            continue
        if verdict == UNSURE:
            unresolved += 1
        kept.append(row)
    if cache is not None:
        cache.save()
    return Screening(kept=kept, excluded=excluded, excluded_ids=ids,
                     excluded_seconds=cost, unresolved=unresolved)


def keep_all(rows: Sequence) -> Screening:
    """--include-system:原样保留,说明为空。让调用方不必分两条代码路径。"""
    return Screening(kept=list(rows))


def full_text_lookup(runner) -> Callable[[str], Optional[str]]:
    """sql_id → 全文(取不到返回 None)。按 FULL_TEXT_SCRIPTS 的顺序试,同一个 sql_id 只取一次。

    取不到的原因(脚本没登记、备机、权限、统计已清零)一律不抛:后果只是这一条保留在榜上,
    screen 会把它计入 unresolved 并写进说明 —— 不是静默吞掉。
    """
    cache: dict = {}

    def get(sid: str) -> Optional[str]:
        if sid in cache:
            return cache[sid]
        cache[sid] = None
        try:
            key = int(sid)
        except ValueError:
            return None
        for script in FULL_TEXT_SCRIPTS:
            try:
                rows = runner.run(script, {"sid": key})
            except Exception:  # noqa: BLE001 —— 换下一个来源;都不行就按业务 SQL 保留(计入 unresolved)
                continue
            body = str((rows[0].get("query") if rows else "") or "").strip()
            if body:
                cache[sid] = body
                break
        return cache[sid]

    return get


class VerdictCache:
    """sql_id → system / user 的判定缓存,存在状态目录的一个 JSON 文件里。

    读写失败(目录只读、文件坏了)只会让它退化成本次进程内的缓存:后果是下次还要补全文,结果不受影响。
    超过 CACHE_MAX 条时丢掉最早写入的。
    """

    def __init__(self, path: Optional[pathlib.Path]):
        self.path = path
        self._data: Dict[str, str] = {}
        self._dirty = False
        if path is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {str(k): v for k, v in raw.items() if v in (SYSTEM, USER)}
            except (OSError, ValueError):
                pass                     # 没有 / 读不了 / 坏了:从空的开始,下次 save 时覆盖

    def get(self, sid: str) -> Optional[str]:
        return self._data.get(sid)

    def put(self, sid: str, verdict: str) -> None:
        if verdict in (SYSTEM, USER) and self._data.get(sid) != verdict:
            self._data.pop(sid, None)
            self._data[sid] = verdict
            self._dirty = True

    def save(self) -> None:
        if not self._dirty or self.path is None:
            return
        items = list(self._data.items())[-CACHE_MAX:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(dict(items)), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass                         # 状态目录写不了:只是下次还要补全文


def verdict_cache() -> VerdictCache:
    """状态目录(GSDB_HOME)下的判定缓存。没设 GSDB_HOME 且默认目录不可写时退化为进程内缓存。"""
    from common import config
    return VerdictCache(config.state_dir() / CACHE_NAME)
