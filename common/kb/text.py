"""分词 —— 词法检索的确定性半边。

为什么在 Python 侧切词、不用引擎自带的分词器:常见的默认分词都不做中文分词(整段中文是一个 token),
对标点 / 冒号的处理也各不相同。这里切好「中文二元组 + 标识符整词 + 标识符碎片」,
SQLite 索引与文件模式都直接存、直接比这些 token,行为一致且可单测。

标识符(对象名 / GUC / 等待事件)是检索的主入口:整词保留,同时吐出按 . : _ 拆开的
碎片,让「cosp_asyn_task_dtl」「dtl」「walwritelock」都能命中。
"""
from __future__ import annotations

import re
import unicodedata
from typing import List

# 只在查询侧剔除:这些字组成的二元组只会拉低排序。索引侧全量保留。
_QUERY_STOP_CHARS = frozenset("的了在是和与及或等有为把被对于从到就也都很个么这那")
# 查询侧再剔一层泛词:提问里的「怎么处理」「什么问题」跟任何工单都沾边,留着只会让无关工单上榜。
# 「有没有…的案例」「类似」这类问法同理:知识库里的每一条都是案例。
_QUERY_STOP_WORDS = frozenset({
    "怎么", "如何", "为什", "什么", "处理", "问题", "办法", "方案", "建议", "情况", "出现",
    "是否", "可以", "需要", "应该", "怎样", "一下", "请问", "帮我", "看看", "分析", "排查",
    "案例", "类似", "相似", "以前", "之前", "遇到",
})
# 含停用字、却是数据库领域词的二元组,查询侧不能丢。2026-10-24 测试环境实测:「锁等待」切出来的「锁等」「等待」
# 都含「等」,一个词都不剩;「对象」「主从」「从库」「对账」同理。索引侧本来全量保留,这里放行就能命中。
_QUERY_KEEP_WORDS = frozenset({
    "等待", "对象", "对账", "主从", "从库", "从机", "从节", "等于", "大于", "小于", "有效", "在线",
    "为空", "行为", "到期",
})

_CJK = "一-鿿㐀-䶿"
# 顺序有讲究:标识符(可带 . 或 : 连接段)> 数字词(3s / 16gb)> 中文段。
_TOKEN_RE = re.compile(
    rf"(?P<ident>[a-z_][a-z0-9_]*(?:[.:][a-z0-9_]+)*)"
    rf"|(?P<num>[0-9]+[a-z]*)"
    rf"|(?P<cjk>[{_CJK}]+)"
)
_IDENT_SPLIT_RE = re.compile(r"[.:_]+")


def normalize(text: str) -> str:
    """NFKC(全角→半角)、小写、空白折叠。"""
    folded = unicodedata.normalize("NFKC", text).lower()
    return " ".join(folded.split())


def _cjk_tokens(run: str) -> List[str]:
    if len(run) == 1:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def _ident_tokens(ident: str) -> List[str]:
    parts = [p for p in _IDENT_SPLIT_RE.split(ident) if p]
    if len(parts) <= 1:
        return [ident]
    # 整词在前,碎片在后;中间层(去掉 schema 前缀的对象名)也要有。
    out = [ident]
    if "." in ident or ":" in ident:
        out += [seg for seg in re.split(r"[.:]", ident) if seg and seg != ident]
    out += [p for p in parts if p not in out]
    return out


def tokenize(text: str) -> List[str]:
    """索引侧分词:顺序保留,允许重复(位置信息靠它)。"""
    tokens: List[str] = []
    for m in _TOKEN_RE.finditer(normalize(text)):
        kind = m.lastgroup
        if kind == "cjk":
            tokens += _cjk_tokens(m.group())
        elif kind == "ident":
            tokens += _ident_tokens(m.group())
        else:
            tokens.append(m.group())
    return tokens


def query_tokens(text: str) -> List[str]:
    """查询侧分词:剔掉含停用字的中文词,去重保序。"""
    seen = set()
    out: List[str] = []
    for tok in tokenize(text):
        is_cjk = tok[0] >= "㐀"
        if is_cjk and tok not in _QUERY_KEEP_WORDS and (
                tok in _QUERY_STOP_WORDS or any(ch in _QUERY_STOP_CHARS for ch in tok)):
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out
