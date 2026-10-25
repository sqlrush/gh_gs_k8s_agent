"""中间件路径：走 GRMP 的两个 HTTP 接口访问数据库。

调用链与 agent 在客户环境必须做的事完全一致：
    查命令清单 → 按 cmd_name 匹配逻辑名 → 取 id → invoke

**不硬编码 id**：脚本 ID 是环境相关数据。硬编码的失败方式极其隐蔽 ——
换环境后 ID 依然存在，指向另一条脚本，执行成功、结果无关、不报错。

响应解析上有两条不能省的校验（规范说明 §9）：
  接口一判成功看 code，接口二判成功看 status 且 result 必须存在。
  只判 HTTP 200 会把业务错误当成成功；只取 result.data 会把执行失败
  读成「查询结果为空」。
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Mapping, Optional

from . import paths, proxy, userid
from .errors import QueryError
from .hints import with_hint
from .params import to_param_value

_ERROR_BODY_MAX = 4000   # 4xx/5xx 响应体最多读这么多字节:够装一条 JDBC 报错,不够装一个页面

DEFAULT_TIMEOUT = 120
LIST_PAGE_SIZE = 1000  # 协议上限；脚本数不多，一次拉完


class GrmpError(QueryError):
    """中间件返回的业务错误，或响应结构不符合协议。

    继承 QueryError，使 skill 只需 catch 一个类型 ——
    换一种访问方式时，skill 的降级逻辑不用动。
    """


def _error_body(exc: urllib.error.HTTPError) -> str:
    """把 4xx/5xx 响应体里中间件说的原因带出来;是 JSON 就取 msg 一类字段,不是就给原文。"""
    try:
        raw = exc.read(_ERROR_BODY_MAX).decode("utf-8", errors="replace").strip()
    except Exception:  # 响应体读不了:至少还有状态码
        return ""
    return _error_detail(raw)


def _error_detail(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    detail = raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            for key in ("msg", "message", "error", "detail"):
                if parsed.get(key):
                    detail = str(parsed[key])
                    break
    except ValueError:
        pass
    return "；中间件返回：%s" % detail[:500]


class GrmpClient:
    """一个 GRMP 端点 + 一个 dataIp。命令清单在进程内缓存。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        data_ip: str,
        timeout: int = DEFAULT_TIMEOUT,
        signer: Optional[Any] = None,
        user_id: Optional[Any] = None,
        proxy_socket: str = "",
        path_prefix: Optional[str] = None,
    ):
        # 经凭据代理时本进程里没有令牌:令牌、签名、工号都由代理加(common/grmp/proxy.py)
        if not token and not proxy_socket:
            raise GrmpError("缺少 auth 令牌")
        # 接口路径前缀部署时配置(common/grmp/paths.py);不给就读环境变量 GRMP_PATH_PREFIX,都没有就报清楚
        try:
            prefix = paths.normalize(path_prefix) if path_prefix is not None else paths.require()
        except paths.PrefixError as exc:
            raise GrmpError(str(exc)) from None
        if not prefix:
            raise GrmpError("中间件接口路径前缀为空(%s)" % paths.PREFIX_ENV)
        self.list_path = paths.list_path(prefix)
        self.invoke_path = paths.invoke_path(prefix)
        self._proxy = proxy_socket
        self.base_url = base_url.rstrip("/")
        self.data_ip = data_ip
        self._token = token
        self._timeout = timeout
        self._signer = signer          # common.grmp.signing.RequestSigner;None = 中间件未开签名校验
        self._user_id = user_id        # common.grmp.userid.UserIdSettings;None = 中间件不收工号
        self._ids: Optional[Dict[str, str]] = None

    @property
    def signer(self) -> Optional[Any]:
        return self._signer

    # -- 传输 -------------------------------------------------------------

    def _headers(self, path: str) -> Dict[str, str]:
        if self._proxy:
            return {"Content-Type": "application/json"}      # 凭据由代理加,这里一样都不带
        # 头名是 auth，不是 Authorization，且无 Bearer 前缀
        headers = {"auth": self._token, "Content-Type": "application/json"}
        if self._signer is not None:
            # 2026-09-18 起中间件还校验 Appkey / Timestamp / Signature(认应用、防重放、防改路径),每次请求现算
            headers.update(self._signer.headers(path))
        # 2026-09-20 客户确认中间件要显式收调用人工号;头名由客户给,没配就不加
        return userid.apply_headers(self._user_id, headers)

    def with_user_id(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """按配置把工号加进报文顶层,返回新字典;没配就原样返回。"""
        return userid.apply_payload(self._user_id, payload)

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._proxy:
            return self._post_via_proxy(path, payload)
        payload = self.with_user_id(payload)
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(path),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # HTTPError 是 URLError 的子类,必须先接:4xx/5xx 的响应体里才有中间件说的失败原因。
            # 现场 SQL 在备机上执行失败被包成 HTTP 400,只 catch URLError 时用户看到的是
            # 「HTTP Error 400: 」后面一片空白,排查被带到参数名方向去了两天。
            raise GrmpError(with_hint(
                "请求 %s 失败：HTTP %s %s%s（中间件 %s）"
                % (path, exc.code, exc.reason, _error_body(exc), self.base_url)
            )) from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            # 连不上、被对端断开、超时、DNS 解析失败——都在这里变成带地址的一句话。
            # 地址必须写出来:中间件换地址后老会话打旧地址(09-14 现场),排查第一眼要看的就是它。
            raise GrmpError("请求 %s 失败：%s（中间件 %s）" % (path, exc, self.base_url)) from exc
        return self._parse(body)

    @staticmethod
    def _parse(body: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise GrmpError("响应不是合法 JSON：%s" % body[:200]) from exc
        if not isinstance(parsed, dict):
            raise GrmpError("响应不是 JSON 对象：%s" % body[:200])
        return parsed

    def _post_via_proxy(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """工号也不在这里加:代理按本 Pod / 槽位绑定的人强制写入,模型改不了。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            status, reason, raw = proxy.post(self._proxy, path, data, self._headers(path), self._timeout)
        except (OSError, http.client.HTTPException) as exc:
            raise GrmpError("请求 %s 失败：凭据代理不可用（%s：%s）。容器刚启动时稍等几秒重试；一直这样请联系管理员"
                            % (path, self._proxy, exc)) from exc
        body = raw.decode("utf-8", errors="replace")
        if status >= 400:
            raise GrmpError(with_hint("请求 %s 失败：HTTP %s %s%s（经凭据代理）"
                                      % (path, status, reason, _error_detail(body[:_ERROR_BODY_MAX]))))
        return self._parse(body)

    # -- 接口一 -----------------------------------------------------------

    def list_operations(self) -> List[Dict[str, Any]]:
        """拉全量命令清单。翻页按原始分页走，不做客户端过滤后的计数。"""
        details: List[Dict[str, Any]] = []
        page = 1
        while True:
            body = self._post(
                self.list_path,
                {"dataIp": self.data_ip, "offset": page, "limit": LIST_PAGE_SIZE},
            )
            # 接口一判成功只能看 code —— 业务错误也是 HTTP 200
            if body.get("code") != "0":
                raise GrmpError(
                    "查询命令清单失败：%s" % body.get("msg", body)
                )
            result = body.get("result") or {}
            details.extend(result.get("list") or [])
            if not result.get("hasNextPage"):
                break
            page += 1
        return details

    def _id_map(self) -> Dict[str, str]:
        if self._ids is None:
            self._ids = {
                d.get("cmd_name"): d.get("id")
                for d in self.list_operations()
                if d.get("cmd_name")
            }
        return self._ids

    def resolve_id(self, script_name: str) -> str:
        ids = self._id_map()
        if script_name not in ids:
            raise GrmpError(
                "中间件未注册脚本 %s。已注册的有：%s"
                % (script_name, ", ".join(sorted(ids)) or "（无）")
            )
        return ids[script_name]

    def invalidate_cache(self) -> None:
        """脚本集随版本变化，需要时手动失效。"""
        self._ids = None

    # -- 接口二 -----------------------------------------------------------

    def invoke(
        self, script_name: str, values: Mapping[str, Any] = None
    ) -> List[Dict[str, str]]:
        payload: Dict[str, Any] = {
            "dataIp": self.data_ip,
            "id": self.resolve_id(script_name),
        }
        if values:
            payload["param"] = [
                {"param_name": key, "param_value": to_param_value(val)}
                for key, val in values.items()
            ]

        body = self._post(self.invoke_path, payload)

        # 两者缺一即按失败处理。只取 result.data 会把执行失败读成
        # 「查询结果为空」——「慢 SQL 返回 0 条」被读成「当前没有慢 SQL」。
        status = body.get("status")
        if status != "finished" or "result" not in body:
            raise GrmpError(with_hint(
                "执行 %s 失败（status=%r，task_id=%s）：%s"
                % (script_name, status, body.get("task_id"), body.get("msg", ""))
            ))

        result = body["result"] or {}
        result_type = str(result.get("type", "")).lower()
        if result_type != "array":
            raise GrmpError(
                "脚本 %s 未返回结果集（type=%r）。诊断脚本应当是查询语句。"
                % (script_name, result.get("type"))
            )
        return list(result.get("data") or [])


class GrmpRunner:
    """driver 为 grmp 时使用。对外形状与 DirectRunner 一致。

    **不提供持久会话**：接口二每次调用都是独立连接。实测 —— 第一次调用
    set work_mem='63MB'，第二次调用 show 读回的是默认值 16MB。
    """

    provides_session = False
    # 只执行预注册脚本。与 provides_session 是**两根独立的轴**（见 access.py）：
    # 这条说「跑不了临时给的 SQL」，那条说「跨语句状态留不住」。skill 拿它决定
    # 降级时该怎么措辞 —— 白名单部署里没有可切换的直连选项，劝人换驱动是噪音。
    whitelist_only = True

    def __init__(self, client: GrmpClient):
        self._client = client

    @property
    def client(self) -> GrmpClient:
        """底层 HTTP 客户端。

        gaussdb-login 要用它做连通性验证 —— 调接口一（取命令清单）就能一次
        验掉端点可达、令牌有效、dataIp 被受理三件事，而且不依赖任何一条具体
        脚本是否注册过。留个公开访问器，好过让调用方去摸 _client。
        """
        return self._client

    @property
    def data_ip(self) -> str:
        return self._client.data_ip

    def run(
        self, script_name: str, values: Mapping[str, Any] = None
    ) -> List[Dict[str, str]]:
        return self._client.invoke(script_name, values)
