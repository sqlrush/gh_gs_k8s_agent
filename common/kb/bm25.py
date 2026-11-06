"""词法打分:BM25,信号 token 加权,归一到 0..1。文件模式与 SQLite 索引共用这一份算式。

两边算出来的分必须是同一个刻度:query.py 的绝对 / 相对门槛(lexical_min、relative_floor)只认一套数。
归一的分母是「每个查询 token 都饱和命中」的理想分,所以只保证序与相对大小,不是概率。
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple, TypeVar

K1 = 1.2
B = 0.75
SIGNAL_WEIGHT = 3.0          # 信号 token(复发标志 / keywords)的词频权重

Key = TypeVar("Key")


def term_freqs(tokens: Sequence[str], signals: Sequence[str] = ()) -> Tuple[Dict[str, float], float]:
    """一段文本的 (词频, 长度)。信号 token 每次出现按 SIGNAL_WEIGHT 计。"""
    tf: Dict[str, float] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0.0) + 1.0
    for t in signals:
        tf[t] = tf.get(t, 0.0) + SIGNAL_WEIGHT
    return tf, float(len(tokens) + len(signals))


def idf(df: int, n: int) -> float:
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5)) if df else 0.0


def scores(query_tokens: Sequence[str], df: Mapping[str, int], n: int, avg_len: float,
           docs: Iterable[Tuple[Key, float, Mapping[str, float]]]) -> List[Tuple[Key, float]]:
    """[(key, 0..1 分)],只含命中的。docs 只需要给出含查询 token 的那些(key, 长度, 词频)。"""
    q = [t for t in dict.fromkeys(query_tokens) if t]
    weights = {t: idf(int(df.get(t, 0)), n) for t in q}
    ideal = sum(w * (K1 + 1.0) for w in weights.values() if w > 0)
    if ideal <= 0:
        return []
    avg = avg_len or 1.0
    out: List[Tuple[Key, float]] = []
    for key, length, tf in docs:
        s = 0.0
        for t, w in weights.items():
            f = tf.get(t)
            if not f or w <= 0:
                continue
            norm = K1 * (1.0 - B + B * length / avg)
            s += w * f * (K1 + 1.0) / (f + norm)
        if s > 0:
            out.append((key, min(1.0, s / ideal)))
    return out
