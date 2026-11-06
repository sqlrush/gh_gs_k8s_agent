"""先写临时文件再改名:读 Pod 与写 Pod 共享 NAS,任何时刻读到的都是完整文件。"""
from __future__ import annotations

import os
import pathlib


def write_text_atomic(path: pathlib.Path, text: str) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
