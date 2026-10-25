"""把调用人工号送到 GRMP(客户 2026-09-20 确认要)。

中间件按 `dataIp` 路由、按 `auth` 令牌认调用方,报文里本来没有「谁在调用」这一维
(脚本白名单表 21 列全是实例属性,`create_user` 只是建表人)。客户确认要显式收工号。

**字段名与位置还没给**,所以两处都做成开关,跟签名旋钮一个路子——对不上不用改代码:

    GRMP_USER_ID_HEADER   请求头名,例如 X-User-Id / staffNo;空 = 不放头里
    GRMP_USER_ID_PARAM    报文顶层键名,例如 userId;空 = 不放报文里

两个都不配 = 跟加这个特性之前完全一样,中间件还没开这项校验时照常可用。

取值只有一个来源:`GSDB_USER_ID`(平台按 Pod 注入的工号),与报告落款
`common.audit.actor_id()` 同一个变量。不另设来源——两处取到不同的值,
比不传更糟:报告说是张三干的,中间件的审计里记的是李四。

**配了字段名却取不到工号 → ConfigError,不发空值。** 悄悄发空,中间件看到的是
「有人调用但没有身份」,而我方退出码 0、报告照出,正是最难查的那类失效。
默认签名原文是「路径 + 时间戳」,不含头与报文,所以加这两样不影响签名;
若客户日后改成对报文签名,`GRMP_USER_ID_PARAM` 要在签名之前加进去,届时再调顺序。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from ..audit import ENV_USER_ID
from ..config import ConfigError

ENV_HEADER = "GRMP_USER_ID_HEADER"
ENV_PARAM = "GRMP_USER_ID_PARAM"

__all__ = ["ENV_USER_ID", "ENV_HEADER", "ENV_PARAM", "UserIdSettings", "settings_from"]


@dataclass(frozen=True)
class UserIdSettings:
    """工号往哪儿放、放什么。两个位置至少配一个,否则 settings_from 返回 None。"""

    user_id: str
    header: str = ""
    param: str = ""


def settings_from(env: Mapping[str, str]) -> Optional[UserIdSettings]:
    """没配任何字段名 → None(中间件没要求)。配了但没有工号 → ConfigError。"""
    header = (env.get(ENV_HEADER) or "").strip()
    param = (env.get(ENV_PARAM) or "").strip()
    if not header and not param:
        return None
    user_id = (env.get(ENV_USER_ID) or "").strip()
    if not user_id:
        where = " / ".join(n for n, v in ((ENV_HEADER, header), (ENV_PARAM, param)) if v)
        raise ConfigError(
            "配了 %s 要把工号发给中间件，但 %s 是空的。"
            "容器部署下这个变量由平台按 Pod 注入；单机运行请先设置它，"
            "或清空 %s 不发工号——绝不发空工号。" % (where, ENV_USER_ID, where))
    return UserIdSettings(user_id=user_id, header=header, param=param)


def apply_headers(settings: Optional[UserIdSettings], headers: Dict[str, str]) -> Dict[str, str]:
    """返回新的头字典;没配头名时原样返回。头名按客户给的原文发,不做大小写归一。"""
    if settings is None or not settings.header:
        return headers
    return {**headers, settings.header: settings.user_id}


def apply_payload(settings: Optional[UserIdSettings], payload: Dict[str, Any]) -> Dict[str, Any]:
    """返回新的报文;没配键名时原样返回。不就地改调用方构造的报文。"""
    if settings is None or not settings.param:
        return payload
    return {**payload, settings.param: settings.user_id}
