"""白名单核对的「只读账号直连」:用 whitelist.EXPORT_SQL 读中间件库的 grmp.script_config。

只读到底:连上先把会话设成只读事务、语句超时,再查;口令只在内存里,不进日志、不进报错。
报错翻译成运维看得懂、知道找谁的话 —— 原文(驱动的英文)附在后面,不吞。

**GaussDB 默认的 sha256 认证(password_encryption_type=2)标准客户端不认**,psycopg2 也不认:只读账号要在该参数为
0 / 1 时创建、pg_hba 对发版台这台机器用 md5。做不到就用「导出文件」那条路(白名单团队按 EXPORT_SQL 导出 CSV)。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .whitelist import EXPORT_SQL

CONNECT_TIMEOUT_S = 10
STATEMENT_TIMEOUT_MS = 30000
DEFAULT_PORT = 5432


class DbError(Exception):
    pass


def _has(*keys: str) -> Callable[[str], bool]:
    return lambda low: any(k in low for k in keys)


# 驱动报错原文(小写)→ 说法,按顺序取第一条命中的
_REASONS: List[Tuple[Callable[[str], bool], str]] = [
    (_has("authentication method", "sasl", "sha256"),
     "库要求的认证方式标准客户端不支持(GaussDB 默认的 sha256):请 DBA 在 password_encryption_type 为 0 或 1 时建只读账号、"
     "pg_hba 对发版台这台机器用 md5;做不到就改用「导出文件」"),
    (_has("password authentication failed", "invalid username/password", "invalid password"), "账号或口令不对"),
    (_has("no pg_hba.conf entry"), "库的 pg_hba 不允许从发版台这台机器连:请 DBA 加一条(md5)"),
    (lambda low: "relation" in low and "does not exist" in low, "库里没有 grmp.script_config:库名填的是中间件库吗"),
    (lambda low: "database" in low and "does not exist" in low, "库名不对:没有这个库"),
    (_has("permission denied"), "只读账号没有 grmp.script_config 的查询权限:请 DBA 授予 schema grmp 的 USAGE 与这张表的 SELECT"),
    (_has("statement timeout"), "查询超过 %d 秒没返回" % (STATEMENT_TIMEOUT_MS // 1000)),
    (_has("timeout expired", "timed out", "could not connect", "connection refused", "could not translate host name",
          "no route to host"), "连不上"),
]


def explain(raw: str, where: str) -> str:
    low = str(raw).lower()
    for hit, text in _REASONS:
        if hit(low):
            return "%s %s(原文:%s)" % (where, text, _first_line(raw))
    return "%s 查询失败:%s" % (where, _first_line(raw))


def rows(cfg: Dict[str, Any], password: str, connect: Optional[Callable[..., Any]] = None) -> List[tuple]:
    """按资源配置连中间件库,返回 EXPORT_SQL 的行。connect 给测试替换用,默认 psycopg2.connect。"""
    host = str(cfg.get("whitelist.db.host") or "").strip()
    port = str(cfg.get("whitelist.db.port") or DEFAULT_PORT).strip()
    dbname = str(cfg.get("whitelist.db.database") or "").strip()
    user = str(cfg.get("whitelist.db.user") or "").strip()
    missing = [n for n, v in (("地址", host), ("库名", dbname), ("账号", user), ("口令", password)) if not v]
    if missing:
        raise DbError("白名单只读账号没配全:缺%s" % "、".join(missing))
    if not port.isdigit():
        raise DbError("白名单库端口不是数字:%s" % port)
    where = "%s:%s/%s(账号 %s)" % (host, port, dbname, user)
    if connect is None:
        try:
            import psycopg2  # noqa: F401  镜像里有;本机直接跑发版台时可能没装
        except ImportError:
            raise DbError("这里没有数据库驱动 psycopg2:用发版台镜像跑,或改用「导出文件」")
        connect = psycopg2.connect
    try:
        conn = connect(host=host, port=int(port), dbname=dbname, user=user, password=password,
                       connect_timeout=CONNECT_TIMEOUT_S, application_name="gaussdb-agent-release")
    except Exception as exc:                       # noqa: BLE001  驱动的各种连接错误都在这里翻译
        raise DbError(explain(str(exc), where)) from None
    try:
        conn.autocommit = True                     # 每句自成事务:下面的「只读」对查询那句生效
        cur = conn.cursor()
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY", None)
        cur.execute("SET statement_timeout = %d" % STATEMENT_TIMEOUT_MS, None)
        cur.execute(EXPORT_SQL, None)              # 不带参数必须传 None:psycopg2 收到参数容器会做 % 插值
        return [tuple(r) for r in cur.fetchall()]
    except Exception as exc:                       # noqa: BLE001
        raise DbError(explain(str(exc), where)) from None
    finally:
        try:
            conn.close()
        except Exception:                          # noqa: BLE001  关连接失败不影响结果
            pass


def _first_line(text: str) -> str:
    return (str(text).strip().splitlines() or [""])[0][:200]
