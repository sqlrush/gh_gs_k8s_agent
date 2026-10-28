"""验收知识库:跑题期间把测试环境的共用知识库换成「样例知识 + 官方文档集」,结束后原样还原。

评分标准里的知识引用都对着样例知识库(agent/skills/gaussdb-kb-import/testdata/sample-kb,全部虚构)写,
测试环境的共用知识库内容另有用途、会变,所以每轮:
  backup   把共用知识库(官方文档集 docsets/ 除外,它不动)rsync 到备份目录,写一个标记;
  install  清掉条目与索引(保留 docsets/、kb.yaml、向量缓存),放进样例知识;
  reindex  在本人导入容器里跑 kb.py index,直到退出 0(官方文档集按时限分几次建完);
  restore  备份 rsync --delete 回去,删备份。
**备份没还原之前不许开下一轮**(标记还在就拒绝),中途中断用 `run.py restore`。
本机还留一份验收知识库副本(local_dir,docsets 指回共用目录),评分时核对引用编号用。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import time
from typing import Callable, Dict, List, Mapping, Optional, Sequence

REPO = pathlib.Path(__file__).resolve().parents[2]
SAMPLE = REPO / "agent" / "skills" / "gaussdb-kb-import" / "testdata" / "sample-kb"
ITEM_DIRS = ("cases", "rules", "runbooks", "defects", "product", "profiles", "baselines", "wiki", "graph")
# 「知识库变了」只看知识条目本身:sources/(ingest 就地留的原文)、topics/ 与顶层索引文件(index 重建)不算 ——
# 它们在写库前的正常步骤里就会变,不是「未经确认就入库」
KNOWLEDGE_DIRS = ITEM_DIRS + ("archive", "errata", "evidence", "guides", "strategies")
NOT_TRACKED = ("docsets", "index", "inbox", "init")
KEEP = ("docsets", "kb.yaml")
KEEP_INDEX = ("embed-cache",)                         # 向量缓存按内容寻址,留着能省掉重算
MARKER = ".acceptance-backup.json"


class SwapError(Exception):
    pass


def _rsync(args: Sequence[str]) -> None:
    p = subprocess.run(["rsync", "-a", *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise SwapError("rsync %s 失败:%s" % (" ".join(args), (p.stderr or p.stdout)[-300:]))


def build_local(local: pathlib.Path, nas: pathlib.Path, sample: pathlib.Path = SAMPLE) -> None:
    """本机的验收知识库副本:样例知识 + 指回共用目录的 docsets(评分时 cite-check 用)。"""
    if local.exists():
        shutil.rmtree(local)
    local.mkdir(parents=True)
    for d in ITEM_DIRS:
        if (sample / d).is_dir():
            shutil.copytree(sample / d, local / d)
    shutil.copy2(sample / "VERSION", local / "VERSION")
    if (nas / "docsets").is_dir():
        (local / "docsets").symlink_to(nas / "docsets", target_is_directory=True)
    write_default_strategy(local)


def write_default_strategy(kb: pathlib.Path) -> None:
    """预置工单转化策略(全按默认):不然每道导入题都停在「首次导入」的 8 个策略问题上,走不到抽取与确认清单。
    首次导入要问策略这件事本身由 gaussdb-kb-import 的单测覆盖。用技能自己的函数写,格式跟着技能走。"""
    import sys
    for p in (REPO / "agent", REPO / "agent" / "skills" / "gaussdb-kb-import" / "scripts"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    import kb_cases  # type: ignore  # noqa: E402
    kb_cases.write_default_strategy(kb)


def pending_backup(backup: pathlib.Path) -> Optional[dict]:
    m = backup / MARKER
    if not m.is_file():
        return None
    try:
        return json.loads(m.read_text(encoding="utf-8"))
    except ValueError:
        return {"run": "?"}


def backup(nas: pathlib.Path, dst: pathlib.Path, run_id: str) -> None:
    left = pending_backup(dst)
    if left:
        raise SwapError("上一轮(%s)的知识库备份还没还原:先跑 run.py restore" % left.get("run"))
    if not (nas / "docsets").is_dir():
        raise SwapError("%s 不像知识库目录(没有 docsets/)" % nas)
    dst.mkdir(parents=True, exist_ok=True)
    _rsync(["--delete", "--exclude", "/docsets", "--exclude", "/" + MARKER, str(nas) + "/", str(dst) + "/"])
    (dst / MARKER).write_text(json.dumps({"run": run_id, "at": time.strftime("%F %T"), "nas": str(nas)},
                                         ensure_ascii=False), encoding="utf-8")


def install(nas: pathlib.Path, local: pathlib.Path, backup_dir: pathlib.Path) -> None:
    if not pending_backup(backup_dir):
        raise SwapError("没有这一轮的备份,不动共用知识库")       # 先备份成功才许清
    for entry in nas.iterdir():
        if entry.name in KEEP:
            continue
        if entry.name == "index" and entry.is_dir():
            for sub in entry.iterdir():
                if sub.name not in KEEP_INDEX:
                    shutil.rmtree(sub) if sub.is_dir() else sub.unlink()
            continue
        shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    for d in ITEM_DIRS + ("strategies",):
        if (local / d).is_dir():
            shutil.copytree(local / d, nas / d)
    shutil.copy2(local / "VERSION", nas / "VERSION")
    (nas / "inbox" / "uploads").mkdir(parents=True, exist_ok=True)
    subprocess.run(["chmod", "-R", "a+rwX", str(nas)], capture_output=True)


def restore(nas: pathlib.Path, src: pathlib.Path) -> str:
    info = pending_backup(src)
    if not info:
        return "没有待还原的备份"
    _rsync(["--delete", "--exclude", "/docsets", "--exclude", "/" + MARKER, str(src) + "/", str(nas) + "/"])
    shutil.rmtree(src)
    return "已还原(备份来自 %s)" % info.get("run")


def fingerprint(nas: pathlib.Path) -> Dict[str, str]:
    """知识库里每个文件的 sha256(官方文档集、索引、收件、标准化目录不算):导入题前后对比用。"""
    out: Dict[str, str] = {}
    for entry in sorted(nas.iterdir()):
        if entry.name in NOT_TRACKED or entry.name.startswith("."):
            continue
        files = [entry] if entry.is_file() else sorted(p for p in entry.rglob("*") if p.is_file())
        for f in files:
            out[f.relative_to(nas).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def changed(before: Mapping[str, str], after: Mapping[str, str]) -> List[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def knowledge_changed(paths: Sequence[str]) -> List[str]:
    """变了的文件里,属于知识条目的那些(导入题的一票否决看它)。"""
    return [p for p in paths if "/" in p and p.split("/", 1)[0] in KNOWLEDGE_DIRS]


def reindex(exec_in_pod: Callable[[List[str]], subprocess.CompletedProcess], kb_pod: str, script: str,
            rounds: int = 30) -> str:
    """在导入容器里重建索引;官方文档集按时限分批,没建完退出 2,接着跑。"""
    last = ""
    for _ in range(rounds):
        p = exec_in_pod(["python3", script, "index", "--kb", kb_pod])
        last = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 0:
            return last
        if p.returncode != 2:
            raise SwapError("kb.py index 失败(退出 %d):%s" % (p.returncode, last[-400:]))
    raise SwapError("kb.py index 跑了 %d 次还没建完:%s" % (rounds, last[-300:]))
