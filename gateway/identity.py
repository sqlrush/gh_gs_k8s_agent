"""从请求里取出「这是谁」—— 网关的安全边界。

客户的 SSO / WAF 认完人之后,把工号放进一个请求头交给本网关。这意味着:
**谁能把这个头发进来,谁就能冒充任何人。** 所以本模块的重点不是解析,是「凭什么信它」。

两道门,可以叠加,但**至少要有一道**:
  GATEWAY_TRUST_SECRET    共享密钥,上游必须在 X-Agent-Trust 头里带上(常数时间比较)
  GATEWAY_TRUST_CIDRS     允许的来源网段,逗号分隔(上游 SSO / Ingress 的地址)

**一道都没配时网关拒绝启动**(config.py 里 fail closed)。不这么做的话,一个配错的
部署就是个「任填工号」的接口,而且从外面看它工作得好好的 —— 正是最危险的形态。

工号还要过一遍格式校验:它会被拼进 k8s 资源名(runtime-<工号>)与 NAS 路径
(users/<工号>),不校验就是注入。只放行 RFC 1123 标签的子集。
"""
from __future__ import annotations

import hmac
import ipaddress
import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

# k8s 资源名用 RFC 1123 标签:小写字母数字与 -,首尾必须是字母数字。
# 资源名是 `runtime-<工号>`,已占 8 字符,标签上限 63,所以工号留 54。
_USER_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,52}[a-z0-9])?$")

HEADER_TRUST = "x-agent-trust"


class IdentityError(Exception):
    """取不到可信身份。调用方一律回 401/403,**不要**把原因细节回给客户端。"""


@dataclass(frozen=True)
class Identity:
    """本模块只回答「这是谁、他带了哪些角色」。

    **刻意不在这里判断谁是知识库管理员。** 那取决于配置项 KB_ADMIN_ROLES,判定在
    server.py。这里曾有一个 is_kb_admin 属性把 "kb-admin" 写死,两条路并存的后果是:
    谁顺手用了属性,那个配置项就对他那条路静默失效。
    """
    user_id: str
    roles: tuple = ()


def normalize_user_id(raw: Optional[str]) -> str:
    """去空白 + 转小写。大小写不统一会变成两个 Pod、两份 NAS 目录、两套历史。"""
    return (raw or "").strip().lower()


def valid_user_id(raw: Optional[str]) -> bool:
    return bool(_USER_ID_RE.match(normalize_user_id(raw)))


def _cidr_allows(cidrs: Sequence[str], peer: Optional[str]) -> bool:
    if not cidrs:
        return False
    try:
        addr = ipaddress.ip_address((peer or "").strip())
    except ValueError:
        return False
    for c in cidrs:
        try:
            if addr in ipaddress.ip_network(c.strip(), strict=False):
                return True
        except ValueError:
            continue          # 配错的网段跳过,不让它把整条链路判成可信
    return False


def trusted(headers: Mapping[str, str], peer: Optional[str],
            secret: str, cidrs: Sequence[str]) -> bool:
    """上游可信吗。两道门任一通过即可;两道都没配 → False(由 config 保证不会走到这)。"""
    if secret:
        got = ""
        for k, v in headers.items():
            if k.lower() == HEADER_TRUST:
                got = v or ""
                break
        # 常数时间比较:普通 == 会因为提前返回而泄露前缀,可以被逐字节猜出来
        if hmac.compare_digest(got, secret):
            return True
    return _cidr_allows(cidrs, peer)


def extract(headers: Mapping[str, str], peer: Optional[str], *, header_name: str,
            roles_header: str, secret: str, cidrs: Sequence[str]) -> Identity:
    """可信来源 + 合法工号 → Identity;否则抛 IdentityError。"""
    if not trusted(headers, peer, secret, cidrs):
        raise IdentityError("来源不可信")
    lower = {k.lower(): v for k, v in headers.items()}
    uid = normalize_user_id(lower.get(header_name.lower()))
    if not uid:
        raise IdentityError("请求头 %s 里没有工号" % header_name)
    if not valid_user_id(uid):
        # 不把原值回显给客户端 —— 它会被原样写进日志与错误页
        raise IdentityError("工号格式不合法")
    roles = tuple(r.strip().lower() for r in (lower.get(roles_header.lower()) or "").split(",") if r.strip())
    return Identity(user_id=uid, roles=roles)
