from __future__ import annotations

import os
import re
import pathlib
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

import yaml

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_VALID_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)

_VALID_TYPES = frozenset({"opengauss", "gaussdb"})

# grmp：不直连数据库，改走 GRMP 兼容中间件的两个 HTTP 接口。
# 此时 host/port 指向中间件端点，data_ip 是中间件用来路由到目标实例的键。
_VALID_DRIVERS = frozenset({"gsql", "psycopg2", "grmp"})

# 2026-09 直连驱动由 pg8000 整体换成 psycopg2,**不保留别名**。老配置里写着
# driver: pg8000 的连接会在这里当场报错——泛泛一句 "must be one of" 不足以让
# 客户知道该动哪一行,所以单独给一条带改法的提示。
_RETIRED_DRIVERS = {
    "pg8000": "psycopg2（直连驱动已整体更换，pg8000 不再受支持，也没有保留别名）",
}


MODE_GSQL = "gsql"
MODE_API = "api"
_VALID_MODES = frozenset({MODE_GSQL, MODE_API})


@dataclass(frozen=True)
class Connection:
    """One named database target (immutable)."""

    name: str
    type: str
    host: str
    port: int
    database: str
    user: str
    sslmode: str = ""
    driver: str = "gsql"
    # 仅 driver=grmp 时使用：中间件按它路由到目标高斯实例
    data_ip: str = ""
    # 所属应用分组（新格式 db_connections 下的一级键）。旧格式为空。
    app: str = ""
    # 内联口令。密文时是 base64，AAD 用连接名，与 credentials/ 目录同一把钥匙。
    # 两者都没有时回落到 credentials/<name>.enc —— 老配置不用改就能继续用。
    password: str = ""
    encrypted: bool = False

    @property
    def qualified(self) -> str:
        """带应用前缀的全名，如 app1/conn1。没有分组时就是名字本身。"""
        return "%s/%s" % (self.app, self.name) if self.app else self.name

    def with_sslmode(self, sslmode: str) -> "Connection":
        """Return a new Connection with sslmode replaced (no mutation)."""
        return replace(self, sslmode=sslmode)


@dataclass(frozen=True)
class ApiEndpoint:
    """connection_mode: api 时的中间件端点。"""

    host: str
    port: int
    host_env: str = "GRMP_API_HOST"
    token: str = ""             # 内联令牌（客户配置格式如此）
    token_env: str = "GRMP_AUTH_TOKEN"
    # 2026-09-18 中间件加固:请求头再带 Appkey / Timestamp / Signature。appkey 为空 = 不签名。
    # sign 里是签名细节(credential / key_env / user_id / timestamp / format / encoding / payload),
    # 解释在 common/grmp/signing.py;私钥永远不放这里。
    appkey: str = ""
    appkey_env: str = "GRMP_APPKEY"
    sign: Mapping[str, Any] = field(default_factory=dict)

    def resolve_token(self) -> str:
        """取令牌：环境变量优先于配置文件里的内联值。

        环境变量优先是有意的：内联令牌会随配置文件进版本库、进备份、被随手
        cat；而客户环境里它是**长期有效、无重放保护**的静态凭据。支持内联
        只是因为客户的配置格式就是这样，不代表推荐这么放。
        """
        return os.environ.get(self.token_env, "") or self.token

    def resolve_host(self) -> str:
        """取HOST IP：环境变量优先于配置文件里的内联值。

        环境变量优先是有意的：内联HOST随配置文件进版本库、进备份、被随手
        cat；而客户环境里它是**长期有效、无重放保护**的静态凭据。支持内联
        只是因为客户的配置格式就是这样，不代表推荐这么放。
        """
        return os.environ.get(self.host_env, "") or self.host


class ConfigError(Exception):
    """Raised on malformed config or connection definitions."""


