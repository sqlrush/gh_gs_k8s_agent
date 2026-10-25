"""路由与请求处理。

处理逻辑全部收在 App.handle 里，不碰套接字 —— 这样每条协议行为都能
用普通断言逐条验证，HTTP 那层只剩一个薄壳（见 http_server.py）。

一条贯穿的规则：**业务错误走 HTTP 200 + code!="0"**，HTTP 状态码只用于
传输层问题（路由不存在、方法不对）。这是客户的错误模型，照搬。
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, Mapping, Optional, Tuple

from . import envelope, executor, pagination
from .executor import (
    DEFAULT_MAX_RESULT_ROWS,
    DEFAULT_STATEMENT_TIMEOUT_SECONDS,
    ExecError,
)
from .instances import InstanceMap
from .signature import SignaturePolicy
from common.grmp import paths as grmp_paths
from common.grmp.placeholder import ParamError
from common.grmp.settings import Settings
from .store import ScriptStore

# 接口路径前缀:与客户端同一个变量 GRMP_PATH_PREFIX(common/grmp/paths.py);没设就用样例前缀。
# 下面两个常量是**默认前缀**下的路径,给单测与启动横幅用;App 实际用的是自己的 list_path / invoke_path。
DEFAULT_PATH_PREFIX = grmp_paths.EXAMPLE_PREFIX
LIST_PATH = grmp_paths.list_path(DEFAULT_PATH_PREFIX)
INVOKE_PATH = grmp_paths.invoke_path(DEFAULT_PATH_PREFIX)

DEFAULT_OFFSET = 1
DEFAULT_LIMIT = 10
MAX_LIMIT = 1000

# 接口一请求体允许的字段。客户端的 --cmd-type 是本地过滤，
# 服务端没有对应字段 —— 传了要报错，默默忽略会让调用方以为服务端筛过了。
_LIST_KEYS = frozenset({"dataIp", "offset", "limit"})

# 接口二请求体允许的字段
# 刻意不含 readonly：会话模式只能来自已注册脚本的声明。
# 允许请求指定的话，任何调用方都能给自己开写权限。
_INVOKE_KEYS = frozenset({"dataIp", "id", "param"})

# param 数组元素只能是 OperationValue。接口文档 3.2 的示例误填成了
# OperationParam（把参数的「定义」当成「取值」发出去），那个形状要拒绝：
# 接受它就得猜「值到底在 description 还是别处」，猜错就是静默取错值。
_OPERATION_VALUE_KEYS = frozenset({"param_name", "param_value"})


def _default_open_db(name: str, read_only: bool = True):
    """默认连接方式：复用仓库既有的凭据与驱动兜底，不新增凭据存储。"""
    from common.db import Database

    return Database.connect(name, read_only=read_only)


class App:
    """协议处理器。无状态，可被多个请求复用。"""

    def __init__(
        self,
        store: ScriptStore,
        instances: InstanceMap,
        token: str,
        settings: Settings,
        open_db=None,
        max_result_rows: int = DEFAULT_MAX_RESULT_ROWS,
        statement_timeout: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
        standby: bool = False,
        signature: Optional[SignaturePolicy] = None,
        path_prefix: Optional[str] = None,
    ):
        prefix = grmp_paths.normalize(path_prefix) if path_prefix is not None else grmp_paths.from_env()
        self.path_prefix = prefix or DEFAULT_PATH_PREFIX
        self.list_path = grmp_paths.list_path(self.path_prefix)
        self.invoke_path = grmp_paths.invoke_path(self.path_prefix)
        # 接口文档的示例 URL 多了一段 /dataip/{dataip}，客户实际调用没有。
        # 一并接受这种写法：照着文档写的客户端若拿到 404，排查方向会被带偏。
        self._dataip_re = re.compile(r"^%s/dataip/[^/]+(?P<rest>/.*)$" % re.escape(self.path_prefix))
        self._store = store
        self._instances = instances
        self._token = token
        self._settings = settings
        # 2026-09-18 中间件加固:给了策略就在 auth 之外再校验 Appkey / Timestamp / Signature
        self._signature = signature
        self._open_db = open_db or _default_open_db
        self._max_result_rows = max_result_rows
        self._statement_timeout = statement_timeout
        # 模拟备机:读 statement_history(unlogged 表)的脚本按真实 GRMP 的形态回 HTTP 400,
        # health.overview 的 in_recovery 置 t。复现现场 2026-09-01 的 400,让 skill 的降级路径可测。
        self._standby = standby

    # -- 入口 -------------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Tuple[int, Dict[str, Any]]:
        route = self._normalise_path(path)
        if route not in (self.list_path, self.invoke_path):
            return 404, {"error": "no such route: %s" % path}
        if method.upper() != "POST":
            return 405, {"error": "method not allowed: %s" % method}

        error = self._check_auth(headers)
        if error is not None:
            return 200, error
        if self._signature is not None:
            reason = self._signature.check(route, headers)
            if reason is not None:
                return 200, envelope.error(
                    "签名校验失败：%s（本实现约定：客户环境此场景的响应形态未知）" % reason)

        if route == self.invoke_path:
            return self._invoke_operation(body)

        return self._list_operations(body)

    def _normalise_path(self, path: str) -> str:
        """去掉查询串，并把 /dataip/{ip}/ 这一段折叠掉（body 里的 dataIp 才作数）。"""
        path = path.split("?", 1)[0].rstrip("/") or "/"
        match = self._dataip_re.match(path)
        if match:
            return self.path_prefix + match.group("rest")
        return path

    # -- 鉴权 -------------------------------------------------------------

    def _check_auth(self, headers: Mapping[str, str]) -> Optional[Dict[str, Any]]:
        """auth 头校验。

        鉴权失败的响应形态文档未给（【缺】），本实现按业务错误模型处理，
        并在 msg 里声明这是本实现约定，避免被当成已证实行为。
        """
        supplied = None
        for name, value in headers.items():
            if name.lower() == "auth":
                supplied = value
                break
        if not supplied:
            return envelope.error(
                "缺少 auth 请求头（本实现约定：客户环境此场景的响应形态未知）"
            )
        if supplied != self._token:
            return envelope.error(
                "auth 令牌不匹配（本实现约定：客户环境此场景的响应形态未知）"
            )
        return None

    # -- 接口一 -----------------------------------------------------------

    def _list_operations(self, body: bytes) -> Tuple[int, Dict[str, Any]]:
        parsed, error = _parse_json_object(body)
        if error is not None:
            return 200, error

        unknown = set(parsed) - _LIST_KEYS
        if unknown:
            return 200, envelope.error(
                "请求体含不支持的字段：%s。注意本接口不支持按命令类型服务端"
                "过滤，客户端的 --cmd-type 是本地过滤。"
                % ", ".join(sorted(unknown))
            )

        data_ip = parsed.get("dataIp")
        if not data_ip:
            return 200, envelope.error("缺少必填参数 dataIp")

        if self._instances.resolve(data_ip) is None:
            # 【实】必须逐字一致：客户把这个响应当作调用链路正常的判据
            return 200, envelope.error(envelope.ERR_INSTANCE_NOT_FOUND)

        page_num, error = _positive_int(parsed, "offset", DEFAULT_OFFSET, None)
        if error is not None:
            return 200, error
        page_size, error = _positive_int(parsed, "limit", DEFAULT_LIMIT, MAX_LIMIT)
        if error is not None:
            return 200, error

        details = [rec.to_api_detail() for rec in self._store.list_all()]
        return 200, envelope.ok_list(pagination.paginate(details, page_num, page_size))


    # -- 接口二 -----------------------------------------------------------

    def _invoke_operation(self, body: bytes) -> Tuple[int, Dict[str, Any]]:
        """执行一条已注册脚本。

        失败一律走 envelope.fail_invoke（无 result 键）—— 见那里的说明。
        每次调用都先取 task_id，失败响应也带着它，否则日志无从关联。
        """
        task_id = envelope.new_task_id()
        parsed_holder: Dict[str, Any] = {}

        def fail(msg: str) -> Tuple[int, Dict[str, Any]]:
            # 失败在 stderr 单独记一行 —— HTTP 层一律 200，访问日志里看不出来
            print("[GRMP] FAIL %s :: %s" % (_log_name(parsed_holder), msg[:160]),
                  file=sys.stderr, flush=True)
            return 200, envelope.fail_invoke(msg, task_id)

        parsed, error = _parse_json_object(body)
        if error is not None:
            return fail(error["msg"])
        parsed_holder.update(parsed if isinstance(parsed, dict) else {})

        unknown = set(parsed) - _INVOKE_KEYS
        if unknown:
            return fail("请求体含不支持的字段：%s" % ", ".join(sorted(unknown)))

        data_ip = parsed.get("dataIp")
        if not data_ip:
            return fail("缺少必填参数 dataIp")
        conn_name = self._instances.resolve(data_ip)
        if conn_name is None:
            return fail(envelope.ERR_INSTANCE_NOT_FOUND)

        script_id = parsed.get("id")
        if not script_id:
            return fail("缺少必填参数 id")
        record = self._store.find_by_id(str(script_id))
        if record is None:
            return fail(
                "脚本 id=%s 不存在。脚本 ID 是环境相关数据，调用方应先查"
                "命令清单再取 id，不要硬编码。" % script_id
            )

        if record.script_type.upper() != "SQL":
            return fail(
                "不支持 %s 类型命令：文档对 PYTHON 只有声明没有规范"
                "（运行时、执行位置、沙箱边界全空白），拿到规范前不启用。"
                % record.script_type
            )
        if record.is_asyn:
            return fail(
                "不支持异步执行：文档没有任务状态查询与结果拉取接口，"
                "异步链路无法依据本文档实现。本实现不做「异步脚本偷偷同步"
                "执行」的兜底——那会让调用方误以为拿到了异步语义。"
            )

        values, error_msg = _parse_operation_values(parsed.get("param"))
        if error_msg is not None:
            return fail(error_msg)

        if self._standby and "statement_history" in record.script_content.lower():
            print("[GRMP] FAIL %s :: standby(unlogged)" % record.script_name, file=sys.stderr, flush=True)
            return 400, envelope.fail_invoke(STANDBY_ERROR, task_id)

        try:
            result = executor.execute(
                record,
                values,
                conn_name,
                self._settings,
                self._open_db,
                max_rows=self._max_result_rows,
                timeout=self._statement_timeout,
            )
        except (ParamError, ExecError) as exc:
            return fail(str(exc))
        except Exception as exc:  # 数据库错误等
            return fail(str(exc))

        if self._standby:
            result = mark_standby(result)
        print("[GRMP] OK   %s :: %d 行" % (record.script_name, len(result.get("data") or [])),
              file=sys.stderr, flush=True)
        return 200, envelope.ok_invoke(result, task_id)


# 现场 provider.core.log 里 JDBC 报错的原文形态(实例 id / 地址已换成占位)
STANDBY_ERROR = ("SQL execution failed via JDBC on instance standby-mock: "
                 "ERROR: Temporary or unlogged table cannot be accessed on the standby.")


def mark_standby(result: Dict[str, Any]) -> Dict[str, Any]:
    """结果集里有 in_recovery 列就置成 t(不改原对象):模拟 pg_is_in_recovery() 在备机上的返回。"""
    data = result.get("data") or []
    return {**result, "data": [
        {**row, "in_recovery": "t"} if isinstance(row, dict) and "in_recovery" in row else row
        for row in data]}


def _log_name(parsed: Mapping[str, Any]) -> str:
    """失败日志里的标识。脚本名此时未必解析出来，退回用 id。"""
    sid = parsed.get("id")
    return "id=%s" % sid if sid else "(未取到 id)"

def _parse_operation_values(raw: Any) -> Tuple[Dict[str, str], Optional[str]]:
    """把 param 数组解析成 {名: 值}。元素必须是 OperationValue。"""
    if raw is None:
        return {}, None
    if not isinstance(raw, list):
        return {}, "param 必须是数组"

    values: Dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            return {}, "param 的每一项必须是对象"
        keys = set(item)
        if keys != _OPERATION_VALUE_KEYS:
            return {}, (
                "param 元素的字段必须恰好是 param_name + param_value，"
                "收到 %s。注意接口文档 3.2 的示例填成了参数「定义」"
                "（data_type/required/description），那是错的。"
                % ", ".join(sorted(keys))
            )
        name = item["param_name"]
        value = item["param_value"]
        if not isinstance(name, str) or not name:
            return {}, "param_name 必须是非空字符串"
        if not isinstance(value, str):
            return {}, (
                "param_value 必须是字符串（所有类型的取值都以字符串承载，"
                "整数也写成 \"10\"），参数 %s 收到 %r" % (name, value)
            )
        if name in values:
            return {}, "参数 %s 重复传入" % name
        values[name] = value
    return values, None


def _parse_json_object(
    body: bytes,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """请求体是边界输入：解析失败与类型不对都要明确报错。"""
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        return {}, envelope.error("请求体不是合法 JSON：%s" % exc)
    if not isinstance(parsed, dict):
        return {}, envelope.error("请求体必须是 JSON 对象")
    return parsed, None


def _positive_int(
    parsed: Mapping[str, Any],
    key: str,
    default: int,
    maximum: Optional[int],
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """取一个 1-based 的正整数参数。越界一律报错，**不夹取**。

    夹取会静默改变调用方要的那一页，之后翻页逻辑全对不上而毫无征兆。
    bool 要单独挡：Python 里 True 是 int 的子类，不挡就会被当成 1。
    """
    if key not in parsed:
        return default, None
    value = parsed[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return 0, envelope.error(
            "%s 必须是整数，收到 %r" % (key, value)
        )
    if value < 1:
        return 0, envelope.error("%s 最小值为 1，收到 %d" % (key, value))
    if maximum is not None and value > maximum:
        return 0, envelope.error(
            "%s 最大值为 %d，收到 %d" % (key, maximum, value)
        )
    return value, None
