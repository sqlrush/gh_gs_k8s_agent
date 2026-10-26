"""WDR 报告里的知识引用:渲染前先核对编号在不在知识库里,再按 common/kb/citestyle 上色、加出处括号。

2026-10-26 回答模板化(R5 / R7):模型在 interp.json 的发现里写 kbRefs(编号 + 那句话),不在文字字段里手写颜色标签 ——
标签由脚本统一拼,不会出现引号写成中文引号而不变色;编号由 gaussdb-kb 的 cite-check 同一套逻辑核对,
库里找不到的(编造)、已废止的,一律不进正文,列到校验区。知识库目录不存在时也不进正文:核对不了的引用不展示。
"""
from __future__ import annotations

import pathlib
import sys
from typing import Iterable, List, Optional, Tuple

from common.kb import citestyle

_KB_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "gaussdb-kb" / "scripts"
OK_STATUS = ("存在", "前缀匹配")


def _cite_checker():
    if str(_KB_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_KB_SCRIPTS))
    import kb_cite  # noqa: E402  (技能目录之间是同级的,装到客户环境也一样)
    return kb_cite


def default_kb_dir() -> Optional[pathlib.Path]:
    try:
        from common.kb import config
        return config.resolve_kb_dir(None)
    except (ImportError, OSError, ValueError):
        return None


def resolve(refs: Iterable, kb_dir: Optional[pathlib.Path]) -> Tuple[List[str], List[str]]:
    """→ (正文里的引用行, 剔除说明)。refs 是 interp.KbRef 列表。"""
    refs = list(refs or [])
    if not refs:
        return [], []
    if kb_dir is None or not pathlib.Path(kb_dir).is_dir():
        return [], ["%s:知识库目录不存在,引用核对不了,未展示" % r.id for r in refs]
    kc = _cite_checker()
    lines: List[str] = []
    dropped: List[str] = []
    for r in refs:
        try:
            cites = kc.check(r.id, pathlib.Path(kb_dir))
        except Exception as exc:  # noqa: BLE001 —— 核对出错不能让整份报告渲染失败:这条不展示,写明原因
            dropped.append("%s:核对出错(%s: %s)" % (r.id, type(exc).__name__, exc))
            continue
        status = cites[0].status if cites else "编号格式认不出"
        if status not in OK_STATUS:
            dropped.append("%s:%s" % (r.id, status))
            continue
        line = citestyle.format_ref(r.id, r.text, r.note)
        if line is None:
            dropped.append("%s:认不出是哪个库" % r.id)
            continue
        lines.append(line)
    return lines, dropped
