"""快速恢复语句的生成 —— **只生成文本，绝不执行**。

按 holder 的状态选函数，理由是这两个函数解决的不是同一件事：

  active                  正在跑语句 → pg_cancel_session 取消这条语句，
                          会话还在、事务还在，代价最小
  idle in transaction     没在跑语句，只是攥着锁不放事务 →
                          **cancel 对它无效**（没有语句可取消），
                          只能 pg_terminate_session 断掉会话

状态取不到时选 terminate：选错成 cancel 的后果是「命令成功了但锁还在」，
而操作的人会以为已经处理过 —— 那比多断一个会话糟得多。

两个函数都用两参数、会话感知的版本 —— pg_cancel_session(pid, sessionid) /
pg_terminate_session(pid, sessionid)，不是单参数的 pg_cancel_backend(pid) /
pg_terminate_backend(pid)。原因不是「两参数版本更精确」这么简单，而是它有
一个单参数版本没有的性质：

    **两参数版本失败是关闭的（fail closed）**：pid 和 sessionid 必须同时对上
    同一个会话，函数才会动手；对不上就返回 false，什么也不做。单参数版本
    只认 pid——而 openGauss 开着线程池（本环境 enable_thread_pool=on）时，
    pid 是线程号，会被系统复用给别的会话（pairs.yaml 的表头注释已经测出这
    一点）。诊断报告生成到人工执行之间有时间差，这段时间里 pid 完全可能已
    经被复用：单参数版本这时候会伤及无辜——把别人的会话/语句杀掉，而自己
    毫无察觉；两参数版本这时候只会返回 false，什么都不发生。

    也正因为「失败是关闭的」，**参数顺序错了也不危险，只是这条语句失效**
    （返回 false、无副作用），不会杀错会话——这也是本模块敢在没有把
    pg_cancel_session 的参数顺序执行验证之前，就已经可以用它的原因：即便
    顺序猜错，最坏后果是「语句什么都没做」而不是「杀错了人」。

实测记录：

  Step 1（查 pg_proc.pronargs，og5 / openGauss-lite 5.0.3）：
    pg_cancel_backend(pid)                pronargs=1
    pg_terminate_backend(pid)             pronargs=1
    pg_terminate_session(pid, sessionid)  pronargs=2
    （后来在 pg_proc 里另外发现 pg_cancel_session(pid, sessionid) 同样存在，
    pronargs=2，但两者都未见于 openGauss 官方文档的 server-signal-functions
    页面下的 pg_cancel_session 条目——文档只记录了 pg_terminate_session
    (pid int64, sessionid int64) 这一个双参数函数。）

  自建 scratch 会话（本进程自己开的 pg_sleep 会话，非任何已存在的业务会话）
  做的参数顺序实测：
    pg_cancel_session(pid, sessionid)     -> True，sleep 立即被中断
    pg_cancel_session(sessionid, pid)     -> False，sleep 照常跑满全程
    pg_terminate_session(pid, sessionid)  -> True，连接立即被断开
    pg_terminate_session(sessionid, pid)  -> False，sleep 照常跑满全程

  确认了：(1) 参数顺序是 (pid, sessionid)，与官方文档给 pg_terminate_session
  标注的顺序一致；(2) 两个函数在顺序颠倒时都精确地「什么也不做」，即上面
  说的 fail-closed 性质，不是猜测。
"""
from __future__ import annotations

from dataclasses import dataclass

from common.grmp.values import is_null

# 这些状态下 cancel 无效，必须 terminate
_NEEDS_TERMINATE_PREFIX = "idle in transaction"


@dataclass(frozen=True)
class KillStatement:
    sql: str
    target_sessionid: int
    target_pid: int
    function: str
    why: str
    impact: str


def kill_for(holder: dict) -> KillStatement:
    """给一个根 holder 生成恢复语句。holder 是 lockwait 的 pairs 查询返回的一行字典。"""
    state = str(holder.get("holder_state") or "").strip().lower()
    pid = int(holder.get("holder_pid") or 0)
    sid = int(holder.get("holder_sessionid") or 0)

    if state == "active":
        fn = "pg_cancel_session"
        why = "holder 正在执行语句，取消该语句即可解堵，会话与事务保留"
    else:
        fn = "pg_terminate_session"
        if state.startswith(_NEEDS_TERMINATE_PREFIX):
            why = ("holder 处于 %s —— 没有正在执行的语句，"
                   "cancel 对它无效，只能断开会话" % state)
        else:
            why = ("holder 状态为 %r，无法确认 cancel 是否有效；"
                   "选用更强的 terminate —— 取消失败而锁仍在，"
                   "会让人误以为已经处理过"
                   % (holder.get("holder_state") or ""))

    sql = "SELECT %s(%d, %d);" % (fn, pid, sid)

    xact_age = holder.get("holder_xact_age_s")
    # `or "?"` 在这里不对：0 是「事务刚开始」这个真实、有信息量的值
    # （恰恰是 cancel 最便宜、terminate 明显过度的那种情形），
    # 用 or 会把它和「取不到」混成一样——`0 or "?"` 求值成 "?"。
    # 必须显式判"未知"，不能靠真值判断。
    #
    # **不能只判 `is None`**：这条协议把 NULL 渲染成空字符串 ""，不是
    # Python 的 None（common/grmp/serialize.py 的 render_cell 对
    # value is None 走 settings.null_text，默认就是空串；实测确认 og
    # 连接用的 DirectRunner 同样如此，见 lockwait.py 的说明）。裸
    # `is None` 在真实查询结果面前永远不会命中——被遗弃的预备/2PC 事务
    # （这个字段存在的理由）这一行会渲染成"事务已持续  秒"，中间一段
    # 空白，比印出字面 "None" 更容易被读的人忽略过去。改用
    # common.grmp.values.is_null()，一次认全 "" 和 None 两种形态。
    xact_age_display = "?" if is_null(xact_age) else xact_age
    impact = (
        "会话 %s（pid %s）/ 用户 %s / 应用 %s / 事务已持续 %s 秒；"
        "正在执行：%s"
        % (sid, pid,
           holder.get("holder_user") or "?",
           holder.get("holder_app") or "?",
           xact_age_display,
           (holder.get("holder_query") or "").strip() or "(取不到)")
    )
    return KillStatement(
        sql=sql,
        target_sessionid=sid, target_pid=pid, function=fn,
        why=why, impact=impact,
    )


def render_kills(kills: list) -> str:
    """渲染成报告里的一段。空列表要**明说**，不能返回空串。"""
    if not kills:
        return ("## 快速恢复语句\n\n"
                "无 —— 当前没有需要处理的根阻塞会话。\n")
    out = ["## 快速恢复语句\n",
           "> **这些语句由本 skill 生成，供人工判断后自行执行；"
           "本 skill 不会执行它们，也不要直接执行。**",
           "> 只针对**根**阻塞会话生成 —— 杀链条中间的会话不解堵。\n"]
    for k in kills:
        out.append("```sql\n%s\n```" % k.sql)
        out.append("- 为什么用 `%s`：%s" % (k.function, k.why))
        out.append("- 会杀掉谁：%s\n" % k.impact)
    return "\n".join(out) + "\n"
