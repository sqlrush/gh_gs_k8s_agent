"""skill 侧的统一入口：按连接的 driver 选路。

    runner = access.for_conn("og")
    rows = runner.run("slowsql.slow_sql", {"threshold_ms": 200, "limit": 20})

skill 不感知自己走的是中间件还是直连，两条路径返回相同形状
（全字符串化的行字典）。这一点就是本层的全部价值：skill 代码在本地
与客户环境完全相同，不需要为两边各留一套。

    driver: psycopg2 / gsql  →  直连路径
    driver: grmp           →  中间件路径
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from typing import Any, Optional

from .config import ConfigError, Connection, api_endpoint_if_configured, find, resolve
from .grmp import paths as grmp_paths, proxy, signing, userid
from .grmp.client import GrmpClient, GrmpRunner
from .grmp.errors import QueryError
from .grmp.registry import Registry
from .grmp.runner import DirectRunner
from .grmp.settings import Settings

# 取数失败的**唯一**对外异常类型。skill 的降级逻辑只 catch 它 ——
# 新增一种访问方式时，新 Runner 抛 QueryError 即可，skill 一行不改。
# 详见 common/grmp/errors.py 里关于「哪些错误不归一」的说明。
__all__ = ["for_conn", "runner_for", "session_for", "session_for_conn",
           "connection_for", "connection_for_conn",
           "require_unregistered_sql", "require_unregistered_sql_for_conn",
           "may_provide_session", "whitelist_only",
           "QueryError", "AccessError", "SessionUnavailable",
           "UnregisteredSqlUnsupported"]

TOKEN_ENV = "GRMP_AUTH_TOKEN"
HOST_ENV = "GRMP_API_HOST"

DIRECT_DRIVERS = frozenset({"gsql", "psycopg2"})

# ---------------------------------------------------------------------------
# 驱动的能力位
#
# skill 表达「我需要什么能力」，由这里回答给不给得了 —— 所以 skill 代码里
# 不该出现任何 driver 名字。explain 曾经写死 `driver == "grmp"`：将来客户
# 换一种白名单型中间件，那句判断会静默放行，错报到中间件那边去，
# 排查方向整个被带偏。要加新访问方式，只在这两行里加。
#
# 两个能力是**两根独立的轴**，今天恰好都只有 grmp 缺，别合并：
#   - 白名单：只执行预先注册的脚本，跑不了临时给的 SQL
#   - 无状态：每次调用独立连接，跨语句会话不留存
# gsql 就是「能跑任意 SQL、但没有持久会话」的现成反例。
_WHITELIST_ONLY = frozenset({"grmp"})
_STATELESS = frozenset({"grmp"})

# 语句超时:直连路径能设(SET statement_timeout),中间件路径设不了 ——
# 协议没有这个旋钮,注册脚本里也塞不进 SET(那是第二条语句)。
_NO_STATEMENT_TIMEOUT = frozenset({"grmp"})

# skill 不显式指定 --timeout 时用的秒数。与迁移前各 skill 的 argparse
# 默认值一致 —— 迁移只该换取数通道,不该顺手改超时行为。
DEFAULT_SKILL_TIMEOUT_SECONDS = 30


class AccessError(Exception):
    """选路或凭据缺失。一律在构造时抛出，不拖到第一次请求。"""


class SessionUnavailable(AccessError):
    """当前访问路径不提供跨语句的持久会话。

    hypopg 虚拟索引验证这类流程（建虚拟索引 → 在同一会话里 EXPLAIN）
    离开会话就会**静默给出错误结论**：虚拟索引没了，EXPLAIN 看到原计划，
    于是得出「加这个索引没用」。所以宁可在入口处报错，也不能让它跑下去。
    """


class UnregisteredSqlUnsupported(AccessError):
    """当前访问路径只执行预先注册的脚本，跑不了临时给的任意 SQL。

    与 SessionUnavailable 是**两回事**：那个说的是「跨语句状态留不住」，
    这个说的是「这条语句根本递不进去」。gsql 能跑任意 SQL 却没有持久会话，
    正好说明两者不能互相代替 —— 用会话守卫来挡任意 SQL，会把一批
    本来能用的连接一并拒掉。
    """


def _open_database(conn: Connection, read_only: bool = True):
    """打开原始连接。抽成函数是为了测试能替换掉它。"""
    from .db import Database

    return Database.connect(conn.name, read_only=read_only)


def connection_for_conn(conn: Connection, read_only: bool = True):
    """索取一条**能执行任意 SQL** 的原始连接，给不了就报错。

    与 session_for_conn() 的差别只在会话那一档：这里**不要求**跨语句状态
    留存。explain 就属于这一档 —— 单条 EXPLAIN，DML 的 ANALYZE 也是在一次
    调用里 BEGIN/ROLLBACK 包住的。用会话守卫会把 gsql（每条语句起独立
    子进程）一并拒掉，那是借来的约束。

    存在的理由是「怎么建连」不该写在 skill 里：explain 原先自己调
    Database.connect，等于把访问方式的知识漏到了 skill 层。
    """
    require_unregistered_sql_for_conn(conn)
    return _open_database(conn, read_only=read_only)


def connection_for(name: str, read_only: bool = True):
    """按连接名索取能执行任意 SQL 的原始连接。"""
    return connection_for_conn(resolve(name), read_only=read_only)


def session_for_conn(conn: Connection, read_only: bool = True):
    """索取一条**带持久会话**的原始连接，拿不到就报错。

    与 runner_for() 是两条不同的口子：runner 面向「执行一条已注册脚本」，
    这里面向「一串必须落在同一会话里的语句」。后者是白名单模型撑不住的
    场景，所以要显式索取、显式失败。

    read_only 默认 True，与 runner 一侧一致。放开它只有一个已知理由：
    EXPLAIN ANALYZE 一条 DML —— 语句要真执行（外面包回滚事务），只读会话
    会在事务里就把它挡回。默认不放开，调用方必须显式要求。
    """
    driver = conn.driver or "gsql"
    if driver in _STATELESS:
        raise SessionUnavailable(
            "连接 %s 的 driver 是 grmp：中间件的执行接口每次调用都是独立连接，"
            "不提供跨语句的持久会话。\n"
            "依赖会话的流程（hypopg 虚拟索引验证等）在客户的白名单模型下"
            "本来就跑不了 —— 需要为这类诊断单独保留一条直连通道，"
            "或在客户环境不提供该能力。" % conn.name
        )
    db = _open_database(conn, read_only=read_only)
    if not getattr(db, "provides_session", False):
        db.close()
        raise SessionUnavailable(
            "连接 %s 的 driver 是 %s：该后端每条语句起独立子进程，"
            "不提供跨语句的持久会话。\n"
            "本机调试请改用 driver: psycopg2 的连接。" % (conn.name, driver)
        )
    return db


def session_for(name: str, read_only: bool = True):
    """按连接名索取带持久会话的原始连接。"""
    return session_for_conn(resolve(name), read_only=read_only)


def may_provide_session(name: str) -> bool:
    """这条访问路径**有没有可能**给出跨语句的持久会话。不抛异常，不建连接。

    与 session_for() 是问与取的关系：拿 SessionUnavailable 当控制流，意味着
    每次都要先尝试、再从异常里恢复 —— 而对中间件来说那次尝试是注定失败的，
    白跑一趟不说，降级路径还得靠 except 分支拼出来。skill 该在动手之前就
    知道这条路通不通。

    返回 False 是**确定**给不了（白名单型中间件每次调用独立连接）。
    返回 True 只表示驱动这一层没排除 —— gsql 每条语句起独立子进程，
    要等 session_for() 建连后看后端的 provides_session 才能定。
    """
    return (resolve(name).driver or "gsql") not in _STATELESS


def whitelist_only(name: str) -> bool:
    """这条访问路径是不是只执行预注册脚本的白名单型通道。

    给的是**措辞**的依据，不是能力的依据：同一句「拿不到 hypopg 背书」，
    本机调试时下一步是「换 driver: psycopg2 重跑」，而客户的白名单部署里
    压根没有直连通道 —— 那句话在那边不是建议，是噪音，还会把人往
    「绕过白名单」的方向引。
    """
    return (resolve(name).driver or "gsql") in _WHITELIST_ONLY


def require_unregistered_sql_for_conn(conn: Connection) -> None:
    """索取「能执行未注册 SQL」这个能力，给不了就当场报错。

    只判这一条边界，**不顺带要求持久会话** —— 单条 EXPLAIN 不需要会话，
    拿会话守卫来挡会把 gsql 这类能用的连接一并拒掉，属于借来的约束。

    报错只说清「这条路给不了」；至于本 skill 为什么不绕过去（比如注册一条
    直通脚本），那是 skill 自己的策略，由调用方补在后面。
    """
    driver = conn.driver or "gsql"
    if driver in _WHITELIST_ONLY:
        raise UnregisteredSqlUnsupported(
            "连接 %s 的 driver 是 %s：该访问路径只执行预先注册的脚本"
            "（白名单模型），而本次要执行的是临时给定的任意 SQL，无法预注册。"
            % (conn.name, driver)
        )


def require_unregistered_sql(name: str) -> None:
    """按连接名索取「能执行未注册 SQL」这个能力。"""
    require_unregistered_sql_for_conn(resolve(name))


def _base_url(conn: Connection) -> str:
    scheme = "https" if conn.sslmode in ("require", "verify-ca", "verify-full") else "http"
    return "%s://%s:%d" % (scheme, conn.host, conn.port)


def runner_for(
    conn: Connection,
    registry: Optional[Registry] = None,
    settings: Optional[Settings] = None,
    timeout: Optional[int] = None,
) -> Any:
    """按 Connection 造一个 runner。

    timeout 是**语句超时秒数**;None 表示调用方没提要求,用默认值。

    中间件路径设不了超时(协议没这个旋钮)。这时如果调用方**显式**要了一个
    超时值,就在 stderr 说一声 —— 收下参数然后当没看见,才是真正危险的:
    用户以为查询 5 秒会被掐断,实际它能在客户生产库上一直跑。
    只在显式指定时提示,默认值不吭声,否则每次调用都刷一行,提示很快
    就没人看了。
    """
    driver = conn.driver or "gsql"
    if driver in DIRECT_DRIVERS:
        return DirectRunner(
            conn_name=conn.name,
            registry=registry or Registry(),
            settings=settings or Settings(),
            statement_timeout=(DEFAULT_SKILL_TIMEOUT_SECONDS
                               if timeout is None else timeout),
        )
    if driver == "grmp":
        if timeout is not None and driver in _NO_STATEMENT_TIMEOUT:
            print(
                "注意：连接 %s 的 driver 是 %s，该访问路径无法设置语句超时"
                "（协议没有这个参数），--timeout %s 不会生效。"
                "长查询能否被掐断取决于中间件和数据库自身的配置。"
                % (conn.name, driver, timeout),
                file=sys.stderr, flush=True,
            )
        if not conn.data_ip:
            raise AccessError(
                "连接 %s 使用 grmp 驱动，但未配置 data_ip。" % conn.name
            )
        # 接口路径前缀是部署配置(GRMP_PATH_PREFIX):没配就在这里报清楚,不拖到请求时表现成中间件 404
        try:
            grmp_paths.require()
        except grmp_paths.PrefixError as exc:
            raise AccessError("连接 %s 使用 grmp 驱动，%s" % (conn.name, exc)) from exc
        sock = proxy.socket_path()
        if sock:
            # 凭据代理(容器部署):本进程里没有令牌与私钥,中间件地址、签名、工号都由代理定,这里只管报文
            return GrmpRunner(GrmpClient(base_url="http://credproxy", token="", data_ip=conn.data_ip,
                                         proxy_socket=sock))
        token = os.environ.get(TOKEN_ENV)

        # 如果设置了HOST_ENV 变量，则以变量的值为主，未设置则以配置文件里的host为主
        if os.environ.get(HOST_ENV):
            conn = replace(conn, host=os.environ.get(HOST_ENV))        
 
        if not token:
            # fail fast：令牌只从环境变量读，不落盘、不进代码。
            # 拖到第一次请求才失败，错误会表现成「中间件返回鉴权失败」，
            # 排查方向会被带到中间件那边去。
            raise AccessError(
                "连接 %s 使用 grmp 驱动，但环境变量 %s 未设置。"
                % (conn.name, TOKEN_ENV)
            )
        if not conn.host:
            # 排查方向会被带到中间件那边去。
            raise AccessError(
                "连接 %s 使用 grmp 驱动，但环境变量 %s 未设置。"
                % (conn.name, HOST_ENV)
            )
        # 2026-09-18 中间件加固:配了 Appkey 就带签名头;没配就只带 auth。缺私钥 / 参数不对在这里
        # 就报清楚——拖到第一次请求会表现成中间件鉴权失败,排查方向又被带到中间件那边去。
        try:
            sign_settings = signing.settings_from(os.environ, api_endpoint_if_configured())
        except ConfigError as exc:
            raise AccessError("连接 %s 使用 grmp 驱动，签名配置有误：%s" % (conn.name, exc)) from exc
        # 2026-09-20 客户要中间件显式收工号。配了字段名却没有工号要在这里就报——
        # 拖到第一次请求会发出一个空工号,中间件那边有调用无身份,我方还是退出码 0。
        try:
            user_id_settings = userid.settings_from(os.environ)
        except ConfigError as exc:
            raise AccessError("连接 %s 使用 grmp 驱动，工号配置有误：%s" % (conn.name, exc)) from exc
        return GrmpRunner(
            GrmpClient(
                base_url=_base_url(conn),
                token=token,
                data_ip=conn.data_ip,
                signer=signing.RequestSigner(sign_settings) if sign_settings else None,
                user_id=user_id_settings,
            )
        )
    raise AccessError("连接 %s 的 driver %r 不受支持" % (conn.name, driver))


def for_conn(
    name: str,
    registry: Optional[Registry] = None,
    settings: Optional[Settings] = None,
    timeout: Optional[int] = None,
) -> Any:
    """按连接名造 runner。timeout=None 表示不提要求,用默认值。"""
    return runner_for(resolve(name), registry=registry, settings=settings,
                      timeout=timeout)