def validate(conn: Connection) -> None:
    """Fail fast on malformed connection definitions (boundary input)."""
    if not conn.name or not _NAME_RE.match(conn.name):
        raise ConfigError(
            f"connection name {conn.name!r}: must start with a lowercase "
            f"letter or digit and contain only [a-z0-9_-]"
        )
    if conn.type not in _VALID_TYPES:
        raise ConfigError(f"type {conn.type!r}: must be opengauss or gaussdb")
    if not conn.host:
        raise ConfigError("host is required")
    if not isinstance(conn.port, int) or conn.port < 1 or conn.port > 65535:
        raise ConfigError(f"port {conn.port}: out of range")
    if not conn.database:
        raise ConfigError("database is required")
    if not conn.user:
        raise ConfigError("user is required")
    if conn.sslmode and conn.sslmode not in _VALID_SSLMODES:
        raise ConfigError(
            f"sslmode {conn.sslmode!r}: must be one of "
            f"disable/allow/prefer/require/verify-ca/verify-full"
        )
    if conn.driver in _RETIRED_DRIVERS:
        raise ConfigError(
            f"connection {conn.name!r}: driver {conn.driver!r} 已停用，"
            f"请改成 {_RETIRED_DRIVERS[conn.driver]}——"
            f"编辑 config.yaml 里这条连接的 driver 字段即可，其余字段不用动。"
        )
    if conn.driver not in _VALID_DRIVERS:
        raise ConfigError(
            f"driver {conn.driver!r}: must be one of "
            f"{'/'.join(sorted(_VALID_DRIVERS))}"
        )
    if conn.driver == "grmp" and not conn.data_ip:
        raise ConfigError(
            f"connection {conn.name!r}: driver grmp requires data_ip "
            f"(中间件按它路由到目标实例，缺了会在第一次调用时才失败)"
        )
    if conn.password and not conn.encrypted:
        # **配置文件里不允许出现明文口令。**
        #
        # config.yaml 会被 cat、会进备份、会被贴进工单和聊天窗口，而它本身
        # 只是「连接元数据」，没人会想到里面藏着生产库口令。加密存放不是
        # 更安全一点点的问题 —— 是把口令挪出这条随手会被复制的路径。
        #
        # 拒绝而不是「警告后继续」：警告在一堆输出里没人看，而配置一旦
        # 带着明文跑起来，它就会一直那样跑下去。
        raise ConfigError(
            f"连接 {conn.name!r} 的 password 是明文（encrypted 不是 true）——"
            f"配置文件里不允许出现明文口令。\n"
            f"把它加密后存进凭据目录：\n"
            f"    python3 -m common.credential_cli set {conn.name}\n"
            f"然后从 config.yaml 里删掉 password/encrypted 两行。\n"
            f"（也可以内联密文：encrypted: true + base64 密文，"
            f"与凭据目录同一把钥匙。）"
        )


# 没设 GSDB_HOME 时的落点。现场沙箱是 skills 安装目录里的 common/;进镜像后这里是只读的,
# 所以 ensure_dir() 建不出目录时必须说清「设 GSDB_HOME」,不能让 PermissionError 直接冒出来。
_DEFAULT_STATE_DIR = pathlib.Path("/workspace/.opencode/skills/common")


def state_dir() -> pathlib.Path:
    base = os.environ.get("GSDB_HOME") or os.environ.get("GDAA_HOME")
    if base:
        return pathlib.Path(base)
    return _DEFAULT_STATE_DIR


def ensure_dir() -> pathlib.Path:
    """Return the state directory, creating it with 0700 if absent."""
    base = state_dir()
    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(base, 0o700)
    except OSError as exc:
        raise ConfigError(
            "状态目录 %s 无法创建或写入(%s)。请设置环境变量 GSDB_HOME 指向一个可写目录"
            "(容器里应指向持久卷,例如 /nas/me/gdaa)。" % (base, exc.strerror or exc)) from exc
    return base


def _config_path() -> pathlib.Path:
    return state_dir() / "config.yaml"


def _read_raw() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"parse {path}: {exc}") from exc


def mode() -> str:
    """connection_mode：gsql（直连）还是 api（走 GRMP 中间件）。

    缺省 gsql —— 旧配置没有这一行，而它们全是直连。默认成 api 会让老配置在
    第一次取数时才失败，且错误表现成「中间件连不上」。
    """
    raw = _read_raw()
    value = str(raw.get("connection_mode", "") or MODE_GSQL).strip().lower()
    if value not in _VALID_MODES:
        raise ConfigError(
            "connection_mode %r 不认识，只能是 %s。"
            % (value, " 或 ".join(sorted(_VALID_MODES))))
    return value


