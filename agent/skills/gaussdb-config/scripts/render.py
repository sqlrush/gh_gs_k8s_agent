"""参数体检的渲染 —— 纯函数，不连库。"""
from __future__ import annotations

from typing import Dict, List

from baseline import Setting, how_to_apply

# 名字里带这些词的参数,取值一律不展示 —— `--show` 是用户自己点名要看的,
# 但点名也不该把口令类取值打印到聊天记录/报告存档里(两者都会被转发、被备份)。
_REDACT = ("password", "passwd", "secret", "token", "key_file", "ssl_key")


def _redacted(name: str) -> bool:
    low = name.lower()
    return any(w in low for w in _REDACT)


def table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return ""
    out = "| " + " | ".join(headers) + " |\n"
    out += "|" + "|".join(["---"] * len(headers)) + "|\n"
    for r in rows:
        out += "| " + " | ".join(str(c) for c in r) + " |\n"
    return out


_BYTE_UNITS = ("B", "kB", "8kB", "16kB", "32kB", "MB", "GB")
_TIME_UNITS = ("ms", "s", "min", "h", "d")


def human(s: Setting) -> str:
    """归一后的人类可读值。

    **这一列不是装饰。** `shared_buffers` 的单位是 8kB,现值 524288 —— 不换算
    就会被读成 512KB 或 524MB,而真实是 4GB。本 skill 自己的 SKILL.md 里也警告过
    这个误读,那就必须在输出里真的把换算给出来,而不是让人自己乘。
    """
    if _redacted(s.name) or s.num is None:
        return "-"
    if s.unit in _BYTE_UNITS:
        f = float(s.num)
        for u in ("B", "K", "M", "G", "T"):
            if f < 1024 or u == "T":
                return "%.1f%s" % (f, u)
            f /= 1024
    if s.unit in _TIME_UNITS:
        ms = float(s.num)
        if ms == 0:
            return "0（不限制）"
        if ms < 1000:
            return "%.0f 毫秒" % ms
        if ms < 60000:
            return "%.1f 秒" % (ms / 1000)
        return "%.1f 分钟" % (ms / 60000)
    return "-"


_HEAD = """# 运行参数体检

> 本体检**只报客观上就是错的**三类问题：参数自相矛盾、会静默丢数据或丢可观测性、
> 按本实例内存预算（`max_process_memory`）算出来的超卖。
> **不给「推荐值」** —— 多数性能参数的合理值取决于机器规格与业务形态，
> 而本工具看不到 CPU 核数、物理内存和磁盘类型；对着看不到的事实报数字就是编依据。
> 要调性能参数，请结合压测与 `gaussdb-wdr` 的实测结论。

"""


def report(st: Dict[str, Setting], findings: list, show: List[str]) -> str:
    out = _HEAD
    out += "共读取 %d 个运行参数。\n\n" % len(st)

    out += "## 体检结论\n\n"
    if not findings:
        out += ("未发现上述三类问题。**这不等于「参数都调优了」** —— "
                "本体检不评估性能参数是否适合这台机器。\n\n")
    else:
        rows = [[f.severity.label(), f.code, f.metric, f.value] for f in findings]
        out += table(["严重度", "Code", "参数", "现值"], rows) + "\n"
        for f in findings:
            out += "### %s %s · %s\n\n" % (f.severity.label(), f.code, f.metric)
            out += "- 现值：`%s`（阈值/期望 %s）\n" % (f.value, f.threshold)
            out += "- %s\n\n" % f.evidence

    if show:
        out += "## 人工核对（--show）\n\n"
        rows = []
        for name in show:
            s = st.get(name)
            if s is None:
                rows.append([name, "（这个实例上没有这个参数）", "", "", "", ""])
                continue
            shown = "[已隐去]" if _redacted(s.name) else (s.raw or "(空)")
            rows.append([s.name, shown, s.unit or "-", human(s),
                         "是" if s.changed_from_default else "否", how_to_apply(s)])
        out += table(["参数", "现值", "单位", "归一后", "改过默认值", "生效方式"],
                     rows) + "\n"

    out += ("—— 本报告只读采集；所有处置都要 DBA 人工执行，"
            "本 skill 不会修改任何参数。\n")
    return out
