"""{{占位符}} 的类型校验与文本替换 —— 两条执行路径的共享渲染器。

**替换方式是文本替换，不是绑定变量。** 这是刻意的选择（开发方案 §2.4）：
客户协议用的是命名文本占位符，占位符可以出现在表名、列名、ORDER BY 等
非取值位置。改用绑定变量的话，这些位置会**静默失效** ——
`ORDER BY $1` 语法通过、执行成功、但按常量排序等于不排序。
一条在客户环境跑得好好的脚本，在本地会安静地给出错误结果，
方向与「本地复现客户行为」这个目标正相反。

代价是注入面完全落在类型校验上，所以本模块的校验一律从严：
不合法即拒绝执行，不做宽容解析、不做默认值兜底。
"""
from __future__ import annotations

import dataclasses
import datetime
import re
from typing import Dict, Iterable, List, Sequence

from .settings import Settings


class ParamError(Exception):
    """参数定义或取值不合法。一律在执行前抛出，绝不带着可疑取值往下走。"""


# 五种类型。parameter_config 里是全大写，API 的 data_type 是驼峰，
# 内部一律用全大写做规范形式。
_API_NAMES = {
    "STRING": "String",
    "INTEGER": "Integer",
    "BOOLEAN": "Boolean",
    "DATETIME": "DateTime",
    "TIMESTAMP": "Timestamp",
}

# 占位符只认 {{标识符}}，不允许内部空格 —— 宽容匹配会让「写错的占位符」
# 变成「渲染后残留的字面量」，进而变成数据库语法错误，排查成本更高。
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")

# \d 在 str 模式下会匹配全角数字等 Unicode 数字，这里必须限定 ASCII
_INTEGER_RE = re.compile(r"^-?[0-9]+$")
_DIGITS_RE = re.compile(r"^[0-9]+$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

_BOOLEAN_LITERALS = ("true", "false")

_STRING_REFUSED_MSG = (
    "String 类型参数当前不可用：脚本占位符的引号责任（作者写 '{{x}}' 还是"
    "中间件补引号）未经客户环境证实，猜测会导致本机与客户环境行为不一致。"
    "取证方式：向客户索取一条带 String 参数的真实 script_config 记录。"
    "确认后把 settings.string_param_policy 切到 as_is。"
)


@dataclasses.dataclass(frozen=True)
class ParamDef:
    """一个参数的声明。对应 parameter_config 的一个元素。

    description 只在响应的 OperationParam 与注册报告里用到，渲染时无关；
    放在这里是为了避免调用方维护一份与 params 平行的描述数组。
    """

    key: str
    type: str
    description: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise ParamError("param key is required")
        # frozen dataclass 里规范化字段的标准写法
        object.__setattr__(self, "type", canonical_type(self.type))

    @property
    def api_type(self) -> str:
        return api_type_name(self.type)


def canonical_type(name: str) -> str:
    """把 "Integer" / "INTEGER" 都归一到 "INTEGER"；五种之外拒绝。"""
    if not isinstance(name, str):
        raise ParamError("param type must be a string, got %r" % (name,))
    upper = name.upper()
    if upper not in _API_NAMES:
        raise ParamError(
            "param type %r: must be one of %s"
            % (name, "/".join(_API_NAMES[k] for k in sorted(_API_NAMES)))
        )
    return upper


def api_type_name(canonical: str) -> str:
    """还原成 API 侧的驼峰枚举。DateTime/Timestamp 不是简单的首字母大写。"""
    return _API_NAMES[canonical_type(canonical)]


def validate_value(canonical: str, raw: str, settings: Settings) -> None:
    """校验单个取值。不合法即抛 ParamError，永不返回「修正后的值」。"""
    ctype = canonical_type(canonical)
    if not isinstance(raw, str):
        raise ParamError(
            "param value must be a string (all values travel as strings), "
            "got %r" % (raw,)
        )

    if ctype == "INTEGER":
        if not _INTEGER_RE.match(raw):
            raise ParamError(
                "Integer param value %r: must be a decimal string, "
                "optionally signed" % (raw,)
            )
    elif ctype == "BOOLEAN":
        if raw not in _BOOLEAN_LITERALS:
            raise ParamError(
                "Boolean param value %r: must be exactly 'true' or 'false'"
                % (raw,)
            )
    elif ctype == "DATETIME":
        if not _DATETIME_RE.match(raw):
            raise ParamError(
                "DateTime param value %r: must match yyyy-MM-dd HH:mm:ss"
                % (raw,)
            )
        try:
            datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ParamError(
                "DateTime param value %r: %s" % (raw, exc)
            ) from exc
    elif ctype == "TIMESTAMP":
        # 秒（10 位）与毫秒（13 位）文档都给了例子，且文本替换下中间件并不
        # 解释单位——只把数字串原样贴进 SQL，秒/毫秒的判断在脚本作者那边。
        # 所以这里不按长度分支，只要求是纯数字。
        if not _DIGITS_RE.match(raw):
            raise ParamError(
                "Timestamp param value %r: must be a string of digits" % (raw,)
            )
    else:  # STRING
        if settings.string_param_policy == "refuse":
            raise ParamError(_STRING_REFUSED_MSG)


def extract_placeholders(sql: str) -> List[str]:
    """按首次出现顺序返回占位符名，去重。"""
    seen = []
    for match in _PLACEHOLDER_RE.finditer(sql):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def render(
    sql: str,
    defs: Sequence[ParamDef],
    values: Dict[str, str],
    settings: Settings,
) -> str:
    """校验后做文本替换，返回渲染好的 SQL。输入均不被修改。

    校验顺序是刻意的：先把「声明与模板对不上」「传参与声明对不上」这类
    结构性问题挑出来，再校验取值，最后才替换。任何一步失败都不替换。
    """
    declared = {d.key: d for d in defs}
    used = extract_placeholders(sql)

    undeclared = [name for name in used if name not in declared]
    if undeclared:
        raise ParamError(
            "SQL 中的占位符未在参数中声明：%s" % ", ".join(sorted(undeclared))
        )

    stray = [name for name in values if name not in declared]
    if stray:
        raise ParamError(
            "传入了脚本未声明的参数：%s（脚本声明的是：%s）"
            % (
                ", ".join(sorted(stray)),
                ", ".join(sorted(declared)) or "无",
            )
        )

    missing = [key for key in declared if key not in values]
    if missing:
        raise ParamError(
            "缺少必填参数：%s（本实现不对未传参数做「删掉条件」之类的猜测，"
            "那会静默改变 SQL 语义）" % ", ".join(sorted(missing))
        )

    for key, definition in declared.items():
        validate_value(definition.type, values[key], settings)

    # 单趟替换：re.sub 只扫描原模板一次，替换进去的文本不会被再次匹配。
    # 若改成逐个 str.replace 循环，先替进去的值里若含 {{other}} 形态，
    # 会被后续轮次当成占位符再替一次 —— 一个安静的注入点。
    def _sub(match: "re.Match") -> str:
        return values[match.group(1)]

    return _PLACEHOLDER_RE.sub(_sub, sql)
