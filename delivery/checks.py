"""交付包自检:转化脚本生成交付包后逐项核对,任何一项出错就不提交。

  layout     顶层只能是清单规定的几样;技能、common 文件与清单逐个对上
  imports    静态:技能与 common 里引用的 common.* 都在包里;动态:每个技能脚本在交付包里真 import 一遍
             (机器上缺第三方库时只提示,不算失败——转化脚本只要求 Python 3.9+)
  content    测试环境路径、行名、密钥、内网 IP、对模拟组件的调用;注释里提到故障演练只计数提示
  whitelist  诊断包:scripts/registry 与 whitelist/scripts.sql 逐条一致(名单、SQL 正文、参数)
  junk       没有 __pycache__ / .pyc
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List

HERE = pathlib.Path(__file__).resolve().parent


def _sibling(name: str):
    """按文件载入同目录的模块,不经 sys.path:测试仓库里还有别的 checks / deps 之类的同名模块。"""
    spec = importlib.util.spec_from_file_location("delivery_%s_%d" % (name, abs(hash(str(HERE)))), HERE / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


deps = _sibling("deps")
regyaml = _sibling("regyaml")

THIRD_PARTY = {"yaml", "psycopg2", "cryptography", "sqlite_vec", "PIL", "pillow_heif"}

FORBIDDEN = (   # (说明, 正则);都是交付包里不该出现的
    ("测试环境的路径", r"/Users/sqlrush|/workspace/sqlrush|release-e2e|kf-verify|agent-nas-(k8s|rel)|p2venv|~/\.gdaa|host\.docker\.internal"),
    ("密钥", r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|\bsk-[A-Za-z0-9]{24,}"),
    ("内网 IP", r"(?<![\d.])(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])"),
    ("对模拟组件的调用", r"\b(import|from)\s+grmp_middleware|grmp_mock\.|tools/faultlab|faultlab\.py|sso-sim\.py|grmp_middleware/"),
)
MENTION = r"faultlab|grmp_mock|模拟中间件"          # 注释里的测试记录:不拦,计数写进差异摘要

# 行名:本仓库公开,不能写出行名本身,只存 sha256——中文按连续汉字里每个 2 ~ 6 字的片段比对,英文按单词(小写)比对
NAME_SHA256 = {
    "a5997a1549f6e36e873378de9edf0f957edc8760a99c73ece1d02f8c165bdbf5",
    "e7e901fd1d79de188b9b33c3ddc76d4c52b21807bb87842689759c46df99895d",
    "82f0ebfd4f57433d705a52e955e7e8e3162dec5122b78b7bc4692fd0b6750f81",
}
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_WORD = re.compile(r"[A-Za-z]{3,}")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def name_hit(text: str):
    """text 里第一处行名的位置(没有返回 None)。"""
    for m in _WORD.finditer(text):
        if _sha(m.group(0).lower()) in NAME_SHA256:
            return m.start()
    for m in _CJK_RUN.finditer(text):
        run = m.group(0)
        for n in range(2, 7):
            for i in range(len(run) - n + 1):
                if _sha(run[i:i + n]) in NAME_SHA256:
                    return m.start() + i
    return None


@dataclass
class Report:
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def merge(self, other: "Report") -> "Report":
        self.errors += other.errors
        self.notes += other.notes
        return self


def _text_files(root: pathlib.Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            try:
                yield p, p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue


def check_layout(root: pathlib.Path, manifest, package: str, expected: List[str]) -> Report:
    r = Report()
    pkg = manifest.PACKAGES[package]
    allowed = set(manifest.TOP_LEVEL[package])
    for p in root.iterdir():
        if p.name not in allowed and p.name != ".git":
            r.errors.append("交付包里多了顶层条目 %s" % p.name)
    skills = sorted(p.name for p in (root / "skills").iterdir() if p.is_dir())
    if skills != sorted(pkg["skills"]):
        r.errors.append("技能与清单不一致:包里 %s,清单 %s" % (skills, sorted(pkg["skills"])))
    have = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
                  and ".git" not in p.parts and p.name not in ("补充说明.md", ".delivery.json"))
    if have != sorted(expected):
        extra, lack = sorted(set(have) - set(expected)), sorted(set(expected) - set(have))
        r.errors.append("文件与清单装配结果不一致:多 %s,少 %s" % (extra[:5], lack[:5]))
    for s in pkg["skills"]:
        if any(x in manifest.SKILL_EXCLUDE_DIRS for p in (root / "skills" / s).rglob("*") for x in p.relative_to(root).parts):
            r.errors.append("技能 %s 里带了测试数据目录" % s)
    return r


def check_imports_static(root: pathlib.Path) -> Report:
    r = Report()
    for f, names in sorted(deps.unresolved_common_imports(root).items()):
        r.errors.append("%s 引用了包里没有的 %s" % (f, "、".join(sorted(names))))
    return r


_PROBE = r"""
import contextlib, importlib.util, io, json, pathlib, re, sys
root, skill = pathlib.Path(sys.argv[1]), sys.argv[2]
DIST = {"PyYAML": "yaml", "pillow": "PIL", "pillow-heif": "pillow_heif"}   # 脚本自己提示的包名 → 模块名
out = []
sys.path.insert(0, str(root))      # 与入口脚本一样:common 从交付包根目录找(-I 已排除其他来源)
for i, f in enumerate(sorted((root / "skills" / skill).rglob("*.py"))):
    if "__pycache__" in f.parts:
        continue
    sys.path.insert(0, str(f.parent))
    err = io.StringIO()
    try:
        spec = importlib.util.spec_from_file_location("deliv_probe_%d" % i, f)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        with contextlib.redirect_stderr(err):
            spec.loader.exec_module(mod)
        out.append([str(f.relative_to(root)), "ok", ""])
    except ModuleNotFoundError as exc:
        out.append([str(f.relative_to(root)), "missing", str(exc.name or "")])
    except SystemExit as exc:           # 脚本自己检查依赖:打印「缺少依赖 X」后退出
        m = re.search(r"缺少依赖 ([A-Za-z0-9_.-]+)", err.getvalue())
        if m:
            out.append([str(f.relative_to(root)), "missing", DIST.get(m.group(1), m.group(1))])
        else:
            out.append([str(f.relative_to(root)), "error", "SystemExit: %s %s" % (exc.code, err.getvalue()[-200:])])
    except BaseException as exc:
        out.append([str(f.relative_to(root)), "error", "%s: %s" % (type(exc).__name__, str(exc)[:200])])
    finally:
        sys.path.remove(str(f.parent))
