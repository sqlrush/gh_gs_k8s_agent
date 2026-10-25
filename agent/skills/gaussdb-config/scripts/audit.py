"""参数体检的判定 —— 纯函数，不连库。

每条规则都是一个小函数，返回 0 或多个 Finding。**互不短路**：一个实例可以同时
犯好几条，用 elif 串起来会让排在后面的真实问题从报告里消失（这是 vacuum/rules.py
已经踩过并写在注释里的坑，这里照同一个结构写）。

**判什么、不判什么（这条线是本模块的全部设计）：**
  判 —— ① 自相矛盾（两个参数放一起必然有一个不生效）
        ② 会静默丢数据或丢可观测性（与机器规格无关）
        ③ 按实例自己的内存预算 max_process_memory 算出来的超卖
  不判 — 任何需要知道 CPU 核数 / 物理内存 / 磁盘类型 / 业务形态才能下结论的
        "建议值"。看不到那些事实，给出的数字就是编的。
"""
from __future__ import annotations

import pathlib
import sys
from typing import Dict, List

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "common" / "finding.py").exists():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from common.finding import Finding, Severity  # noqa: E402

from baseline import Setting, how_to_apply  # noqa: E402

DIMENSION = "Parameters"

# 关掉这些 = 拿正确性换性能，掉电/崩溃后可能丢数据或读到坏页。与机器规格无关。
# (参数名, 危险取值是不是 on, 严重度, 为什么, 怎么办)
_DANGEROUS = [
    ("fsync", False, Severity.CRITICAL,
     "关掉 fsync 后提交不再保证落盘 —— 操作系统崩溃或掉电会丢掉已提交的事务，"
     "而且损坏往往到下次读那一页时才暴露。",
     "改回 on。只有在「数据可以整库重建」的导入场景里才允许临时关闭，且导入后必须改回。"),
    ("full_page_writes", False, Severity.CRITICAL,
     "关掉之后，崩溃恢复时无法修复写了一半的页（torn page），表现是恢复失败或读到坏页。",
     "改回 on。"),
    ("zero_damaged_pages", True, Severity.CRITICAL,
     "开启后遇到坏页会**直接把它当成空页跳过**，查询照常返回 —— 数据静默丢失，"
     "而且没有任何报错。",
     "改回 off。只在抢救损坏实例、且已知要丢数据时临时开启。"),
    ("ignore_checksum_failure", True, Severity.CRITICAL,
     "开启后校验和不匹配只告警不报错，坏数据会被当成正常数据读出来并可能写回。",
     "改回 off。"),
    ("logging_collector", False, Severity.WARN,
     "日志收集器没开，数据库日志可能散落在标准错误里或直接丢失 —— "
     "死锁详情、慢语句、崩溃原因事后都查不到。",
     "改为 on（%s）。" ),
    ("autovacuum", False, Severity.CRITICAL,
     "全局关掉 autovacuum，死元组不会被回收、统计信息不会更新 —— "
     "表会持续膨胀，执行计划也会越来越差。",
     "改回 on。个别表确实需要手工控制时，用表级 `autovacuum_enabled=false`，不要全局关。"),
]


def _dangerous(st: Dict[str, Setting]) -> List[Finding]:
    out = []
    for name, bad_is_on, sev, why, how in _DANGEROUS:
        s = st.get(name)
        if s is None:
            continue
        if s.on is bad_is_on:
            out.append(Finding(
                DIMENSION, "CFG_UNSAFE", sev, name, s.raw,
                "off" if bad_is_on else "on",
                "%s 处置：%s" % (why, (how % how_to_apply(s)) if "%s" in how else how)))
    return out


def _archive(st: Dict[str, Setting]) -> List[Finding]:
    """归档开着却没有目标 = 归档必然失败。

    **这是本内核上唯一能查的归档问题。** 没有 `pg_stat_archiver`(实测不存在),
    失败次数、最后一次失败时间都取不到 —— 所以查不了"归档在失败"，
    只能查"归档不可能成功"。这个边界要写进证据里,否则读的人会以为归档已经被体检过。
    """
    mode = st.get("archive_mode")
    if mode is None or not mode.on:
        return []
    cmd = (st.get("archive_command").raw if st.get("archive_command") else "").strip()
    dest = (st.get("archive_dest").raw if st.get("archive_dest") else "").strip()
    bad_cmd = (not cmd) or cmd == "(disabled)"
    if bad_cmd and not dest:
        return [Finding(
            DIMENSION, "CFG_ARCHIVE_NO_TARGET", Severity.CRITICAL,
            "archive_mode 开着但没有归档目标",
            "archive_command=%s、archive_dest=%s" % (cmd or "(空)", dest or "(空)"),
            "两者至少配一个",
            "归档开启后 WAL 必须成功归档才会被回收；目标没配置，归档永远不会成功，"
            "**WAL 会一直堆积直到填满磁盘**。处置：配置 archive_command 或 archive_dest，"
            "或在确认不需要归档时把 archive_mode 关掉。"
            "注意：本内核没有 pg_stat_archiver，归档**是否正在失败**查不到，"
            "这里只能查出「不可能成功」这一种。")]
    return []