def api_endpoint() -> ApiEndpoint:
    """connection_mode: api 时的中间件端点。"""
    items = _read_raw().get("api_connection") or []
    if isinstance(items, dict):        # 允许写成单个映射而不是列表
        items = [items]
    if not items:
        raise ConfigError(
            "connection_mode 是 api，但配置里没有 api_connection —— "
            "不知道往哪个中间件发请求。")
    if len(items) > 1:
        raise ConfigError(
            "api_connection 配了 %d 个端点，无法判断该用哪一个。"
            "多环境请分别放在不同的 config.yaml 里。" % len(items))
    item = items[0]
    host = str(item.get("host", "") or "").strip()
    if not host:
        raise ConfigError("api_connection.host 不能为空")
    try:
        port = int(item.get("port", 0))
    except (TypeError, ValueError):
        raise ConfigError("api_connection.port %r 不是整数"
                          % item.get("port")) from None
    if port < 1 or port > 65535:
        raise ConfigError("api_connection.port %d 越界" % port)

    sign = item.get("sign") or {}
    if not isinstance(sign, dict):
        raise ConfigError("api_connection.sign 应为映射(credential / user_id / timestamp / format / encoding / payload)")
    return ApiEndpoint(
        host=host,
        port=port,
        host_env=str(item.get("host_env", "") or "GRMP_API_HOST"),
        token=str(item.get("token", "") or ""),
        token_env=str(item.get("token_env", "") or "GRMP_AUTH_TOKEN"),
        appkey=str(item.get("appkey", "") or "").strip(),
        appkey_env=str(item.get("appkey_env", "") or "GRMP_APPKEY"),
        sign={str(k): ("" if v is None else v) for k, v in sign.items()},
    )


@dataclass(frozen=True)
class InstanceAlias:
    """部署时登记的「实例别名 → 实例 IP / 库名」(api 模式)。是部署配置,不是谁的登录会话。"""
    name: str
    ip: str
    database: str
    note: str = ""


def instance_aliases() -> list:
    """config.yaml 的 instance_aliases(容器里由 podctl 按 GRMP_INSTANCE_ALIASES 生成,非容器版直接写在 config.yaml)。没配 = 空。

    缺字段的项跳过并记进 instance_alias_errors() —— 别名只是登录时的便利,一项写错不该让登录整个失败。
    """
    out = []
    for item in _read_raw().get("instance_aliases") or []:
        if not isinstance(item, dict):
            continue
        name, ip, db = (str(item.get(k) or "").strip() for k in ("name", "ip", "database"))
        if name and ip and db:
            out.append(InstanceAlias(name, ip, db, str(item.get("note") or "").strip()))
    return out


def instance_alias_errors() -> list:
    """部署时写错、没能登记进别名清单的项(原文)。"""
    raw = _read_raw()
    errs = [str(e) for e in raw.get("instance_aliases_errors") or []]
    for item in raw.get("instance_aliases") or []:
        if not isinstance(item, dict) or not all(str(item.get(k) or "").strip() for k in ("name", "ip", "database")):
            errs.append("instance_aliases 里的一项缺 name / ip / database:%r" % (item,))
    return errs


def api_endpoint_if_configured() -> Optional[ApiEndpoint]:
    """配置里有 api_connection 才解析;没有返回 None(签名等设置这时全靠环境变量)。"""
    return api_endpoint() if _read_raw().get("api_connection") else None


def _connection_from(item: dict, app: str = "") -> Connection:
    conn = Connection(
        name=item.get("name", ""),
        type=item.get("type", ""),
        host=item.get("host", ""),
        port=item.get("port", 0),
        database=item.get("database", ""),
        user=item.get("user", ""),
        sslmode=item.get("sslmode", "") or "",
        driver=item.get("driver", "gsql") or "gsql",
        data_ip=item.get("data_ip", "") or "",
        app=app,
        password=str(item.get("password", "") or ""),
        encrypted=bool(item.get("encrypted", False)),
    )
    validate(conn)
    return conn


