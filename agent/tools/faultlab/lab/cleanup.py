"""收尾:杀掉实验会话、逐项撤掉残留、核对到真的干净。

**核对是重点,不是杀完就算完。** 「清理完毕」如果只是「命令都发出去了」,那么下一次运行
会带着上一次的残留开始,而现象看起来一模一样 —— 这种错误能骗过所有人很久。所以每一项
撤完都回查一次,最后把还剩下的东西原样报出来,一件都不掩饰。

**两档:**
  · run()   —— 回到「数据已备好」的状态:杀会话、撤残留、清掉运行期写入的数据。下次可直接再跑。
  · purge() —— 连模式与用户一起删,目标库回到装这套之前的样子。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from . import config as C
from . import state
from .db import MAINT_GUARDS, REAL_SESSION, Conn, LabError, omm_sql

# 运行期会长大的表:每次清理都清空(不然跑几次之后它们比事实表还大)
TRUNCATE_AFTER_RUN = ("wal_log", "uniq_race", "subx_log", "doc_blob")

# 膨胀类场景的表:清理时重建,把空间收回来。
#
# **为什么非做不可:** 这几张表的膨胀是**跨运行累积**的 —— bloat_hot 从备好时的 74 MB
# 一路长到 578 MB(2026-10-17 第四次跑时实测,死元组占 92%)。不收回去的话,
# 下一次运行就是从一张已经很胖的表开始,「膨胀到几倍」这个现象的起点每次都不一样,
# 跑出来的数也没法跨运行比较;跑得多了还会把盘吃光。
VACUUM_FULL_AFTER_RUN = ("bloat_hot", "bloat_slow", "xmin_hold")

# 场景运行期往固定表里插的行:id 都在造数据时的最大 id 之上,清理时按这条界线删掉。
#
# **不删会出事:** K6 / K5 / D5 / D6 都往这些表里插数据。留着的话不但数据越跑越多,
# 还会让下一次运行的插入撞上主键 —— 而那个报错会被当成「预期内的报错」吞掉,
# 表现成「这个场景永远等不到锁」(2026-10-17 第四次跑时 K6 就是这么悄悄失效的)。
DELETE_ABOVE = {"acct_child": 2_000, "order_items": 3_000_000, "dl_child": C.DL_CHILD_ROWS}


def kill_sessions(c: Conn) -> int:
    """杀掉所有 application_name 以 fl: 开头的会话。

    只按前缀杀 —— og5 上还跑着别人的东西(u9500 的日报、别的测试),绝不按「空闲时间长」
    之类的条件去杀。
    """
    infra = tuple(C.APP_PREFIX + a for a in C.INFRA_APPS)
    rows = c.query("SELECT pid, sessionid FROM pg_stat_activity "
                   " WHERE application_name LIKE %%s AND pid <> pg_backend_pid() AND %s "
                   "   AND application_name <> ALL (%%s)" % REAL_SESSION,
                   (C.APP_PREFIX + "%", list(infra)))
    killed = 0
    for pid, sessionid in rows:
        try:
            # 线程池模式下会话和线程不是一对一的,按 (pid, sessionid) 杀才落在正确的会话上
            c.exec("SELECT pg_terminate_session(%s, %s)", (pid, sessionid))
            killed += 1
        except Exception:        # noqa: BLE001 —— 会话可能自己先退了,不是错误
            try:
                c.exec("SELECT pg_terminate_backend(%s)", (pid,))
                killed += 1
            except Exception:    # noqa: BLE001
                pass
    return killed


def _exists(c: Conn, kind: str, name: str) -> bool:
    """这件残留在库里到底在不在。登记簿只说明「可能留下了」,不说明「真的建成了」。"""
    if kind == "prepared_xact":
        return bool(c.one("SELECT count(*) FROM pg_prepared_xacts WHERE gid = %s", (name,)))
    if kind == "slot":
        return bool(c.one("SELECT count(*) FROM pg_replication_slots WHERE slot_name = %s", (name,)))
    if kind == "column":
        tbl, col = name.rsplit(".", 1)
        schema, table = tbl.split(".", 1)
        return bool(c.one("SELECT count(*) FROM pg_attribute a JOIN pg_class t ON t.oid = a.attrelid "
                          "  JOIN pg_namespace n ON n.oid = t.relnamespace "
                          " WHERE n.nspname = %s AND t.relname = %s AND a.attname = %s "
                          "   AND NOT a.attisdropped", (schema, table, col)))
    if kind == "index":
        schema, idx = name.split(".", 1)
        return bool(c.one("SELECT count(*) FROM pg_class i JOIN pg_namespace n ON n.oid = i.relnamespace "
                          " WHERE n.nspname = %s AND i.relname = %s", (schema, idx)))
    return True                     # table_option:RESET 本身就是幂等的,照做


# 登记了、库里却没有的那几种,各自说清楚是怎么回事 —— 以前一律报「已删」,
# 2026-10-24 那轮 K3 的 ALTER 被取消、列根本没加上,清理还写着「已删列」。
_ABSENT = {
    "prepared_xact": "两阶段事务 %s 已不在(没 PREPARE 成,或已被回滚),不用处理",
    "slot": "复制槽 %s 已不在(没建成,或被 WAL 守卫删了),不用处理",
    "column": "列 %s 不存在(那条 ALTER 没做成),不用删",
    "index": "索引 %s 不存在(那次建索引没建成),不用删",
}


def _undo(c: Conn, kind: str, name: str) -> Tuple[str, bool]:
    """撤掉一件残留。**按实际结果报**:先看在不在,不在就说不在,在就撤、撤完再查一遍。

    返回 (一句话说明, 是否已经不在了)。还在的不从登记簿里划掉,下次清理接着收。
    """
    text = _undo_once(c, kind, name)
    return text, not text.endswith(_STILL_THERE)


_STILL_THERE = "要人工看一下"


def _undo_once(c: Conn, kind: str, name: str) -> str:
    if kind not in state.KINDS:
        raise LabError("不认得的残留种类 %r" % kind)
    _safe(name.split(":", 1)[0])
    if not _exists(c, kind, name):
        return _ABSENT[kind] % name
    if kind == "prepared_xact":
        c.exec("ROLLBACK PREPARED '%s'" % _safe(name))
        done = "已回滚两阶段事务 %s" % name
    elif kind == "slot":
        c.exec("SELECT pg_drop_replication_slot(%s)", (name,))
        done = "已删复制槽 %s" % name
    elif kind == "table_option":
        table = name.split(":", 1)[0]
        c.exec("ALTER TABLE %s RESET (autovacuum_enabled, autovacuum_vacuum_cost_delay, "
               "  autovacuum_vacuum_cost_limit, autovacuum_vacuum_scale_factor)" % _safe(table))
        return "已还原 %s 的表级参数" % table
    elif kind == "column":
        tbl, col = name.rsplit(".", 1)
        c.exec("ALTER TABLE %s DROP COLUMN IF EXISTS %s" % (_safe(tbl), _safe(col)))
        done = "已删列 %s" % name
    else:
        c.exec("DROP INDEX IF EXISTS %s" % _safe(name))
        done = "已删索引 %s" % name
    if _exists(c, kind, name):
        return "%s 的命令发了,但它还在 —— %s" % (name, _STILL_THERE)
    return done


def _table_exists(c: Conn, table: str) -> bool:
    return bool(c.one("SELECT count(*) FROM pg_tables WHERE schemaname = %s AND tablename = %s",
                      (C.SCHEMA, table)))


def _safe(ident: str) -> str:
    """库对象名只允许字母、数字、下划线和点 —— 它要拼进 DDL。"""
    if not ident or not all(ch.isalnum() or ch in "_." for ch in ident):
        raise LabError("库对象名不合法:%r" % ident)
    return ident


def residue_check(c: Conn) -> List[str]:
    """直接去库里查还剩什么 —— 不看登记簿,看事实。

    登记簿可能本来就漏记(某个场景忘了登记),所以这里按「实验用的东西长什么样」
    主动搜一遍:fl_ 开头的两阶段事务与复制槽、fl 模式下多出来的列和索引、还活着的会话。
    """
    left: List[str] = []
    n = c.one("SELECT count(*) FROM pg_prepared_xacts WHERE gid LIKE 'fl\\_%'")
    if n:
        left.append("两阶段事务 %s 条" % n)
    n = c.one("SELECT count(*) FROM pg_replication_slots WHERE slot_name LIKE 'fl\\_%'")
    if n:
        left.append("复制槽 %s 个" % n)
    # **只数真会话。** 线程池留下的空闲线程条目(sessionid = 0)不持锁、也没有客户端,
    # 把它们算进残留的话,清理永远「不干净」—— 见 db.REAL_SESSION 的说明。
    n = c.one("SELECT count(*) FROM pg_stat_activity "
              " WHERE application_name LIKE %%s AND pid <> pg_backend_pid() AND %s "
              "   AND application_name <> ALL (%%s)" % REAL_SESSION,
              (C.APP_PREFIX + "%", [C.APP_PREFIX + a for a in C.INFRA_APPS]))
    if n:
        left.append("实验会话 %s 个" % n)
    n = c.one("SELECT count(*) FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace "
              " WHERE ns.nspname = %s AND c.relname LIKE 'idx\\_fl\\_%%'", (C.SCHEMA,))
    if n:
        left.append("跑中途建的索引 %s 个" % n)
    n = c.one("SELECT count(*) FROM pg_attribute a "
              " WHERE a.attrelid = '%s.payments'::regclass AND a.attname LIKE 'fl\\_%%' "
              "   AND NOT a.attisdropped" % C.SCHEMA)
    if n:
        left.append("加出来的列 %s 个" % n)
    # import_stage 的 autovacuum_enabled=false 是**设计的一部分**(schema.sql 里设的,
    # 为了让统计信息永远停在空表),不是残留 —— 所以排除它,否则每次清理都会报一张表没还原。
    n = c.one("SELECT count(*) FROM pg_stat_user_tables t "
              " WHERE t.schemaname = %s AND t.relname <> 'import_stage' "
              "   AND EXISTS (SELECT 1 FROM pg_class c "
              "   WHERE c.oid = t.relid AND c.reloptions::text LIKE '%%autovacuum%%')", (C.SCHEMA,))
    if n:
        left.append("表级参数还没还原的表 %s 张" % n)
    return left


def run() -> Dict[str, object]:
    """收尾清理。先杀会话,再撤残留 —— 顺序不能反:会话还握着的锁会让 DDL 挂住。"""
    out: Dict[str, object] = {}
    # 等锁最多一分钟:会话已经杀过,还等不到锁就说明有别人的会话挡着,如实报失败,别挂住。
    c = Conn("cleanup", guards=MAINT_GUARDS)
    try:
        out["杀掉会话"] = kill_sessions(c)
        done: List[str] = []
        for kind, name in state.residues():
            if kind == "_broken":
                done.append("登记簿文件坏了(%s),只能靠下面的实地核对" % name)
                continue
            try:
                text, gone = _undo(c, kind, name)
                done.append(text)
                if gone:
                    state.drop_residue(kind, name)
            except Exception as exc:         # noqa: BLE001
                done.append("撤 %s %s 失败:%s" % (kind, name, str(exc).splitlines()[0][:120]))
        for t in TRUNCATE_AFTER_RUN:
            try:
                if not _table_exists(c, t):      # 运行期才建的表在没跑过新版本的库里还不存在
                    continue
                c.exec("TRUNCATE %s.%s" % (C.SCHEMA, t))
            except Exception as exc:         # noqa: BLE001
                done.append("清空 %s 失败:%s" % (t, str(exc).splitlines()[0][:100]))
        for table, max_id in DELETE_ABOVE.items():
            try:
                if not _table_exists(c, table):
                    continue
                c.exec("DELETE FROM %s.%s WHERE id > %d" % (C.SCHEMA, table, max_id))
            except Exception as exc:         # noqa: BLE001
                done.append("清 %s 的新增行失败:%s" % (table, str(exc).splitlines()[0][:100]))
        done.append(_shrink(c))
        out["撤掉的残留"] = done or "无"
        left = residue_check(c)
        out["残留"] = "、".join(left) if left else "无"
    finally:
        c.close()
    return out


def _shrink(c: Conn) -> str:
    """把膨胀类场景的表重建回备好时的大小(见 VACUUM_FULL_AFTER_RUN 的说明)。

    放在撤残留**之后**:表级 autovacuum 的参数要先还原,不然重建完它还是不会被自动清理。
    """
    out = []
    for t in tuple(VACUUM_FULL_AFTER_RUN) + tuple(DELETE_ABOVE):
        if not _table_exists(c, t):
            continue
        try:
            before = c.one("SELECT pg_total_relation_size('%s.%s')" % (C.SCHEMA, t)) or 0
            c.exec("VACUUM FULL %s.%s" % (C.SCHEMA, t))
            c.exec("ANALYZE %s.%s" % (C.SCHEMA, t))
            after = c.one("SELECT pg_total_relation_size('%s.%s')" % (C.SCHEMA, t)) or 0
            out.append("%s %d→%d MB" % (t, int(before) // 1048576, int(after) // 1048576))
        except Exception as exc:             # noqa: BLE001
            out.append("%s 重建失败:%s" % (t, str(exc).splitlines()[0][:80]))
    return "已收回膨胀空间(" + "、".join(out) + ")"


def purge() -> Dict[str, object]:
    """连数据与用户一起删。"""
    from . import bootstrap
    out: Dict[str, object] = {"先收尾": run()}
    bootstrap.drop_user()
    out["模式与用户"] = "已删除(%s / %s)" % (C.SCHEMA, C.LAB_USER)
    state.clear()
    for f in (C.STATE_DIR / "fl_app.pw", C.STATE_DIR / "caps.json"):
        if f.exists():
            f.unlink()
    out["本地文件"] = "已删口令与校准文件(时间线留着,供事后对照)"
    leftover = omm_sql("SELECT count(*) FROM pg_roles WHERE rolname = '%s';" % C.LAB_USER)
    out["复核"] = "用户已不在" if leftover.strip().splitlines()[-1].strip() == "0" else "用户还在:" + leftover[:120]
    return out
