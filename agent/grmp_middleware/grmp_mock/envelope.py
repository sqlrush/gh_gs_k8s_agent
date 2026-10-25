"""两套响应信封。

GRMP 最需要注意的结构性特征：两个接口的响应外层结构不一致。
接口一带业务码 {code,msg,result}；接口二无业务码，任务信息平铺在顶层。
调用方因此不能写统一的响应解析器 —— 这个不便是客户协议的真实形态，照搬。

另一条：业务错误走 HTTP 200 + code != "0"，从不用 HTTP 状态码表达。
"""
from __future__ import annotations

import uuid
from typing import Any, Dict

CODE_OK = "0"
CODE_ERROR = "1"

MSG_OK = "success"

TASK_ID_PREFIX = "grmp-"

# 唯一一个由客户实际调用证实的错误文案（传入不存在的 dataIp）。必须逐字一致：
# 客户把这次调用当作「接口鉴权与调用链路正常」的判据。
ERR_INSTANCE_NOT_FOUND = "通过dataIp查询不到对应高斯实例信息"


def ok_list(result: Dict[str, Any]) -> Dict[str, Any]:
    """接口一成功信封。code 是字符串 "0"，不是数字 0。"""
    return {"code": CODE_OK, "msg": MSG_OK, "result": result}


def error(msg: str) -> Dict[str, Any]:
    """接口一错误信封。

    只有 code/msg 两个键：客户样例中未出现 result，本实现也不产出该键——
    产出一个空 result 会让「出错」看起来像「查到 0 条」。
    """
    return {"code": CODE_ERROR, "msg": msg}


def ok_invoke(
    result: Dict[str, Any],
    task_id: str,
    call_type: str = "sync",
    status: str = "finished",
) -> Dict[str, Any]:
    """接口二信封。刻意不含 code/msg —— 参数表与示例一致，确认不是遗漏。"""
    return {
        "result": result,
        "task_id": task_id,
        "call_type": call_type,
        "status": status,
    }


def fail_invoke(
    msg: str,
    task_id: str,
    call_type: str = "sync",
    status: str = "failed",
) -> Dict[str, Any]:
    """接口二的失败响应 —— **这是本实现的约定，不是复刻**。

    文档对这一分支零样例：接口二的成功响应里没有 code/msg，SQL 报错时
    错误信息无处可放。规范说明 §9 把它列为风险最高的一项，理由是若实现
    选择 status:"failed" + result 为空，而调用方只解析 result.data，
    就会把「执行失败」读成「查询结果为空」——「慢 SQL 返回 0 条」被读成
    「当前没有慢 SQL」，结论与事实相反。

    因此这里**不产出 result 键**：
      - 按 §9 建议校验「status==finished 且 result 存在」的调用方，判得准
      - 盲目取 resp["result"]["data"] 的调用方当场 KeyError，吵闹地失败
    错误文本放进 msg（沿用接口一的字段名，是最可能与客户对上的猜测）。
    """
    return {
        "task_id": task_id,
        "call_type": call_type,
        "status": status,
        "msg": msg,
    }


def new_task_id() -> str:
    """"grmp-" + 标准 UUID v4，与客户两个示例的形态一致。"""
    return TASK_ID_PREFIX + str(uuid.uuid4())
