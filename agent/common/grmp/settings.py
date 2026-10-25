"""兼容性开关集中定义。

凡是接口文档没有定死、必须由本实现自己选一个的行为，都在这里显式列出，
而不是散落在各模块里用字面量写死。理由：这些选择每一项都可能与客户中间件
不一致，集中放置才能在启动时整份打印出来，让「我们当前假设了什么」可见。

不可变（frozen dataclass）：运行期不允许改动，避免同一进程内前后行为不一致。
"""
from __future__ import annotations

import dataclasses
from typing import Dict, List, Tuple

# 布尔渲染的两套形式。文档 §3.1 给 true/false，§3.2 给 t/f，同一中间件同一张
# 系统表两种渲染 —— §7.2 判断中间件不做归一化，表现随内核版本/驱动而变。
# 因此本实现不写死任何一种，只能由配置选定并在启动时声明。
BOOL_RENDER: Dict[str, Tuple[str, str]] = {
    "t_f": ("t", "f"),
    "true_false": ("true", "false"),
}


class SettingsError(ValueError):
    """配置项非法。配置是边界输入，非法值一律 fail fast。"""


# String 参数的处理策略。
#
#   as_is   纯文本替换，引号责任在脚本作者（默认）
#   refuse  拒绝执行，错误信息里写明取证方式
#
# 默认取 as_is，依据是接口文档「案例 2」把字符串与布尔型合并为同一个案例
# （取值例如 "value"、"zhangsan"、"true"、"false"）。若中间件按类型补引号，
# String 要补而 Boolean 不能补，两类不可能合并 —— 合并说明替换时不按类型
# 分支。加上唯一的脚本样例占位符裸露（> {{threshold_seconds}}）、文档措辞
# 是「sql 参数用占位符 {{}} 标识」，三处证据一致。
#
# 更直接的一条：客户中间件并不拒绝 String 参数。把 refuse 设成默认等于
# 给中间件加了一个客户没有的行为，属于「改良」，与保真原则相悖 ——
# 本地拒绝一条客户能跑的脚本，测出来的结论就不再适用于客户环境。
#
# 但这仍是【推】而非【实】：没有任何一条 String 类型的真实脚本样例。
# 拿到反证时切回 refuse。
STRING_PARAM_POLICIES = frozenset({"as_is", "refuse"})


@dataclasses.dataclass(frozen=True)
class Settings:
    """grmp-mock 的兼容性假设集合。

    bool_style           结果集中布尔列的渲染形式（文档自相矛盾，见 BOOL_RENDER）
    null_text            结果集中 NULL 的渲染形式。文档未明说；§7.3 由 datacl 的
                         表现推断为空字符串。副作用是 NULL 与空串不可区分 ——
                         这是客户中间件的信息损失，需要一并复刻，不能「改良」成 null。
    string_param_policy  String 参数的处理策略，见 STRING_PARAM_POLICIES
    """

    bool_style: str = "t_f"
    null_text: str = ""
    string_param_policy: str = "as_is"

    def __post_init__(self) -> None:
        if self.bool_style not in BOOL_RENDER:
            raise SettingsError(
                "bool_style %r: must be one of %s"
                % (self.bool_style, "/".join(sorted(BOOL_RENDER)))
            )
        if not isinstance(self.null_text, str):
            raise SettingsError("null_text must be a string")
        if self.string_param_policy not in STRING_PARAM_POLICIES:
            raise SettingsError(
                "string_param_policy %r: must be one of %s"
                % (
                    self.string_param_policy,
                    "/".join(sorted(STRING_PARAM_POLICIES)),
                )
            )

    @property
    def bool_pair(self) -> Tuple[str, str]:
        """(真值渲染, 假值渲染)。"""
        return BOOL_RENDER[self.bool_style]

    def assumption_lines(self) -> List[str]:
        """启动横幅用：把每一项未证实的选择连同当前取值列出来。

        这批选择猜错时大多不报错、只出错值（布尔渲染尤甚：判反了，
        诊断结论直接相反）。运行中没有任何征兆，唯一的防线就是
        启动时让人看见本进程按哪一套假设在跑。
        """
        true_text, false_text = self.bool_pair
        return [
            "占位符替换：文本替换（非绑定变量）—— 刻意复现客户行为，"
            "注入面由类型校验兜底",
            "布尔渲染：%s/%s 【矛】文档 §3.1 给 true/false、§3.2 给 t/f，"
            "本进程取前述其一，未经客户确认"
            % (true_text, false_text),
            "NULL 渲染：%r 【推】由 datacl 的表现推断，"
            "副作用是 NULL 与空串不可区分" % self.null_text,
            "String 参数：%s 【推】引号责任在脚本作者，"
            "无任何 String 类型的真实脚本样例可证" % self.string_param_policy,
        ]
