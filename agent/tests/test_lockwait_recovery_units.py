"""kill 语句的生成 —— **只生成，不执行**。

三条规矩：
  1. 只对根 holder 生成 —— 杀中间节点不解堵
  2. 按 holder 状态选函数 —— active 用 cancel（保住会话），
     idle in transaction 用 terminate（cancel 对它无效）
  3. 每条旁边注明会杀掉谁 —— 让人能自己判断代价，而不是照抄

两个分支现在都用两参数、会话感知的函数 —— pg_cancel_session(pid, sessionid)
与 pg_terminate_session(pid, sessionid)，不是单参数的 pg_cancel_backend(pid) /
pg_terminate_backend(pid)。理由见 recovery.py 模块 docstring：两参数版本
**失败是关闭的**（fail closed）—— pid 与 sessionid 对不上同一个会话就返回
false、什么也不做；单参数版本只认 pid，而本环境线程池开着（enable_thread_
pool=on），pid 是会被复用的线程号，诊断到执行之间的时间差里单参数版本可能
杀错人，两参数版本不会。

实测记录（在自己开的 scratch pg_sleep 会话上做的，非任何已存在的会话）：
  pg_cancel_session(pid, sessionid)     -> True，sleep 立即被中断
  pg_cancel_session(sessionid, pid)     -> False，sleep 照常跑满全程
  pg_terminate_session(pid, sessionid)  -> True，连接立即被断开
  pg_terminate_session(sessionid, pid)  -> False，sleep 照常跑满全程
确认参数顺序是 (pid, sessionid)，且顺序颠倒时两个函数都精确地「什么也不做」。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-lockwait" / "scripts"))

from recovery import kill_for, render_kills  # noqa: E402


def _holder(**kw):
    base = dict(holder_pid=281440306779808, holder_sessionid=2259,
                holder_state="active", holder_user="gaussdb",
                holder_app="gsql", holder_xact_age_s=35.4,
                holder_query="UPDATE accounts SET bal = bal - 1 WHERE id = 7")
    base.update(kw)
    return base


def test_active_holder_gets_cancel_not_terminate():
    """正在跑语句的：取消语句就够了，保住会话 —— 代价小得多。"""
    k = kill_for(_holder(holder_state="active"))
    assert k.function == "pg_cancel_session"
    assert "pg_cancel_session(281440306779808, 2259)" in k.sql


def test_idle_in_transaction_holder_needs_terminate():
    """**cancel 对它无效** —— 它没在跑语句，只是攥着锁不放事务。"""
    k = kill_for(_holder(holder_state="idle in transaction"))
    assert k.function == "pg_terminate_session"
    assert "pg_terminate_session(281440306779808, 2259)" in k.sql


def test_idle_in_transaction_aborted_also_needs_terminate():
    k = kill_for(_holder(holder_state="idle in transaction (aborted)"))
    assert k.function == "pg_terminate_session"


def test_unknown_state_falls_back_to_terminate():
    """状态取不到时选更强的那个 —— 选错成 cancel 的话操作看似成功、
    锁还在，人会以为处理过了。选 terminate 至少确实解堵。"""
    k = kill_for(_holder(holder_state=""))
    assert k.function == "pg_terminate_session"


def test_cancel_statement_uses_both_pid_and_sessionid():
    """cancel 分支现在也是两参数、顺序 (pid, sessionid) —— 与 terminate 对称，
    都是失败关闭的会话感知函数，不是只认 pid 的单参数版本。"""
    k = kill_for(_holder(holder_pid=123, holder_sessionid=999, holder_state="active"))
    assert "pg_cancel_session(123, 999)" in k.sql


def test_terminate_statement_uses_both_pid_and_sessionid():
    """terminate 分支：pg_terminate_session 需要 (pid, sessionid) 两个参数，
    顺序为 pid 在前、sessionid 在后 —— 官方文档给出的顺序，并且已经在
    scratch 会话上实测确认过（见本文件顶部的实测记录）。"""
    k = kill_for(_holder(holder_pid=123, holder_sessionid=999,
                          holder_state="idle in transaction"))
    assert "pg_terminate_session(123, 999)" in k.sql


def test_impact_names_who_gets_killed():
    """照抄之前得看得见代价。"""
    k = kill_for(_holder())
    for token in ("gaussdb", "gsql", "35.4", "2259"):
        assert token in k.impact, "impact 里缺 %s" % token


def test_impact_includes_the_running_sql():
    k = kill_for(_holder())
    assert "UPDATE accounts" in k.impact


def test_impact_shows_placeholder_when_xact_age_missing():
    """holder_xact_age_s 取不到时不能把字面 None 印进报告里。"""
    k = kill_for(_holder(holder_xact_age_s=None))
    assert "None" not in k.impact
    assert "?" in k.impact


def test_impact_shows_zero_xact_age_not_placeholder():
    """0 是「事务刚开始」这个真实、有信息量的值（恰恰是 cancel 最便宜的场景），
    不是「取不到」——不能用 `or "?"` 那种真值判断，0 是 falsy，
    `0 or "?"` 会求值成 "?"，把 0 伪装成未知。必须显式判 None。"""
    k = kill_for(_holder(holder_xact_age_s=0))
    assert "持续 0 秒" in k.impact
    assert "持续 ? 秒" not in k.impact


def test_impact_shows_placeholder_when_xact_age_is_empty_string():
    """真实协议把 NULL 渲染成空字符串，不是 Python 的 None
    （common/grmp/serialize.py 的 render_cell 对 None 走
    settings.null_text，默认就是空串；common/grmp/values.py 的
    is_null() 就是为这件事存在的）。裸 `is None` 判断在这个协议下永远
    不会命中——这条测试专门钉住这一点：holder_xact_age_s="" 时也要显示
    占位符，而不是把空串原样拼进 impact 里（"事务已持续  秒"，中间一段
    空白，比印出字面 None 更容易被忽略）。"""
    k = kill_for(_holder(holder_xact_age_s=""))
    assert "None" not in k.impact
    assert "事务已持续  秒" not in k.impact
    assert "?" in k.impact


def test_render_says_do_not_execute():
    out = render_kills([kill_for(_holder())])
    assert "不要直接执行" in out or "不得执行" in out


def test_render_of_nothing_is_explicit():
    """**没有可生成的语句要明说**，不能返回空串 —— 空白会被读成「这段没生成」。"""
    out = render_kills([])
    assert out.strip(), "空结果必须有明确文字"
    assert "无" in out