print(json.dumps(out, ensure_ascii=False))
"""


def check_imports_dynamic(root: pathlib.Path, skills, python: str = sys.executable) -> Report:
    """每个技能一个干净子进程(-I:不读 PYTHONPATH 与用户目录,只能从交付包里找到 common)。"""
    r = Report()
    skipped = set()
    with tempfile.TemporaryDirectory() as home:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "GSDB_HOME": home, "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        for skill in skills:
            # -B:不写 __pycache__ —— 写进交付包就会被一起提交,摘要每次都不一样(演练时踩过)
            proc = subprocess.run([python, "-I", "-B", "-c", _PROBE, str(root), skill], capture_output=True,
                                  text=True, env=env, cwd=home, timeout=300)
            if proc.returncode != 0:
                r.errors.append("技能 %s 的导入检查没跑完:%s" % (skill, (proc.stderr or proc.stdout)[-300:]))
                continue
            for rel, status, detail in json.loads(proc.stdout.strip().splitlines()[-1]):
                top = detail.split(".")[0]
                if status == "missing" and top in THIRD_PARTY:
                    skipped.add(top)
                elif status != "ok":
                    r.errors.append("%s 导入失败:%s" % (rel, detail or status))
    if skipped:
        r.notes.append("本机没装 %s,用到它们的脚本没能真 import(静态检查已覆盖 common 依赖)" % "、".join(sorted(skipped)))
    return r


def check_content(root: pathlib.Path) -> Report:
    r = Report()
    mentions = 0
    for p, text in _text_files(root):
        rel = p.relative_to(root)
        for label, pat in FORBIDDEN:
            m = re.search(pat, text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                r.errors.append("%s:%d 有%s(%s)" % (rel, line, label, m.group(0)[:40]))
        pos = name_hit(text)
        if pos is not None:
            r.errors.append("%s:%d 有行名" % (rel, text.count("\n", 0, pos) + 1))   # 不回显原文
        mentions += len(re.findall(MENTION, text))
    if mentions:
        r.notes.append("注释里有 %d 处提到故障演练 / 模拟中间件(测试记录,不是依赖)" % mentions)
    return r


def _dml_rows(sql: str) -> Dict[str, Dict[str, str]]:
    """whitelist/scripts.sql 的 INSERT → {脚本名: {sql, params, dbtype}}(单引号转义还原)。"""
    rows = {}
    pat = re.compile(r"VALUES \(grmp\.script_config_seq\.nextval, 'SQL', '([a-z_]+\.[a-z_0-9]+)', "
                     r"'([^']*)', \d+, '[^']*', NULL, NULL, NULL, 'centralization', '((?:[^']|'')*)', '((?:[^']|'')*)'")
    for m in pat.finditer(sql):
        rows[m.group(1)] = {"dbtype": m.group(2), "sql": m.group(3).replace("''", "'"),
                            "params": m.group(4).replace("''", "'")}
    return rows


def check_whitelist(root: pathlib.Path) -> Report:
    r = Report()
    dml = (root / "whitelist" / "scripts.sql").read_text(encoding="utf-8")
    rows = _dml_rows(dml)
    if len(rows) != len(re.findall(r"^INSERT INTO", dml, re.M)):
        r.errors.append("whitelist/scripts.sql 有 INSERT 没解析出来(格式变了?)")
    defs = {}
    for f in sorted((root / "scripts" / "registry").glob("*/*.yaml")):
        d = regyaml.load(f)
        defs[d["name"]] = d
    if set(defs) != set(rows):
        r.errors.append("白名单名单不一致:定义多 %s,DML 多 %s" % (sorted(set(defs) - set(rows))[:5], sorted(set(rows) - set(defs))[:5]))
    for name in sorted(set(defs) & set(rows)):
        if defs[name]["sql"].strip() != rows[name]["sql"].strip():
            r.errors.append("白名单 %s 的 SQL 与 scripts.sql 不一致" % name)
        dbtype = defs[name].get("database_type") or "postgres"
        if dbtype != rows[name]["dbtype"]:
            r.errors.append("白名单 %s 的执行库(database_type)定义是 %s,scripts.sql 是 %s" % (name, dbtype, rows[name]["dbtype"]))
        # appbusiness 的脚本 parameter_config 最前面是路由参数 dbName(写法与行方一致,type 是 String)
        keys = ([("dbName", "String")] if dbtype == "appbusiness" else []) \
            + [(p["key"], str(p["type"]).upper()) for p in defs[name].get("params") or []]
        got = [(p["key"], p["type"]) for p in json.loads(rows[name]["params"] or "[]")]
        if keys != got:
            r.errors.append("白名单 %s 的参数定义与 scripts.sql 不一致" % name)
    r.notes.append("白名单 %d 条,与 whitelist/scripts.sql 逐条一致" % len(rows) if not r.errors else "")
    r.notes = [n for n in r.notes if n]
    return r


def check_junk(root: pathlib.Path) -> Report:
    """字节码缓存进了交付包就会被一起提交,摘要每次都不一样。"""
    r = Report()
    junk = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.name == "__pycache__" or p.suffix == ".pyc")
    if junk:
        r.errors.append("交付包里出现了字节码缓存:%s" % "、".join(junk[:5]))
    return r


def run_all(root: pathlib.Path, manifest, package: str, expected: List[str], python: str = sys.executable) -> Report:
    rep = Report()
    rep.merge(check_layout(root, manifest, package, expected))
    rep.merge(check_imports_static(root))
    rep.merge(check_imports_dynamic(root, manifest.PACKAGES[package]["skills"], python))
    rep.merge(check_content(root))
    if manifest.PACKAGES[package]["whitelist"]:
        rep.merge(check_whitelist(root))
    return rep.merge(check_junk(root))