def load() -> list[Connection]:
    """读 config.yaml，返回全部连接定义。文件不存在时返回空表。

    同时认两种格式：

      新（按应用分组）        旧（平铺）
      db_connections:         connections:
        app1:                   - name: og
          - name: conn1           ...
      两者可以并存,结果合并。旧格式没有 app 归属。

    **跨应用重名一律拒绝。** app1/conn1 与 app2/conn1 同名时，`-c conn1`
    该给哪一个？取第一个会连到另一个应用的库上，执行成功、结果无关、不报错
    —— 这正是本项目一路在防的那类失败。要用同名就必须写全名 app/conn。
    """
    raw = _read_raw()
    conns: list[Connection] = []

    for item in raw.get("connections", []) or []:
        conns.append(_connection_from(item))

    groups = raw.get("db_connections") or {}
    if not isinstance(groups, dict):
        raise ConfigError("db_connections 应当是「应用名: 连接列表」的映射")
    for app, items in groups.items():
        app_name = str(app).strip()
        if not app_name:
            raise ConfigError("db_connections 下出现了空的应用名")
        for item in items or []:
            conns.append(_connection_from(item, app=app_name))

    seen: dict[str, list[str]] = {}
    for conn in conns:
        seen.setdefault(conn.name, []).append(conn.app or "(无分组)")
    dupes = {n: apps for n, apps in seen.items() if len(apps) > 1}
    if dupes:
        listed = "；".join("%s 出现在 %s" % (n, "、".join(apps))
                           for n, apps in sorted(dupes.items()))
        raise ConfigError(
            "连接名重复：%s。\n"
            "`-c <名字>` 无法判断该用哪一个,取第一个会连到另一个应用的库上 ——"
            "执行成功、结果无关、不报错。\n"
            "改掉重名,或在配置里用不同的名字。" % listed)
    return conns


def apps() -> dict:
    """按应用分组的连接，供登录时列菜单用。无分组的归在 '' 下。"""
    grouped: dict = {}
    for conn in load():
        grouped.setdefault(conn.app, []).append(conn)
    return grouped


def find(name: str) -> Connection:
    """按名字取连接。支持 `app/conn` 全名，也支持裸名（唯一时）。"""
    wanted_app, _, wanted = name.rpartition("/")
    for conn in load():
        if conn.name != wanted:
            continue
        if wanted_app and conn.app != wanted_app:
            continue
        return conn

    # 会话里可能有一条**配置文件里不存在**的连接：api 模式下的连接是登录时
    # 按用户给的库名现场构造的。不在这里回落的话，下一个 skill 会找不到它。
    from . import session as _session  # 循环依赖：session 依赖本模块的类型
    try:
        live = _session.current()
    except ConfigError:
        live = None     # 多会话分不清是「省略 -c」那条路的事；按名字找不到就照常报「没有这个连接」
    if live is not None and live.name == wanted:
        if not wanted_app or live.app == wanted_app:
            return live

    raise ConfigError(
        "没有名为 %r 的连接。先运行 gaussdb-login 建立会话，"
        "或用 `gaussdb-login --list` 看有哪些可选。" % name
    )


def resolved_name(name: Optional[str] = None) -> str:
    """报告里该记的连接名。

    省略 `-c` 时不能记成空串 —— 而「省略 -c」现在恰恰是推荐用法。一份不写明
    针对哪个库的健康报告，事后没法分辨它是生产还是测试的；两份放在一起更是
    完全一样。

    返回带应用前缀的全名（app/conn）：不同应用下可以有同名连接，只记 `og`
    在多应用环境里仍然是二义的。
    """
    try:
        return resolve(name).qualified
    except ConfigError:
        # 取不到就把调用方给的原样回去 —— 这个函数只负责「报告怎么写」，
        # 连接本身取不到会在真正取数时报错，不该由它抢先抛。
        return name or ""


def resolve(name: Optional[str] = None) -> Connection:
    """13 个 skill 取连接的统一入口。

    给了 `-c` 就用它；没给就用 gaussdb-login 选定的那条。两者都没有时报错
    并告诉用户下一步做什么 —— 不猜一个默认连接：猜错的后果是在错误的库上
    执行诊断，而输出看起来完全正常。
    """
    if name:
        return find(name)
    from . import session as _session
    live = _session.current()
    if live is not None:
        return live
    # 不列沙箱里有哪些会话、有几个:那是别人的登录(2026-09-11 客户反馈的越权)。
    raise ConfigError(_session.NOT_LOGGED_IN + "gsql 模式也可用 `-c <连接名>` 显式指定。")
