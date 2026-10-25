"""GUC → 代价常数（含单位换算）。

单位换算是这一层唯一的活，也是唯一能静默毁掉整套推演的地方：

    effective_cache_size 的 setting 是「8kB 块的个数」，不是字节
    work_mem 的 setting 是 kB 的个数
    block_size 的 setting 就是字节

换算错了，复算出来的每个数会按同一个比例偏 —— 而按比例偏的数看起来完全
正常。只有校准闸能发现，且报出来像是「模型不适用于这个实例」，排查方向
会被带到模型上去，不会有人想到是单位。所以这里的单位一律从 pg_settings
的 unit 列**读**，不写死。

**缺项一律报错，不取默认值。** 用 PostgreSQL 出厂值顶替，在实例调过参时
会让校准闸失败；失败信息指向模型而不是指向「这个 GUC 没取到」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable


class MissingConstant(Exception):
    """代价常数缺失或无法解析。调用方必须停止推演。"""


# pg_settings.unit 的形态是「倍数 + 单位」，倍数省略时为 1：'kB' / '8kB' / 'MB'
_UNIT_RE = re.compile(r"^(\d*)(B|kB|MB|GB|TB)$")
_UNIT_BYTES = {"B": 1, "kB": 1024, "MB": 1024 ** 2,
               "GB": 1024 ** 3, "TB": 1024 ** 4}

# 无单位的纯浮点代价常数
_COST_NAMES = ("seq_page_cost", "random_page_cost", "cpu_tuple_cost",
               "cpu_index_tuple_cost", "cpu_operator_cost")


@dataclass(frozen=True)
class CostConstants:
    seq_page_cost: float
    random_page_cost: float
    cpu_tuple_cost: float
    cpu_index_tuple_cost: float
    cpu_operator_cost: float
    block_size: int              # 字节
    effective_cache_size: int    # **页数**，不是字节
    work_mem: int                # 字节
    query_dop: int = 1           # openGauss 并行度

    def describe(self) -> list:
        """给推演报告用的「本实例实际值」清单。

        报告里必须把这些原样列出来 —— 读的人要能看出哪些是出厂默认、哪些
        被调过。只给最终 cost 而不给常数，等于要求读者相信我用对了参数。
        """
        return [
            ("seq_page_cost", "%g" % self.seq_page_cost),
            ("random_page_cost", "%g" % self.random_page_cost),
            ("cpu_tuple_cost", "%g" % self.cpu_tuple_cost),
            ("cpu_index_tuple_cost", "%g" % self.cpu_index_tuple_cost),
            ("cpu_operator_cost", "%g" % self.cpu_operator_cost),
            ("block_size", "%d B" % self.block_size),
            ("effective_cache_size", "%d 页" % self.effective_cache_size),
            ("work_mem", "%d B" % self.work_mem),
            ("query_dop", "%d" % self.query_dop),
        ]


def from_gucs(rows: Iterable[Any]) -> CostConstants:
    """rows 是 evidence.GUC 序列（或任何带 name/setting/unit 的对象、映射）。"""
    table = _index_by_name(rows)

    values = {name: _as_float(name, table) for name in _COST_NAMES}

    block_size = int(_as_float("block_size", table))
    if block_size <= 0:
        raise MissingConstant("block_size 取到 %r，不是正数" % block_size)

    # effective_cache_size 的 unit 通常是 '8kB'：setting 是块数不是字节
    cache_bytes = _as_bytes("effective_cache_size", table)

    # query_dop 是 openGauss 特有的并行度。缺了它就只能当 1，而实例上是 2 时
    # 每个复算值会差一倍 —— 校准闸只会报「模型不适用」，不会说少的是并行度。
    dop = int(_as_float("query_dop", table))
    if dop < 1:
        raise MissingConstant("query_dop 取到 %r，不是正整数" % dop)

    return CostConstants(
        block_size=block_size,
        effective_cache_size=max(1, cache_bytes // block_size),
        work_mem=_as_bytes("work_mem", table),
        query_dop=dop,
        **values,
    )


# --- 内部 --------------------------------------------------------------------

def _index_by_name(rows: Iterable[Any]) -> Dict[str, tuple]:
    out: Dict[str, tuple] = {}
    for row in rows:
        if isinstance(row, dict):
            name, setting, unit = row.get("name"), row.get("setting"), row.get("unit")
        else:
            name = getattr(row, "name", None)
            setting = getattr(row, "setting", None)
            unit = getattr(row, "unit", "")
        if not name:
            continue
        out[str(name)] = (setting, unit or "")
    return out


def _require(name: str, table: Dict[str, tuple]) -> tuple:
    if name not in table:
        raise MissingConstant(
            "缺少 GUC %r —— 代价推演的输入不全，不能继续。"
            "它应由 sqltune.key_gucs 取回；若 openGauss 上不存在这个名字，"
            "要补映射并说明，不要在代码里填默认值：默认值会让校准闸失败，"
            "而失败信息会指向模型，不会指向这里。" % name)
    setting, unit = table[name]
    if setting is None or str(setting).strip() == "":
        # 协议把 NULL 渲染成空串 —— 与真空串不可区分，一律当取数失败
        raise MissingConstant("GUC %r 取到空值" % name)
    return str(setting).strip(), str(unit).strip()


def _as_float(name: str, table: Dict[str, tuple]) -> float:
    setting, _ = _require(name, table)
    try:
        return float(setting)
    except ValueError as exc:
        raise MissingConstant("GUC %r 的值 %r 不是数值" % (name, setting)) from exc


def _as_bytes(name: str, table: Dict[str, tuple]) -> int:
    """把带单位的内存类 GUC 换算成字节。

    unit 为空时按**字节**处理 —— pg_settings 里无单位的内存量就是字节
    （block_size 是这一类）。不做「猜它大概是 kB」这种事。
    """
    setting, unit = _require(name, table)
    try:
        raw = float(setting)
    except ValueError as exc:
        raise MissingConstant("GUC %r 的值 %r 不是数值" % (name, setting)) from exc

    if not unit:
        return int(raw)

    match = _UNIT_RE.match(unit)
    if not match:
        raise MissingConstant(
            "GUC %r 的单位 %r 不认识。已知形态是「倍数+单位」（如 8kB、kB、MB）。"
            "遇到新单位要在 costconst.py 里补，别忽略 —— 忽略等于按字节算，"
            "effective_cache_size 会小 8192 倍。" % (name, unit))
    multiple = int(match.group(1) or 1)
    return int(raw * multiple * _UNIT_BYTES[match.group(2)])
