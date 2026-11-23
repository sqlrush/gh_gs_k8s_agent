"""common 模块的静态依赖分析(只用标准库 ast):某个技能顺着 import 会用到哪些 common 模块。

守卫测试用它核对清单的归属,转化脚本的自检用它确认裁剪后的包里依赖都在。只认 import 语句;函数里的延迟导入
同样算依赖(执行到就要有)。按「文件存在」定位 common 的写法(如 common/sql.py)看不出来,那种依赖写在清单的注释里,
由「每个技能脚本在裁剪后的包里真 import 一遍」兜底。
"""
from __future__ import annotations

import ast
import pathlib
from typing import Dict, Iterable, Set


def module_name(rel: pathlib.PurePath) -> str:
    """common/kb/query.py → common.kb.query;common/kb/__init__.py → common.kb。"""
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def common_modules(root: pathlib.Path) -> Dict[str, pathlib.Path]:
    """root(agent/ 或交付包根)下 common/ 的全部模块:模块名 → 文件。"""
    out = {}
    for p in sorted((root / "common").rglob("*.py")):
        if "__pycache__" not in p.parts:
            out[module_name(p.relative_to(root))] = p
    return out


def _resolve(name: str, mods: Dict[str, pathlib.Path]) -> Set[str]:
    while name.startswith("common"):
        if name in mods:
            return {name}
        if "." not in name:
            return set()
        name = name.rsplit(".", 1)[0]
    return set()


def _package_of(path: pathlib.Path, root: pathlib.Path) -> str:
    rel = path.relative_to(root)
    name = module_name(rel)
    return name if path.name == "__init__.py" else name.rsplit(".", 1)[0]


def file_imports(path: pathlib.Path, root: pathlib.Path, mods: Dict[str, pathlib.Path]) -> Set[str]:
    """一个文件引用到的 common 模块(含相对导入、from common import x 里的子模块)。"""
    found: Set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    pkg = _package_of(path, root) if path.is_relative_to(root / "common") else ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found |= _resolve(a.name, mods)
        elif isinstance(node, ast.ImportFrom):
            mod = _absolute(node, pkg)
            if not mod.startswith("common"):
                continue
            for a in node.names:
                sub = _resolve(mod + "." + a.name, mods)
                found |= sub if sub else _resolve(mod, mods)
            found |= _resolve(mod, mods)
    return found


def closure(start: Iterable[str], root: pathlib.Path, mods: Dict[str, pathlib.Path]) -> Set[str]:
    """从 start 出发的全部 common 模块;导入子模块时上级包的 __init__ 也会执行,一并算上。"""
    seen: Set[str] = set()
    todo = list(start)
    cache: Dict[str, Set[str]] = {}
    while todo:
        m = todo.pop()
        if m in seen or m not in mods:
            continue
        seen.add(m)
        parts = m.split(".")
        todo.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        if m not in cache:
            cache[m] = file_imports(mods[m], root, mods)
        todo.extend(cache[m])
    return seen


def skill_closure(root: pathlib.Path, skill: str, mods: Dict[str, pathlib.Path]) -> Set[str]:
    """一个技能(全部 .py)用到的 common 模块。"""
    direct: Set[str] = set()
    for f in sorted((root / "skills" / skill).rglob("*.py")):
        if "__pycache__" not in f.parts:
            direct |= file_imports(f, root, mods)
    return closure(direct, root, mods)


def _exports(init: pathlib.Path) -> Set[str]:
    """包的 __init__.py 里定义或导入的名字(from 包 import 名字 时,名字可以是这些,也可以是子模块)。"""
    names: Set[str] = set()
    for node in ast.parse(init.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return names


def _absolute(node: ast.ImportFrom, pkg: str) -> str:
    if not node.level:
        return node.module or ""
    base = pkg.split(".")
    base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join(base + ([node.module] if node.module else []))


def unresolved_common_imports(root: pathlib.Path) -> Dict[str, Set[str]]:
    """交付包自检:技能与 common 里引用的 common.* 在包里找不到的(文件 → 缺的名字)。

    `from common import kbsection` 只有 common 包在还不够:kbsection 要么是包里的子模块,
    要么是 common/__init__.py 里定义的名字,否则就是裁剪时漏了文件。
    """
    mods = common_modules(root)
    exports = {m: _exports(p) for m, p in mods.items() if p.name == "__init__.py"}
    missing: Dict[str, Set[str]] = {}
    for f in sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        pkg = _package_of(f, root) if f.is_relative_to(root / "common") else ""
        lack: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                lack |= {a.name for a in node.names if a.name.startswith("common") and a.name not in mods}
            elif isinstance(node, ast.ImportFrom):
                mod = _absolute(node, pkg)
                if not mod.startswith("common"):
                    continue
                if mod not in mods:
                    lack.add(mod)
                    continue
                if mod not in exports:          # 从普通模块取名字:名字是模块的属性,不是文件,不在这里核对
                    continue
                for a in node.names:
                    if a.name != "*" and mod + "." + a.name not in mods and a.name not in exports[mod]:
                        lack.add(mod + "." + a.name)
        if lack:
            missing[str(f.relative_to(root))] = lack
    return missing