def _thread_pool(st: Dict[str, Setting]) -> List[Finding]:
    """max_connections 超过线程池大小 —— 多出来的连接数永远用不上。

    og5 实测:max_connections=1000、thread_pool_attr='800, 2, (nobind)'。
    这不是"性能调优建议",是**客观的配置矛盾**:按 1000 做容量规划的人会以为还有
    200 的余量,而实际 800 就满了,之后新请求排队、表现成「库没响应」。
    """
    pool = st.get("thread_pool_attr")
    enabled = st.get("enable_thread_pool")
    mc = st.get("max_connections")
    if pool is None or enabled is None or mc is None or not enabled.on:
        return []
    head = pool.raw.split(",", 1)[0].strip()
    if not head.isdigit() or mc.num is None:
        return []
    size = int(head)
    if size <= 0 or mc.num <= size:
        return []
    return [Finding(
        DIMENSION, "CFG_POOL_LT_CONNS", Severity.WARN,
        "max_connections 超过线程池大小",
        "max_connections=%d，池=%d" % (int(mc.num), size), "应 ≤ 池大小",
        "开启线程池后真正的并发上限是池大小（%d），超出的 %d 永远用不上 —— "
        "按 max_connections 做容量规划会高估余量，而池满后新请求排队、"
        "表现成「库没响应」且连接数看着还远没到上限。"
        "处置：把 thread_pool_attr 的线程数调到不低于 max_connections，"
        "或把 max_connections 降到池大小（两者都%s）。"
        % (size, int(mc.num) - size, how_to_apply(mc)))]


def _memory_overcommit(st: Dict[str, Setting]) -> List[Finding]:
    """work_mem × 并发上限 超过实例自己的内存预算。

    **分母用 openGauss 自己的 `max_process_memory`,不猜物理内存。** 这是实例
    被允许用的总量,是个可读的事实;物理内存取不到,拿它做分母才是编依据。

    work_mem 是**每个排序/哈希算子**的上限,一条 SQL 可以有多个算子 —— 所以这个
    估算已经偏保守了,偏保守的估算越界就更说明问题。
    """
    wm = st.get("work_mem")
    mpm = st.get("max_process_memory")
    conc = st.get("max_connections")
    pool = st.get("thread_pool_attr")
    enabled = st.get("enable_thread_pool")
    if wm is None or mpm is None or wm.num is None or mpm.num is None or mpm.num <= 0:
        return []
    # 并发上限取线程池大小(开了池的话),否则 max_connections —— 用实际会生效的那个
    limit = None
    if enabled is not None and enabled.on and pool is not None:
        head = pool.raw.split(",", 1)[0].strip()
        if head.isdigit() and int(head) > 0:
            limit = int(head)
    if limit is None and conc is not None and conc.num:
        limit = int(conc.num)
    if not limit:
        return []
    worst = wm.num * limit
    if worst <= mpm.num:
        return []
    return [Finding(
        DIMENSION, "CFG_WORKMEM_OVERCOMMIT", Severity.WARN,
        "work_mem × 并发上限 超过实例内存预算",
        "%s × %d = %s > max_process_memory %s"
        % (_b(wm.num), limit, _b(worst), _b(mpm.num)),
        "应 ≤ max_process_memory",
        "每个排序/哈希算子最多用 work_mem，并发打满时总量会超过实例被允许使用的"
        "内存预算 —— 真实表现是语句报内存不足、或大量算子被迫落盘（临时文件暴涨）。"
        "这个估算还偏保守：一条 SQL 可以有多个算子。"
        "处置：降 work_mem（会话级可按需调大给个别大查询），或降并发上限，"
        "或调大 max_process_memory（需重启）。")]


