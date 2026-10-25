"""lockwait 入口。用假 runner，不连库。"""
import io
import json
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-lockwait" / "scripts"))

import lockwait  # noqa: E402
from common.finding import Severity  # noqa: E402


def _pair(**kw):
    base = dict(waiter_pid="1002", waiter_sessionid="2260",
                waiter_mode="AccessShareLock",
                holder_pid="1001", holder_sessionid="2259",
                holder_mode="AccessExclusiveLock",
                locktype="relation", lock_object="public.t",
                locktag="3985:b2123:0:0:0:0", waiter_wait_s="4.0",
                waiter_user="app", waiter_app="gsql",
                waiter_query="SELECT count(*) FROM t",
                holder_state="active", holder_user="gaussdb",
                holder_app="gsql", holder_xact_age_s="10.0",
                holder_query="LOCK TABLE t IN ACCESS EXCLUSIVE MODE")
    base.update(kw)
    return base


class _Runner:
    def __init__(self, pairs=None, edges=None):
        self._pairs = pairs if pairs is not None else []
        self._edges = edges if edges is not None else []

    def run(self, script, values=None):
        if script == "lockwait.pairs":
            return self._pairs
        if script == "lockwait.chain":
            return self._edges
        raise AssertionError("没料到的脚本 %s" % script)


