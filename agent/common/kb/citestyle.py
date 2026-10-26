"""知识引用在回答里的样子:按编号认出是哪个库、上什么颜色,拼成「上色的句子(引用自 xxx 库 编号)」。

2026-10-26 回答模板化(R5):出自知识库的句子上色,句后括号注明出处。颜色按「是谁说的」分:贵行自己的规范、案例、手册、
基线、画像、Wiki 等蓝色;产品官方说法(官方文档集,以及客户导入的产品文档 PD-NNNN)青色。
写法的原文在 common/answer_templates/cite.md(注入各技能与 AGENTS.md);这里是同一套规矩的代码版 ——
WDR 报告由脚本渲染(skills/gaussdb-wdr/scripts/finalreport.py),回答格式检查(tools/answer_check.py)也按它认。
颜色标签不带引号、# 前不留空格:2026-10-24 测试环境里写成中文引号就整段不变色。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

CUSTOMER_COLOR = "#2f6fdb"
OFFICIAL_COLOR = "#0f7c86"

# (编号的正则, 库名, 颜色)。顺序有讲究:官方文档集 PD-<代号>-NNNNN 要排在客户导入的 PD-NNNN 前面。
_LIBRARIES: Tuple[Tuple[re.Pattern, str, str], ...] = tuple((re.compile(p), lib, color) for p, lib, color in (
    (r"PD-[A-Z][A-Z0-9]{1,9}-\d{5}", "GaussDB 官方文档库", OFFICIAL_COLOR),
    (r"PD-\d{4}", "产品文档库", OFFICIAL_COLOR),          # 客户导入的产品文档也是「产品官方说法」
    (r"GS-[A-Z]{2,4}-\d{3}", "贵行规范库", CUSTOMER_COLOR),
    (r"S[1-4]-\d{8}-[A-Za-z0-9_]+-.*", "本行案例库", CUSTOMER_COLOR),
    (r"RB-[A-Z]{2,4}-\d{3}", "处置手册库", CUSTOMER_COLOR),
    (r"KD-\d{4}", "已知缺陷库", CUSTOMER_COLOR),
    (r"BL-[A-Z0-9_]+-[A-Z0-9_]+", "贵行基线库", CUSTOMER_COLOR),
    (r"EP-[A-Z0-9_]+", "环境画像库", CUSTOMER_COLOR),
    (r"WK-\d{4}", "Wiki 经验库", CUSTOMER_COLOR),
))
LIBRARY_NAMES = tuple(lib for _p, lib, _c in _LIBRARIES)


def library_for(item_id: str) -> Optional[Tuple[str, str]]:
    """编号 → (库名, 颜色);认不出的返回 None(调用方不要给它上色,也不要编一个库名)。"""
    tok = (item_id or "").strip()
    for pat, lib, color in _LIBRARIES:
        if pat.fullmatch(tok):
            return lib, color
    return None


def format_ref(item_id: str, text: str, note: str = "") -> Optional[str]:
    """`<font color=#…>句子</font>(引用自 库名 编号[,标签])`;编号认不出或句子为空返回 None。

    句子里的换行压成空格:一个标签不能跨段落(跨了就不变色)。标签照抄,不加引号。
    """
    found = library_for(item_id)
    body = " ".join((text or "").split())
    if found is None or not body:
        return None
    lib, color = found
    tail = ",%s" % note.strip() if note and note.strip() else ""
    gap = " " if lib[0].isascii() else ""              # 「引用自 GaussDB 官方文档库」「引用自贵行规范库」
    return "<font color=%s>%s</font>(引用自%s%s %s%s)" % (color, body, gap, lib, item_id.strip(), tail)
