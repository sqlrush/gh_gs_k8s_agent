"""建专用用户与模式(check / prepare 的第一步)。

**为什么要专用用户 fl_app:**
  · 所有故障会话都挂在同一个用户名下 —— 出问题时 `pg_stat_activity` 里一眼看得出
    哪些是实验造的,哪些是别人的;
  · purge 时连用户一起删,og5 回到装这套之前的样子;
  · 口令随机生成、只写进 `~/.faultlab/fl_app.pw`(0600),不进仓库、不打印、不写日志。

**为什么要 SYSADMIN:** 实验要读别人的会话(`pg_locks` / `pg_thread_wait_status` 全库视图)、
建复制槽、做 VACUUM FULL、打 WDR 快照。og5 是一次性测试容器,给够权限比在场景里
到处绕权限简单得多;现场环境永远不会跑这套东西(指纹核对会拦住)。
"""
from __future__ import annotations

from typing import Dict

from . import config as C
from .db import Conn, LabError, new_password, omm_sql, save_password


def user_exists() -> bool:
    out = omm_sql("SELECT count(*) FROM pg_roles WHERE rolname = '%s';" % C.LAB_USER)
    return out.strip().splitlines()[-1].strip() == "1" if out.strip() else False


def ensure_user() -> Dict[str, str]:
    """建用户(已存在就只重置口令)。返回做了什么,供调用方打印。

    口令每次 prepare 都重新生成:上一次的口令文件可能已经被删掉 / 改过,
    「用户在但口令对不上」会表现成连不上库,比直接重置更难查。
    """
    pw = new_password()
    existed = user_exists()
    verb = "ALTER" if existed else "CREATE"
    out = omm_sql(
        "%s USER %s WITH SYSADMIN PASSWORD '%s';\n"
        "GRANT ALL ON DATABASE %s TO %s;\n"
        % (verb, C.LAB_USER, pw, _dbname(), C.LAB_USER))
    low = out.lower()
    if "error" in low or "失败" in out:
        raise LabError("建/改用户 %s 失败:%s" % (C.LAB_USER, out.strip()[:300]))
    save_password(pw)
    return {"action": "已重置口令" if existed else "已创建", "user": C.LAB_USER}


def ensure_schema() -> None:
    """模式 fl 归 fl_app。schema.sql 里也有 CREATE SCHEMA,但那条是 fl_app 自己跑的,
    用户还没有建模式的权限时会失败 —— 所以这里先用 omm 建好并授权。"""
    out = omm_sql("CREATE SCHEMA IF NOT EXISTS %s AUTHORIZATION %s;" % (C.SCHEMA, C.LAB_USER))
    if "error" in out.lower():
        raise LabError("建模式 %s 失败:%s" % (C.SCHEMA, out.strip()[:300]))


def drop_user() -> None:
    """purge 用:删模式再删用户。顺序不能反 —— 模式归他所有,先删用户会被拒。"""
    out = omm_sql("DROP SCHEMA IF EXISTS %s CASCADE;\nDROP USER IF EXISTS %s;"
                  % (C.SCHEMA, C.LAB_USER))
    if "error" in out.lower():
        raise LabError("删用户/模式失败:%s" % out.strip()[:300])


def _dbname() -> str:
    from .db import conn_params
    return str(conn_params()["dbname"])


def check() -> Dict[str, object]:
    """环境自检:容器在不在、能不能连、指纹对不对、数据备没备好。只读,不改任何东西。"""
    from .db import conn_params
    params = conn_params()
    report: Dict[str, object] = {"conn": "%s:%s/%s" % (params["host"], params["port"], params["dbname"]),
                                 "container": params["container"]}
    ping = omm_sql("SELECT 'pong';")
    report["容器可达"] = "pong" in ping
    if not report["容器可达"]:
        report["容器报错"] = ping.strip()[:200]
        return report
    report["专用用户"] = "在" if user_exists() else "没有(prepare 会建)"
    if not (C.STATE_DIR / "fl_app.pw").exists():
        report["口令文件"] = "没有(prepare 会生成)"
        return report
    report["口令文件"] = "在"
    c = Conn("check")
    try:
        from .db import assert_expected_instance
        fp = assert_expected_instance(c)
        report["实例"] = "%s … 数据目录 %s" % (fp["version"][:40], fp["data_dir"])
        report["数据"] = _data_state(c)
    finally:
        c.close()
    return report


def _data_state(c: Conn) -> str:
    got = {r[0] for r in c.query(
        "SELECT tablename FROM pg_tables WHERE schemaname = %s", (C.SCHEMA,))}
    rows = None
    if "order_items" in got:
        rows = c.one("SELECT reltuples::bigint FROM pg_class WHERE oid = '%s.order_items'::regclass"
                     % C.SCHEMA)
    return data_state_text(got, rows)


def data_state_text(got, order_items_rows) -> str:
    """数据备没备好(纯函数)。**运行期对象缺了不算没备好:** run 开跑前会自己补建
    (prepare.ensure_runtime_objects),不该为它们把 900 MB 数据重造一遍。"""
    missing = [t.name for t in C.TABLES if t.name not in C.RUNTIME_TABLES and t.name not in got]
    if missing:
        return "缺 %d 张表(%s…),要先跑 prepare" % (len(missing), ", ".join(missing[:4]))
    text = "齐了(order_items 约 %s 行)" % order_items_rows
    later = [t for t in C.RUNTIME_TABLES if t not in got]
    if later:
        text += ";运行期对象还没建(%s)—— run 开跑前会自动补建,第一次约两分钟" % ", ".join(later)
    return text
