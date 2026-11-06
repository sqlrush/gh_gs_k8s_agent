"""库级临时溢出 → 归到语句上的那一句说明。WDR 与健康检查的时段体检共用,措辞只有一份。

库级计数器(pg_stat_database.temp_bytes)不知道是谁落的盘;语句级的 sort/hash spill
(snap_summary_statement)知道,但只覆盖有语句统计的执行。所以说明要写清三件事:
语句级合计占库级多少、落盘最多的是哪条(它自己占语句级、占库级各多少)、其余归不到具体语句。

**冠军自己占库级多少要直接写出来。** 2026-10-19 测试里只写了「语句级合计占库级 64%」与
「冠军占语句级 87%」,模型把 64% 套到了冠军这一条上(它实际占 56%)。
两边口径对不上(语句级反而比库级大)时直说「占库级多少」不可信,不替它编解释。
"""
from __future__ import annotations

from typing import Optional, Tuple


def _mib(byts: float) -> str:
    return "%.2fMiB" % (float(byts) / (1 << 20))


def describe(db_bytes: float, stmt_bytes: float, champ: Optional[Tuple[str, float]],
             capped: bool = False, off_list: str = "") -> str:
    """champ = (sql_id, 落盘字节);没有语句级落盘时传 None。

    capped:语句级合计只算了取回的那一批(取满了,实际可能更多)。
    off_list:冠军不在显示的榜上时补的一句(如「它不在按耗时排的前 10 名里」),由调用方给。
    """
    if stmt_bytes <= 0 or not champ or champ[1] <= 0:
        return ("语句统计里这段时间没有记到落盘（snap_summary_statement 的 sort/hash spill 都是 0）"
                "—— 落盘的不是有语句统计的那些执行，归不到某一条语句")
    sid, c = champ
    at_least = "至少 " if capped else ""
    tail = ("，" + off_list) if off_list else ""
    share = stmt_bytes / db_bytes if db_bytes > 0 else float("inf")
    if share > 1.05:
        who = "落盘最多的是 sqlid %s（%s，占语句级的 %.0f%%）%s" % (sid, _mib(c), 100.0 * c / stmt_bytes, tail)
        return ("语句级落盘合计 %s%s 反而大于库级 %s —— 两个口径对不上，「占库级多少」不可信；%s"
                % (at_least, _mib(stmt_bytes), _mib(db_bytes), who))
    who = ("落盘最多的是 sqlid %s（%s，占语句级的 %.0f%%、占库级的 %.0f%%）%s"
           % (sid, _mib(c), 100.0 * c / stmt_bytes, 100.0 * c / db_bytes, tail))
    rest = ("；其余 %s 归不到具体语句" % _mib(db_bytes - stmt_bytes)) if share < 0.95 else ""
    return "其中能归到语句的 %s%s（占库级 %.0f%%）；%s%s" % (at_least, _mib(stmt_bytes), 100.0 * share, who, rest)
