"""残留登记簿:记下「不会随会话结束自己消失」的东西,交给 cleanup 逐项收。

**哪些东西会留下来:**
  · 两阶段事务(PREPARE 了没提交)—— 杀会话也在,必须 ROLLBACK PREPARED
  · 复制槽 —— 槽在,WAL 就一直不回收
  · 表级参数(关掉的 autovacuum、调过的 vacuum 限速)
  · 跑中途加出来的列、建出来的索引

**为什么要写进文件而不是只存在内存里:** 编排器被 Ctrl-C、被 kill、Mac 睡过去,内存里的
清单就没了,而库里的东西还在。下一次启动时 cleanup 要能从文件里读出「上次留了什么」
并先收干净 —— 否则这些残留会一直攒着,而且攒到后来谁也说不清是哪一次留下的。

**文件格式故意选 JSON 而不是追加日志:** 同一个东西可能反复加上又撤掉(K5 的索引每轮一次),
用集合语义最省事;日志式的话要自己做「加了又删」的抵消。
"""
from __future__ import annotations

import json
import threading
from typing import Dict, List, Tuple

from . import config as C

_LOCK = threading.Lock()

# 认得的残留种类 → 一句话说明(cleanup 按种类分别处理;不认得的种类会被当成错误)
KINDS: Dict[str, str] = {
    "prepared_xact": "两阶段事务",
    "slot": "复制槽",
    "table_option": "表级参数",
    "column": "加出来的列",
    "index": "建出来的索引",
}


def _load() -> Dict[str, List[str]]:
    if not C.STATE_FILE.exists():
        return {}
    try:
        data = json.loads(C.STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 文件坏了不能当「没有残留」—— 那会让真实残留被永久忽略。
        # 但也不能在这里抛错(调用方多半在清理路径上),所以退回空表并留痕。
        return {"_broken": [str(C.STATE_FILE)]}
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, List[str]]) -> None:
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = C.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(C.STATE_FILE)           # 原子替换:清理途中断电也不会读到半个文件


def add_residue(kind: str, name: str) -> None:
    """登记一件要收的东西。重复登记同一件 = 一件。"""
    if kind not in KINDS:
        raise ValueError("不认得的残留种类 %r(认得的:%s)" % (kind, ", ".join(KINDS)))
    with _LOCK:
        data = _load()
        items = [x for x in data.get(kind, []) if isinstance(x, str)]
        if name not in items:
            items.append(name)
        data[kind] = items
        _save(data)


def drop_residue(kind: str, name: str) -> None:
    """这件已经自己收干净了,从清单里划掉。"""
    with _LOCK:
        data = _load()
        data[kind] = [x for x in data.get(kind, []) if x != name]
        _save(data)


def residues() -> List[Tuple[str, str]]:
    """清单里还剩什么。返回 (种类, 名字) 的列表。"""
    out: List[Tuple[str, str]] = []
    with _LOCK:
        for kind, items in _load().items():
            for name in items or []:
                out.append((kind, name))
    return out


def clear() -> None:
    with _LOCK:
        _save({})