def _weak_password_crypto(st: Dict[str, Setting]) -> List[Finding]:
    """口令加密算法弱。

    og5 实测 `password_encryption_type = 1`(内核默认是 2),而建用户时内核自己就会
    警告:"The encrypted password contains MD5 ciphertext, which is not secure."
    —— 这不是性能取舍,是客观的弱算法配置,和机器规格无关。

    取值含义(openGauss):0/1 含 MD5 兼容,2 = sha256,3 = sm3。
    """
    s_ = st.get("password_encryption_type")
    if s_ is None or s_.num is None:
        return []
    if s_.num >= 2:
        return []
    return [Finding(
        DIMENSION, "CFG_WEAK_PASSWORD_CRYPTO", Severity.WARN,
        "password_encryption_type 口令加密算法偏弱",
        s_.raw, ">=2（2=sha256，3=sm3）",
        "当前取值会以 MD5 兼容方式存储口令散列 —— 建用户时内核自己就会提示"
        "「contains MD5 ciphertext, which is not secure」。"
        "处置：改为 2（sha256）或 3（sm3）（%s）；**已存在的用户口令不会自动升级**，"
        "改完要让这些用户重设口令才会用新算法存储。" % how_to_apply(s_))]


def _no_timeouts(st: Dict[str, Setting]) -> List[Finding]:
    """全局没有任何语句/空闲事务超时 —— 一条跑飞的 SQL 或一个忘了提交的事务
    可以无限期占着资源、挡住回收。

    只报 NOTICE:有些业务(批处理、数据仓库)确实需要不限时,这是**权衡**不是错误。
    措辞上给的是"确认一下是不是有意的",不是"你配错了"。
    """
    out = []
    for name, cn in (("statement_timeout", "语句超时"),
                     ("idle_in_transaction_session_timeout", "空闲事务超时")):
        s = st.get(name)
        if s is None:
            continue
        # **用 effective 而不是 num。** 取数层会给自己的会话设 statement_timeout,
        # 读会话值会看到 30000、把「全局没有超时」这条漏掉(实测确认过)。
        if s.effective_num is None or s.effective_num != 0:
            continue
        out.append(Finding(
            DIMENSION, "CFG_NO_TIMEOUT", Severity.NOTICE, "%s（%s）未设置" % (name, cn),
            "0（不限制）", ">0",
            ("一条跑飞的 SQL 会一直跑下去" if name == "statement_timeout"
             else "一个忘了提交的事务会一直持有快照，挡住死元组回收")
            + " —— 全局不限制时没有任何兜底。批处理/数仓场景确实可能有意如此，"
              "**请确认这是有意的**；若不是，按业务最长可接受时长设置（%s）。"
            % how_to_apply(s)))
    return out


# **「改了配置但还没生效」这条检查在这个内核上做不了 —— 试过了,别再加回来。**
#
# 第一版用 `setting != reset_val` 判,自测时报出 5 条(UDFWorkerMemHardLimit、
# allow_create_sysobject、asp_sample_num、audit_thread_num …),看着很像真的。
# 实测之后发现前提就是错的:
#   · og5 上有 **108 个**参数 `setting != reset_val`,而它们的 `source` 全是
#     `default` —— 没人改过任何东西,是这个内核对一大批参数根本不填有效的
#     reset_val(其中 62 个 reset_val 干脆是 0,其余是空串);
#   · 这个内核**没有 `pending_restart` 列**(information_schema 逐列核对过),
#     而那才是 PostgreSQL 里判这件事的正确依据;
#   · 收窄成 `source='configuration file' AND setting != reset_val` 只剩 1 条,
#     而我无法判断那 1 条是真阳性还是又一个 reset_val 假象 —— 一条无法验证的
#     规则,和虚报是同一种错,只是规模小一点。
#
# 第一版还给这条规则加了「最多报 5 条」的上限 —— **那个上限让问题更难发现**:
# 它把 108 条噪音裁成 5 条,看起来正好像"确实有几个参数改了没生效"。
# 要加这类上限,先确认底下那一百多条不是噪音。


_RULES = (_dangerous, _archive, _thread_pool, _memory_overcommit,
          _weak_password_crypto, _no_timeouts)


def audit(st: Dict[str, Setting]) -> List[Finding]:
    """跑全部规则。**互不短路** —— 一个实例可以同时犯好几条。"""
    out: List[Finding] = []
    for rule in _RULES:
        out.extend(rule(st))
    return out


def _b(n: float) -> str:
    f = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if f < 1024 or u == "T":
            return "%.1f%s" % (f, u)
        f /= 1024
    return "%.1fT" % f
