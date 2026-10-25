"""脚本定义：YAML（仓库内单一事实源）→ ScriptRecord（script_config 21 列）。

字段刻意与客户 script_config 的 21 列逐一对齐，包括我们自己用不到的
region / deployment_form / refered_appbusiness 等作用域列 —— 这样注册工具
导出的 INSERT DML 可以直接交给客户走版本发布，不需要再做一次映射。

脚本没有 API 可注册（客户原文：「由于安全原因，目前脚本仅能通过版本 dml
带出」），所以「注册」在两边都是发布期动作：客户走版本 DML，我们走本工具。
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from .placeholder import ParamDef, api_type_name, extract_placeholders
from .statement import is_read_only

# 客户 script_config 的 21 列，顺序即样例 DML 的列顺序
SCRIPT_CONFIG_COLUMNS: Tuple[str, ...] = (
    "id",
    "script_type",
    "script_name",
    "database_type",
    "refered_appbusiness",
    "kernel_version",
    "region",
    "deployment_form",
    "execute_node_type",
    "cluster_deployment_mode",
    "script_content",
    "parameter_config",
    "scene",
    "is_valid",
    "create_user",
    "create_time",
    "last_modify_user",
    "last_modify_time",
    "is_asyn",
    "extend",
    "compliance_mode",
)

# 样例 DML 里只有 extend 带引号，说明它是保留字或大小写敏感
_QUOTED_COLUMNS = frozenset({"extend"})

# 逻辑名是跨环境的匹配键（调用方按 cmd_name 找 id，不能硬编码 id）。
# 格式收紧到 <域>.<名>，全小写：大小写或空格差异会造成静默失配。
_LOGICAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

_ALLOWED_TOP_KEYS = frozenset(
    {
        "name",
        "description",
        "sql",
        "params",
        "script_type",
        "database_type",
        "scene",
        "is_asyn",
        "is_valid",
        "compliance_mode",
        "kernel_version",
        "cluster_deployment_mode",
        "region",
        "deployment_form",
        "execute_node_type",
        "refered_appbusiness",
        "readonly",
    }
)

_ALLOWED_PARAM_KEYS = frozenset({"key", "type", "description"})


class ScriptError(Exception):
    """脚本定义不合法。YAML 是边界输入，结构性问题一律拒绝入库。"""


def quoted_column(name: str) -> str:
    """按客户 DML 的写法给列名加引号。"""
    return '"%s"' % name if name in _QUOTED_COLUMNS else name


@dataclasses.dataclass(frozen=True)
class ScriptRecord:
    """一条 script_config 记录。不可变；改动一律产出新对象。"""

    script_name: str
    script_content: str
    description: str = ""
    params: Tuple[ParamDef, ...] = ()

    id: Optional[str] = None
    script_type: str = "SQL"
    database_type: str = "postgres"
    refered_appbusiness: int = 1
    kernel_version: str = "ALL"
    region: Optional[str] = None
    deployment_form: Optional[str] = None
    execute_node_type: Optional[str] = None
    cluster_deployment_mode: str = "centralization"
    scene: str = "AGENT"
    is_valid: int = 1
    create_user: Optional[str] = None
    create_time: Optional[str] = None
    last_modify_user: Optional[str] = None
    last_modify_time: Optional[str] = None
    is_asyn: int = 0
    extend: Optional[str] = None
    compliance_mode: str = "ALL"

    # 本地概念，**不属于客户 script_config 的 21 列**：执行器据此决定开
    # 只读还是可写会话。客户文档全篇未提执行会话是否只读，所以这一项是
    # 我们自己加的约束，存储上也不占 script_config 的列（见 store.py）。
    readonly: bool = True

    # -- 派生 -------------------------------------------------------------

    @property
    def parameter_config(self) -> str:
        """【实】与客户样例逐字一致：键名、键序、无空格。

        这份 JSON 是双方共享的数据资产，格式偏一点，导出的 DML 客户就用不了。
        autoAcquire 是 JSON 布尔 false，不是字符串。
        """
        items = [
            {
                "key": p.key,
                "value": "",
                "type": p.type,
                "autoAcquire": False,
            }
            for p in self.params
        ]
        return json.dumps(items, ensure_ascii=False, separators=(",", ":"))

    def with_id(self, script_id: str) -> "ScriptRecord":
        return dataclasses.replace(self, id=str(script_id))

    def stamped(self, user: str, when: str) -> "ScriptRecord":
        """填入审计列，取值形态照客户样例。时间由调用方给，本模块不取当前时刻。

        last_modify_user 在创建时就与 create_user 同值填上，last_modify_time
        留 NULL —— 客户既有记录就是这个形态，交付的 DML 要与之同形。
        """
        return dataclasses.replace(
            self,
            create_user=user,
            create_time=when,
            last_modify_user=user,
            last_modify_time=None,
        )

    def as_row(self) -> Dict[str, Any]:
        """21 列的取值字典，供落库与导出 DML 使用。"""
        return {
            "id": self.id,
            "script_type": self.script_type,
            "script_name": self.script_name,
            "database_type": self.database_type,
            "refered_appbusiness": self.refered_appbusiness,
            "kernel_version": self.kernel_version,
            "region": self.region,
            "deployment_form": self.deployment_form,
            "execute_node_type": self.execute_node_type,
            "cluster_deployment_mode": self.cluster_deployment_mode,
            "script_content": self.script_content,
            "parameter_config": self.parameter_config,
            "scene": self.scene,
            "is_valid": self.is_valid,
            "create_user": self.create_user,
            "create_time": self.create_time,
            "last_modify_user": self.last_modify_user,
            "last_modify_time": self.last_modify_time,
            "is_asyn": self.is_asyn,
            "extend": self.extend,
            "compliance_mode": self.compliance_mode,
        }

    def to_api_detail(self) -> Dict[str, Any]:
        """CommonOmOperationDetail —— 接口一响应里的一条命令详情。

        description 取 script_name：客户响应里这两个值完全相同，且
        script_config 根本没有 description 列。照此复刻，不自作主张
        把 YAML 里给人看的描述填进去 —— 那会让本地调用方依赖一个
        客户环境不存在的信息。
        """
        return {
            "id": self.id,
            "cmd": self.script_content,
            "cmd_name": self.script_name,
            "description": self.script_name,
            "cmd_type": self.script_type,
            # OperationParam（四字段），与请求里的 OperationValue 不是同一结构
            "param": [
                {
                    "param_name": p.key,
                    "data_type": api_type_name(p.type),
                    # 本实现所有声明的参数都必填：未传时报错而不猜测语义
                    "required": True,
                    "description": p.description,
                }
                for p in self.params
            ],
        }


def _require_mapping(raw: Any, path: pathlib.Path) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScriptError("%s: 顶层必须是映射（key: value）" % path)
    return raw


def _parse_params(raw_params: Any, path: pathlib.Path) -> Tuple[ParamDef, ...]:
    if raw_params is None:
        return ()
    if not isinstance(raw_params, list):
        raise ScriptError("%s: params 必须是列表" % path)

    defs: List[ParamDef] = []
    seen = set()
    for item in raw_params:
        if not isinstance(item, dict):
            raise ScriptError("%s: params 的每一项必须是映射" % path)
        unknown = set(item) - _ALLOWED_PARAM_KEYS
        if unknown:
            raise ScriptError(
                "%s: 参数定义含未知键 %s（合法键：%s）"
                % (path, ", ".join(sorted(unknown)), ", ".join(sorted(_ALLOWED_PARAM_KEYS)))
            )
        key = item.get("key")
        if not key:
            raise ScriptError("%s: 参数缺少 key" % path)
        if key in seen:
            raise ScriptError("%s: 参数 %s 重复声明" % (path, key))
        seen.add(key)
        # 类型非法时由 placeholder.canonical_type 抛 ParamError，不在此吞掉
        defs.append(
            ParamDef(
                key=key,
                type=item.get("type", ""),
                description=str(item.get("description", "")),
            )
        )
    return tuple(defs)


def load_script(path: pathlib.Path) -> ScriptRecord:
    """读一个脚本 YAML，校验后返回 ScriptRecord。"""
    path = pathlib.Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ScriptError("%s: YAML 解析失败：%s" % (path, exc)) from exc

    data = _require_mapping(raw, path)

    unknown = set(data) - _ALLOWED_TOP_KEYS
    if unknown:
        raise ScriptError(
            "%s: 含未知顶层键 %s（合法键：%s）"
            % (path, ", ".join(sorted(unknown)), ", ".join(sorted(_ALLOWED_TOP_KEYS)))
        )

    name = data.get("name")
    if not name:
        raise ScriptError("%s: 缺少 name（逻辑脚本名）" % path)
    if not _LOGICAL_NAME_RE.match(str(name)):
        raise ScriptError(
            "%s: name %r 格式不合法，须为 <域>.<名> 全小写，"
            "如 slowsql.slow_sql" % (path, name)
        )

    sql = data.get("sql")
    if not sql or not str(sql).strip():
        raise ScriptError("%s: 缺少 sql（命令正文）" % path)

    defs = _parse_params(data.get("params"), path)

    # 占位符与声明必须双向一致，两个方向的失配都会在运行期变成难查的问题：
    # 少声明 → 渲染后残留 {{}} 导致语法错误；多声明 → 调用方被迫传一个
    # 对 SQL 毫无影响的参数。
    used = set(extract_placeholders(str(sql)))
    declared = {d.key for d in defs}
    missing = used - declared
    if missing:
        raise ScriptError(
            "%s: SQL 中的占位符未在 params 声明：%s"
            % (path, ", ".join(sorted(missing)))
        )
    unused = declared - used
    if unused:
        raise ScriptError(
            "%s: params 声明了但 SQL 未使用：%s"
            % (path, ", ".join(sorted(unused)))
        )

    # 写操作必须显式声明。只标注不拦截的话，一条写脚本能注册进去却永远
    # 执行不了——既不报错也不能用，问题要到执行时才暴露。
    if "readonly" in data and not isinstance(data["readonly"], bool):
        raise ScriptError(
            "%s: readonly 必须是布尔值（true/false），收到 %r。"
            "写成字符串 \"false\" 在真值判断下是 True，与作者意图相反。"
            % (path, data["readonly"])
        )
    # 拦截的目的是**逼作者做决定**，不是禁止某个选择：
    #   未声明 + 静态判不出只读  → 拒绝入库
    #   显式 readonly: false     → 开可写会话（走单独审批）
    #   显式 readonly: true      → 作者担保只读，仍开只读会话；
    #                              整条是占位符（SQL 直通）这类静态判不了的
    #                              脚本，靠它把会话钉在只读上
    if "readonly" not in data and not is_read_only(str(sql)):
        raise ScriptError(
            "%s: 语句不是只读查询，但没有声明 readonly。会话模式必须显式指定：\n"
            "  readonly: false  开可写会话（写操作需单独审批）\n"
            "  readonly: true   仍用只读会话（作者担保，写操作会被数据库挡回）\n"
            "不声明就默认只读的话，一条写脚本能注册进去却永远执行不了 ——"
            "既不报错也不能用，问题要到执行时才暴露。" % path
        )

    overrides = {
        key: data[key]
        for key in data
        if key in _ALLOWED_TOP_KEYS
        and key not in ("name", "description", "sql", "params")
    }

    return ScriptRecord(
        script_name=str(name),
        script_content=str(sql),
        description=str(data.get("description", "")),
        params=defs,
        **overrides
    )