def test_no_blocking_is_reported_explicitly(monkeypatch, capsys):
    """**没有锁堵塞是正常状态。** 必须明说，不能留空白 ——
    空白会被读成「这项没查」。"""
    monkeypatch.setattr(lockwait.access, "for_conn", lambda *a, **k: _Runner())
    rc = lockwait.main(["-c", "x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "当前无锁等待" in out


def test_pair_reports_both_modes_and_the_conflict_reason():
    rep = lockwait.collect(_Runner(pairs=[_pair()]), limit=20)
    md = lockwait.render_markdown(rep)
    assert "AccessExclusiveLock" in md and "AccessShareLock" in md
    assert "互斥" in md


def test_root_is_the_top_of_the_chain_not_the_direct_blocker():
    """3 等 2、2 等 1 —— 根是 1。杀 2 不解堵。"""
    edges = [{"sessionid": "3", "block_sessionid": "2"},
             {"sessionid": "2", "block_sessionid": "1"}]
    rep = lockwait.collect(_Runner(pairs=[_pair(waiter_sessionid="3")], edges=edges), limit=20)
    assert rep.roots[3] == 1


def test_deadlock_is_critical():
    edges = [{"sessionid": "1", "block_sessionid": "2"},
             {"sessionid": "2", "block_sessionid": "1"}]
    rep = lockwait.collect(_Runner(pairs=[_pair()], edges=edges), limit=20)
    codes = {f.code: f.severity for f in rep.findings}
    assert codes.get("LOCK_DEADLOCK") is Severity.CRITICAL


def test_long_wait_is_critical():
    rep = lockwait.collect(_Runner(pairs=[_pair(waiter_wait_s="90.0")]), limit=20)
    assert any(f.code == "LOCK_WAIT_LONG" and f.severity is Severity.CRITICAL
               for f in rep.findings)


def test_short_wait_is_only_a_notice():
    rep = lockwait.collect(_Runner(pairs=[_pair(waiter_wait_s="1.0")]), limit=20)
    sevs = {f.code: f.severity for f in rep.findings}
    assert sevs.get("LOCK_BLOCKED") is Severity.NOTICE
    assert "LOCK_WAIT_LONG" not in sevs


def test_idle_in_transaction_root_is_flagged():
    rep = lockwait.collect(
        _Runner(pairs=[_pair(holder_state="idle in transaction")]), limit=20)
    assert any(f.code == "LOCK_ROOT_IDLE_XACT" for f in rep.findings)


def test_json_output_is_the_finding_contract(monkeypatch, capsys):
    """health 汇总认的就是这个形状。"""
    monkeypatch.setattr(lockwait.access, "for_conn",
                        lambda *a, **k: _Runner(pairs=[_pair(waiter_wait_s="90.0")]))
    rc = lockwait.main(["-c", "x", "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skill"] == "gaussdb-lockwait"
    assert payload["findings"][0]["skill"] == "gaussdb-lockwait"
    assert isinstance(payload["findings"][0]["severity"], int)


def test_query_failure_is_reported_not_thrown(monkeypatch, capsys):
    """取数失败要给错误信息，不能吐 Traceback。"""
    from common.grmp.errors import QueryError

    class _Boom:
        def run(self, *a, **k):
            raise QueryError("ERROR: permission denied (SQLSTATE 42501)")

    monkeypatch.setattr(lockwait.access, "for_conn", lambda *a, **k: _Boom())
    rc = lockwait.main(["-c", "x"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "Traceback" not in err
    assert "SQLSTATE" in err


# ---------------------------------------------------------------------------
# 四点核对（brief 写成时 Task 4-6 还没定型，这里补测新出现的行为）
# ---------------------------------------------------------------------------

def test_non_conflicting_holder_is_filtered_out_as_innocent_bystander():
    """lockwait.pairs 按 locktag 自连接，会把同一把锁上模式其实不冲突的
    holder 也配进来（无辜旁观者）——一个真冲突 + 一个旁观者，旁观者不能
    出现在报告里。"""
    real = _pair()  # AccessShare(waiter) vs AccessExclusive(holder) —— 真冲突
    bystander = _pair(holder_pid="9001", holder_sessionid="9001",
                       holder_mode="AccessShareLock")  # 与 AccessShare 不冲突
    rep = lockwait.collect(_Runner(pairs=[real, bystander]), limit=20)
    assert len(rep.pairs) == 1
    assert rep.pairs[0]["holder_sessionid"] == "2259"


def test_all_bystanders_leave_pairs_empty_and_report_says_so():
    bystander = _pair(holder_mode="AccessShareLock")  # 与默认 waiter 模式不冲突
    rep = lockwait.collect(_Runner(pairs=[bystander]), limit=20)
    assert rep.pairs == []
    md = lockwait.render_markdown(rep)
    assert "当前无锁等待" in md


def test_deadlock_with_no_pairs_does_not_claim_no_waiting():
    """pairs 和 chain 是两条独立查询，中间有时间差：理论上可能查到死锁环、
    却拿不到对应的持有者/等待者明细。这时不能说"当前无锁等待"——
    那与死锁环矛盾，比留空更误导。"""
    edges = [{"sessionid": "1", "block_sessionid": "2"},
             {"sessionid": "2", "block_sessionid": "1"}]
    rep = lockwait.collect(_Runner(pairs=[], edges=edges), limit=20)
    assert rep.pairs == []
    assert rep.deadlocks
    md = lockwait.render_markdown(rep)
    assert "当前无锁等待" not in md
    assert "死锁" in md


def test_unknown_lock_mode_is_kept_not_silently_dropped():
    """矩阵不认识的模式是数据库给出的真实信号，不能因为判不出冲突就当成
    旁观者悄悄丢掉；也不能让 KeyError 冒穿到用户面前。"""
    weird = _pair(holder_mode="SomeBrandNewModeNobodyHasSeen")
    rep = lockwait.collect(_Runner(pairs=[weird]), limit=20)
    assert len(rep.pairs) == 1
    md = lockwait.render_markdown(rep)  # 不能抛
    assert "SomeBrandNewModeNobodyHasSeen" in md


def test_null_wait_duration_is_not_folded_into_zero():
    """**真实协议把 NULL 渲染成空串，不是 Python None**
    （common/grmp/serialize.py: render_cell 对 None 走 settings.null_text，
    默认就是空串；common/grmp/values.py 的 is_null() 就是为这件事存在的）。
    空串形式的未知时长不能被显示成 0，也不能让严重度判定把它当"不严重"。"""
    rep = lockwait.collect(_Runner(pairs=[_pair(waiter_wait_s="")]), limit=20)
    md = lockwait.render_markdown(rep)
    assert "0.0" not in md
    codes = {f.code: f.severity for f in rep.findings}
    assert codes.get("LOCK_WAIT_LONG") is Severity.CRITICAL


def test_none_wait_duration_is_also_handled_defensively():
    """is_null() 同时认 None 和空串——万一某天来了个走原生类型的驱动，
    照样不能被当 0。"""
    rep = lockwait.collect(_Runner(pairs=[_pair(waiter_wait_s=None)]), limit=20)
    md = lockwait.render_markdown(rep)
    assert "0.0" not in md
    codes = {f.code: f.severity for f in rep.findings}
    assert codes.get("LOCK_WAIT_LONG") is Severity.CRITICAL


def test_zero_wait_duration_is_shown_as_zero_not_unknown():
    """反方向的坑：真正的 0 不能被当成"取不到"——0 秒等待信息量很大
    （cancel 代价最小的那种情形），`0 or 默认值` 那种写法会把它吞掉。"""
    rep = lockwait.collect(_Runner(pairs=[_pair(waiter_wait_s="0.0")]), limit=20)
    md = lockwait.render_markdown(rep)
    assert "0.0" in md
    assert "未知" not in md
    sevs = {f.code: f.severity for f in rep.findings}
    assert sevs.get("LOCK_BLOCKED") is Severity.NOTICE


def test_idle_xact_with_unknown_age_does_not_print_literal_none():
    rep = lockwait.collect(
        _Runner(pairs=[_pair(holder_state="idle in transaction",
                              holder_xact_age_s="")]), limit=20)
    f = next(f for f in rep.findings if f.code == "LOCK_ROOT_IDLE_XACT")
    assert "None" not in f.evidence


def _root_edge():
    """让默认 _pair() 里的 holder（2259）在 chain 里被识别成根 ——
    kill 语句只对根 holder 生成，不给这条边就永远没有 kill 语句可测。"""
    return [{"sessionid": "2260", "block_sessionid": "2259"}]


def test_kill_statements_use_the_two_argument_session_aware_functions():
    """recovery.py 生成的函数已经改名——两个参数、会话感知，
    不是单参数的 pg_cancel_backend/pg_terminate_backend。"""
    rep = lockwait.collect(_Runner(pairs=[_pair()], edges=_root_edge()), limit=20)
    md = lockwait.render_markdown(rep)
    assert "pg_cancel_session(" in md or "pg_terminate_session(" in md
    assert "pg_cancel_backend(" not in md
    assert "pg_terminate_backend(" not in md


def test_garbled_holder_pid_does_not_crash_kill_generation():
    """recovery.kill_for() 内部是裸 int(x or 0)——真实查询结果都是字符串，
    非数字字符串会让它抛。lockwait 是第一个把真实取数结果接进 kill_for()
    的调用方，责任在这一层：不能让这个 Traceback 冒到用户面前，也不能
    编一个假 pid 生成一条看着正常、其实指向别的会话的 kill 语句。"""
    rep = lockwait.collect(
        _Runner(pairs=[_pair(holder_pid="not-a-number")], edges=_root_edge()),
        limit=20)
    md = lockwait.render_markdown(rep)  # 不能抛
    assert "not-a-number" in md or "无法生成" in md
    assert "无 —— 当前没有需要处理的根阻塞会话" not in md


def test_main_does_not_crash_on_any_of_the_above_via_json_format(monkeypatch, capsys):
    """--format json 走的是同一条 collect() 路径，同样不能抛。"""
    pairs = [_pair(holder_mode="SomeBrandNewModeNobodyHasSeen",
                    waiter_wait_s="", holder_pid="not-a-number")]
    monkeypatch.setattr(lockwait.access, "for_conn",
                        lambda *a, **k: _Runner(pairs=pairs))
    rc = lockwait.main(["-c", "x", "--format", "json"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Fix round 1：review 发现的两个 Important + 一个 Minor
# ---------------------------------------------------------------------------

def test_kill_section_is_not_a_bare_none_when_chain_data_is_missing():
    """pairs 与 chain 是两条独立查询，覆盖面可能不一致：一个在 pairs 里
    真实冲突的 holder，chain 完全没提到对应的 waiter。这时"快速恢复语句"
    小节不能直接印"无"——那会被读成"已确认没事"，而事实是"没能确认"，
    是这个项目通篇在防的"看似正常、实则没查"，而且恰好出现在报告里
    最要紧的那个位置。"""
    rep = lockwait.collect(_Runner(pairs=[_pair()], edges=[]), limit=20)
    md = lockwait.render_markdown(rep)
    assert "## 快速恢复语句" in md
    section = md[md.index("## 快速恢复语句"):]
    assert "无 —— 当前没有需要处理的根阻塞会话" not in section
    assert "未能确认根" in section
    assert "1" in section  # 数量要出现，不能只说"有一些"


def test_kill_section_lists_both_confirmed_kill_and_unconfirmed_caveat():
    """混合场景：一对有 chain 数据能确认根，另一对完全没有——已确认的
    kill 语句照常生成，未确认的说明也必须同时出现在同一小节里，
    不能被已确认的那条挡住视线。"""
    edges = [{"sessionid": "2260", "block_sessionid": "2259"}]  # 只覆盖这一个 waiter
    confirmed_pair = _pair()  # waiter=2260, holder=2259 —— chain 能确认
    unconfirmed_pair = _pair(waiter_pid="2002", waiter_sessionid="9999",
                              holder_pid="8001", holder_sessionid="8000")
    rep = lockwait.collect(
        _Runner(pairs=[confirmed_pair, unconfirmed_pair], edges=edges), limit=20)
    md = lockwait.render_markdown(rep)
    section = md[md.index("## 快速恢复语句"):]
    assert "pg_cancel_session(" in section or "pg_terminate_session(" in section
    assert "未能确认根" in section


def test_null_holder_pid_does_not_silently_become_zero():
    """holder_pid 缺失（NULL，协议里是空串）不能被 as_int() 的默认值 0
    悄悄顶替——那会生成一条看着正常、其实 pid 是编出来的 kill 语句。"""
    rep = lockwait.collect(
        _Runner(pairs=[_pair(holder_pid="")], edges=_root_edge()), limit=20)
    md = lockwait.render_markdown(rep)
    section = md[md.index("## 快速恢复语句"):]
    assert "pg_cancel_session(0" not in section
    assert "pg_terminate_session(0" not in section
    assert "未能生成" in section
    assert "无 —— 当前没有需要处理的根阻塞会话" not in section


# ---------------------------------------------------------------------------
# Fix round 2：review 发现同一个矛盾还有第二扇门（confirmed 全部生成
# 失败、且没有 unconfirmed 时，旧代码仍会落到 render_kills([]) 的
# "无"）。改成一条参数化不变量测试覆盖组合，而不是每发现一个分支
# 补一条——按分支修正是上一轮漏掉这个分支的原因。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("edges,pair_kwargs,label", [
    ([], {}, "pairs 存在，chain 完全没有该 waiter 的数据"),
    ([{"sessionid": "2260", "block_sessionid": "2259"}], {"holder_pid": ""},
     "根已被 chain 确认，但 kill 语句因 pid 缺失而生成失败"),
    ([{"sessionid": "2260", "block_sessionid": "9999"}], {},
     "chain 确认了根（9999），但这个根没有出现在 pairs 的任何一行"
     "holder 里"),
])
def test_pairs_non_empty_never_yields_the_bare_none(edges, pair_kwargs, label):
    """不变量：只要 rep.pairs 非空，"快速恢复语句"小节就绝不能出现裸的
    "无 —— 当前没有需要处理的根阻塞会话"。这句话只在压根没有阻塞对时
    才成立；不管走哪条分支（chain 缺数据、kill 生成失败、根不在 pairs
    覆盖范围内……）都不能落到这句话上。"""
    rep = lockwait.collect(
        _Runner(pairs=[_pair(**pair_kwargs)], edges=edges), limit=20)
    assert rep.pairs, "前提：这一步必须真的产出非空 pairs，否则测试没有意义"
    md = lockwait.render_markdown(rep)
    assert "## 快速恢复语句" in md
    section = md[md.index("## 快速恢复语句"):]
    assert "无 —— 当前没有需要处理的根阻塞会话" not in section, label


def test_unconfirmed_caveat_counts_by_pair_not_by_holder():
    """两个不同的 waiter 被同一个未确认根 holder 挡住时，"N 对……"里的
    N 和枚举都要数到 2，不能因为 holder 相同就被去重折叠成 1——这一段
    统计和列出的单位是"对阻塞关系"，按 holder 折叠会让数字和枚举都比
    实际少，而这一段的全部职责就是把这些缺口如实列出来。"""
    pair_a = _pair(waiter_sessionid="3001", holder_sessionid="9000")
    pair_b = _pair(waiter_sessionid="3002", holder_sessionid="9000")
    rep = lockwait.collect(_Runner(pairs=[pair_a, pair_b], edges=[]), limit=20)
    md = lockwait.render_markdown(rep)
    section = md[md.index("## 快速恢复语句"):]
    assert "2 对阻塞关系" in section
    assert "3001" in section and "3002" in section


# ---------------------------------------------------------------------------
# Fix round 3：review 发现前两轮堵的都是"聚合状态"的漏洞——一份报告里
# 同时混几条独立的阻塞链时，某一条落进"没材料生成语句"的分类，但因为
# **另一条**链成功生成了语句，kills 非空，兜底分支的触发条件
# （`if not kills and not reasons`）看的是整体，不是这一条本身，于是
# 这一条被静默放过。改成属性测试：不管 pairs 里混了多少种结果，
# rep.pairs 里每一个 waiter 会话号都必须能在恢复语句小节的文本里找到，
# 不针对某一个具体分支写断言——这样以后再加一种新分类，测试依然管用。
# ---------------------------------------------------------------------------

def test_every_pair_is_accounted_for_in_the_recovery_section():
    """属性测试：一份报告里混了五种结果——
      100 ← 200  根已确认，kill 语句成功生成
      101 ← 300  根是 999，但 999 没有作为任何一对的 holder 出现在 pairs 里（orphan_root）
      102 ← 400  chain 完全没有这个 waiter 的数据（data_gap）
      103 ← 500  根已确认，但 holder_pid 缺失，kill 语句生成失败
      105 ← 250  链条中间节点：链条的根是 200，200 已经在上面被确认为根
    这不是按分支各写一条断言，而是不管走哪条分支、组合成什么样，
    rep.pairs 里出现过的每一个 waiter 会话号都必须能在"快速恢复语句"
    这一节的文本里找到——这正是 review 指出的、前两轮都没堵住的那类
    漏洞：某一对是否被提及，不该取决于**另一对**恰好处于什么状态。"""
    pairs = [
        _pair(waiter_sessionid="100", holder_sessionid="200"),
        _pair(waiter_sessionid="101", holder_sessionid="300"),
        _pair(waiter_sessionid="102", holder_sessionid="400"),
        _pair(waiter_sessionid="103", holder_sessionid="500", holder_pid=""),
        _pair(waiter_sessionid="105", holder_sessionid="250"),
    ]
    edges = [
        {"sessionid": "100", "block_sessionid": "200"},
        {"sessionid": "101", "block_sessionid": "999"},
        {"sessionid": "103", "block_sessionid": "500"},
        {"sessionid": "105", "block_sessionid": "250"},
        {"sessionid": "250", "block_sessionid": "200"},
        # 102 故意不给边：chain 完全没有这个 waiter 的数据
    ]
    rep = lockwait.collect(_Runner(pairs=pairs, edges=edges), limit=20)
    assert len(rep.pairs) == 5, "前提：五对都要真的活过冲突过滤，否则测试没有意义"
    md = lockwait.render_markdown(rep)
    assert "## 快速恢复语句" in md
    section = md[md.index("## 快速恢复语句"):]
    for p in rep.pairs:
        wid = str(p.get("waiter_sessionid"))
        assert wid in section, (
            "会话 %s（对应的 pair 出现在阻塞明细里）从「快速恢复语句」"
            "小节的文本里消失了——每一对都必须被覆盖或给出理由，"
            "不能因为报告里别的对处于别的状态就被捎带忽略" % wid)


# ---------------------------------------------------------------------------
# Fix round 4：round 3 的四分类保证了"没有一对被静默略过"，但它对
# `intermediate` 那一类说出的是一句**肯定断言**——"这一对不用单独管，
# 处理掉根就顺带解开了"——而判据只有"这个 waiter 的根恰好也出现在
# pairs 的某个 holder 列里"，从来没有验证过**这一对自己的 holder** 与
# 那个根之间有任何关系。
#
# `lockwait.pairs`（pg_locks 自连接）一个 waiter 可以有好几个真冲突的
# holder（一条 DDL 等 AccessExclusive，被好几个 AccessShare 读者同时
# 挡着，是最平常的现场）；`lockwait.chain`（pg_thread_wait_status）
# 每个等待者只记一个阻塞者。两边覆盖面不对称时，第二个 holder 会被
# 说成"中间节点、根另有处理"——**杀掉根它照样还在挡着**。
#
# 这比前三轮的"漏掉一对"更糟：那是沉默，这是在读的人正准备结束故障的
# 那一节里，给他一句错的肯定。下面这条属性测试钉的就是第二条不变量：
# **除非数据真的建立起了连接，否则不许对任何一对说"它已经被别处覆盖"。**
# 与 round 3 那条一样，断言写成对形状通用的性质，不针对某一个分支，
# 将来再加第六个分类它依然管用。
# ---------------------------------------------------------------------------

_COVERED_CLAIM = "会一并解开"   # 渲染层"这一对已被针对根的处理覆盖"的标记

_COVERAGE_SHAPES = [
    # ① review 给的最小复现：一个 waiter 被两个会话**各自独立**挡着
    #    （两个 holder 与它的锁模式都真冲突），chain 只跟踪了其中一个。
    ("一个 waiter 两个独立 holder，chain 只记了其中一个",
     [("100", "200"), ("100", "201")],
     [("100", "200")]),
    # ② 真正的中间节点：250 自己在等 200，杀 200 确实会让 250 解开。
    ("真正的中间节点（250 自己在等 200）",
     [("100", "200"), ("105", "250")],
     [("100", "200"), ("105", "250"), ("250", "200")]),
    # ③ 旁支：waiter 同时被根和另一个"自己也在等同一个根"的会话挡住 ——
    #    holder 不在 waiter 通往根的那条路径上，但同样会被根的处理解开，
    #    这一类**应该**算已覆盖（防止修得过头，把真覆盖也说成没覆盖）。
    ("旁支 holder 自己也在等同一个根",
     [("100", "200"), ("100", "250")],
     [("100", "200"), ("250", "200")]),
    # ④ chain 对同一个 waiter 给了两条边（chain._blocker_of 只留第一条，
    #    是单独记账的既有债务）——被丢掉的那条边对应的 holder 不能因此
    #    被说成"已覆盖"。
    ("chain 同一个 waiter 多条边，只有第一条被建成链",
     [("100", "201")],
     [("100", "200"), ("100", "201")]),
    # ⑤ 死锁环。
    ("死锁环", [("100", "200")], [("100", "200"), ("200", "100")]),
    # ⑥ 根已被 chain 确认，但没有作为任何一对的 holder 出现在 pairs 里。
    ("根已确认但不在 pairs 的持有者列里",
     [("100", "250")], [("100", "250"), ("250", "999")]),
    # ⑦ chain 完全没有数据。
    ("chain 完全没有数据", [("100", "200")], []),
]


def _waits_on_via_raw_edges(raw_edges, start, target):
    """测试侧**独立**实现：只看 `lockwait.chain` 返回的原始边，判断
    start 是否（传递地）在等 target。

    故意不复用 lockwait.py 里的任何函数——拿被测代码自己的判断去验证
    被测代码，等于什么都没验证。这里刻意写得比实现宽松（只要有**一条**
    路径连得上就算连得上），因为这条测试要卡的是一个**必要**条件：
    实现可以比它更保守，但绝不能在连它都连不上的时候声称"已覆盖"。
    """
    blockers = {}
    for e in raw_edges:
        blockers.setdefault(int(e["sessionid"]), set()).add(int(e["block_sessionid"]))
    if start == target:
        return True
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for nxt in blockers.get(cur, ()):
            if nxt == target:
                return True
            stack.append(nxt)
    return False


def test_no_pair_is_claimed_covered_unless_the_chain_data_connects_it():
    """属性：**没有任何一对可以在数据没建立起连接的情况下被说成"已被别处
    覆盖"。** 对每一种形状都同时查两层——

      分类层：落进"中间节点"这一类的每一对，holder 必须真的（传递地）
              在等这个 waiter 的根；
      渲染层：文本里带"已覆盖"标记的每一行，同样要过这一关。

    顺带继续钉住 round 3 的第一条不变量（每一对都必须被提到），并加强
    成 waiter 与 holder **两个**会话号都要出现——读的人要知道去处理谁。
    """
    saw_claim = False
    for label, pair_ids, edge_ids in _COVERAGE_SHAPES:
        pairs = [_pair(waiter_sessionid=w, holder_sessionid=h)
                 for w, h in pair_ids]
        raw_edges = [{"sessionid": s, "block_sessionid": b} for s, b in edge_ids]
        rep = lockwait.collect(_Runner(pairs=pairs, edges=raw_edges), limit=20)
        assert len(rep.pairs) == len(pair_ids), (
            "前提：%s —— 每一对都要真的活过冲突过滤，否则这个形状没验证到东西"
            % label)

        buckets = lockwait._classify_pairs(rep)

        # round 3 的不变量：分类是全函数，每一对恰好落进一个桶。
        assert (sorted(id(p) for bucket in buckets.values() for p in bucket)
                == sorted(id(p) for p in rep.pairs)), (
            "%s：分类不再是全函数——有的对落进了两个桶，或者一个都没落进" % label)

        # 本轮的不变量（分类层）。
        for p in buckets["intermediate"]:
            w = int(p["waiter_sessionid"])
            h = int(p["holder_sessionid"])
            root = rep.roots.get(w)
            assert root is not None and _waits_on_via_raw_edges(raw_edges, h, root), (
                "%s：把 %s ← %s 归成「中间节点、根另有处理」，但阻塞链数据里"
                "没有任何证据表明会话 %s 在等根会话 %s —— %s 可能是一个独立的"
                "共同阻塞者，处理掉根它照样挡着，这句话是在读的人最需要准确"
                "信息的时候给了他一个错的肯定" % (label, w, h, h, root, h))

        md = lockwait.render_markdown(rep)
        assert "## 快速恢复语句" in md, label
        section = md[md.index("## 快速恢复语句"):]

        # 本轮的不变量（渲染层）。
        for line in section.splitlines():
            if _COVERED_CLAIM not in line:
                continue
            for w_s, h_s in re.findall(r"会话 (\d+) ← (\d+)", line):
                saw_claim = True
                w, h = int(w_s), int(h_s)
                root = rep.roots.get(w)
                assert root is not None and _waits_on_via_raw_edges(raw_edges, h, root), (
                    "%s：这一行对 %s ← %s 说了「%s」，但阻塞链数据里没有任何"
                    "证据表明会话 %s 在等根会话 %s：%s"
                    % (label, w, h, _COVERED_CLAIM, h, root, line))

        # round 3 不变量的加强版：两个会话号都要出现。
        for p in rep.pairs:
            for role, sid in (("等待", p["waiter_sessionid"]),
                              ("持有", p["holder_sessionid"])):
                assert str(sid) in section, (
                    "%s：%s会话 %s 出现在阻塞明细里，却在「快速恢复语句」"
                    "小节里找不到" % (label, role, sid))

    assert saw_claim, (
        "没有任何一行被标成「%s」——这条测试于是空转了：形状 ②③ 里的"
        "中间节点是真的会被根的处理解开，实现应当明确这么说；如果标记"
        "文案改了，这条测试必须跟着改，而不是静默变成摆设" % _COVERED_CLAIM)
