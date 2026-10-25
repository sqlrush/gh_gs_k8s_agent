#!/usr/bin/env python3
"""`gaussdb-lockwait` 的双模式端到端矩阵：同一批用例分别走 `api`
（`-c og-grmp`，中间件）与 `gsql`（`-c og-gsql`，直连）两条真实进程路径各跑
一次，逐例断言退出码与关键内容。

**为什么两条路径都要跑，不能只信其中一条：** 两条路径背后是完全不同的
代码（`common/grmp/runner.py` 的 `GrmpRunner` vs `DirectRunner`），一条
路径测过不代表另一条也对——`common/access.py` 里语句超时的处理就是
一个已知的、**有意为之**的行为分叉（中间件协议没有这个旋钮），所以
`--timeout` 那条用例才特意在两条路径上断言相反的 stderr 内容，而不是
两边断言同一句话。

**运行本工具前必须先部署**（把仓库当前代码同步到 `GDAA_SKILLS_DIR` 指向
的目录），否则测的是上一次部署时的旧代码，本工具却以为测的是当下改动：

    ssh <用户>@<测试机> \\
      "cd ~/gh_skill && bash opencode_skill-main-v2-0729/install-opencode.sh \\
         --dest ~/.config/opencode/skills-dev"
    ssh <用户>@<测试机> \\
      "cd /tmp && python3 ~/gh_skill/opencode_skill-main-v2-0729/tools/matrix_lockwait.py"

**部署目标只认 skills-dev，绝不是 skills**：后者是用户在别的工具里正在用
的目录，本工具默认也只指向 skills-dev；换正式目录是运维操作，改
GDAA_SKILLS_DIR 环境变量即可，不改这份代码。

**判定的两条底线（对每一个用例都成立，不分 baseline 还是三层堵塞用例）：**

    1. 输出（stdout+stderr）里出现 Traceback 一律判 FAIL，无论期望是什么——
       未捕获异常意味着这条路径没人设计过。
    2. 期望被拒的用例（rc 应为非 0）如果退出码是 0，一律判 FAIL——
       静默放行比明确报错更危险。

三层堵塞用例复用 `probe_lock_chain_e2e.py` 的 `build_three_level_chain()`：
一套真实的 A 等 B、B 等 C 排队链，两条访问路径各查一次同一个数据库状态，
断言两边都把根算成 C。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# GSDB_HOME 决定 common.config.resolve() 去哪个目录找 config.yaml/凭据——
# 链条编排（probe_lock_chain_e2e.build_three_level_chain）用 psycopg2 连接
# "og"，这条连接定义在 ~/.gdaa/config.yaml 里。必须在 import
# probe_lock_chain_e2e 之前（更准确地说，在它第一次真正发起连接之前）
# 定下来；不像各子进程用例，那些用各自独立的 env dict，不受这一行影响。
_GDAA_HOME = os.path.expanduser(os.environ.get("GDAA_HOME_FOR_MATRIX", "~/.gdaa"))
os.environ.setdefault("GSDB_HOME", _GDAA_HOME)

import probe_lock_chain_e2e as chain_e2e  # noqa: E402  （复用三层链搭建逻辑）

# 已安装 skill 的根目录。**这是唯一允许写死的路径**——切正式环境时只改
# 这一个环境变量，不改代码。绝不能默认指向 ~/.config/opencode/skills：
# 那是用户正在用的目录，本工具跑起来会产生大量真实连接与真实（自建）锁，
# 不该碰用户正在用的安装。
SK = os.environ.get("GDAA_SKILLS_DIR",
                    os.path.expanduser("~/.config/opencode/skills-dev"))
LOCKWAIT_PY = pathlib.Path(SK) / "gaussdb-lockwait" / "scripts" / "lockwait.py"
PY = sys.executable or "python3"

API_CONN = "og-grmp"
GSQL_CONN = "og-gsql"
ORCH_CONN = "og"          # 三层链编排用：psycopg2，持久会话
BOGUS_CONN = "zz-lockwait-matrix-nope"   # 明确不存在，两种模式下都该被拒

GSQL_HOME = "/tmp/gsql-probe"

RUN_TIMEOUT_S = 60

_TRACEBACK_MARK = "Traceback (most recent call last)"


class ModeSetupError(RuntimeError):
    """某个访问模式的运行环境本身有问题（凭据/令牌缺失等），矩阵不跑。"""


def _grmp_token() -> str:
    """从 `~/.gdaa/grmp.env` 里取 `GRMP_AUTH_TOKEN`。

    该文件的注释写明这是给 shell `source` 用的（见文件内容），但子进程的
    env 是一个 dict，没有 shell 语义可以真的执行 source——这里改用正则
    直接抽取值。只认这一个变量，不打印、不落进任何输出。
    """
    path = pathlib.Path(_GDAA_HOME) / "grmp.env"
    if not path.exists():
        raise ModeSetupError("api 模式需要的令牌文件不存在：%s" % path)
    text = path.read_text(encoding="utf-8")
    m = re.search(r'^export\s+GRMP_AUTH_TOKEN=(\S+)\s*$', text, re.MULTILINE)
    if not m:
        raise ModeSetupError("%s 里没找到 GRMP_AUTH_TOKEN" % path)
    return m.group(1).strip("'\"")


def _api_env() -> dict:
    env = dict(os.environ)
    env["GSDB_HOME"] = _GDAA_HOME
    env["GRMP_AUTH_TOKEN"] = _grmp_token()
    return env


def _gsql_env() -> dict:
    env = dict(os.environ)
    env["GSDB_HOME"] = GSQL_HOME
    env["PATH"] = str(pathlib.Path(GSQL_HOME) / "bin") + os.pathsep + env.get("PATH", "")
    return env


# (模式名, 该模式下的连接名, 构造子进程 env 的函数)
MODES = (
    ("api", API_CONN, _api_env),
    ("gsql", GSQL_CONN, _gsql_env),
)


def run_lockwait(env: dict, conn: str, extra_args: list):
    argv = [PY, str(LOCKWAIT_PY), "-c", conn] + list(extra_args)
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT_S, env=env)
        return p.returncode, p.stdout, p.stderr, time.time() - t0
    except subprocess.TimeoutExpired:
        return -99, "", "TIMEOUT after %ds" % RUN_TIMEOUT_S, time.time() - t0


# ---------------------------------------------------------------------------
# 逐用例判定：每个 checker 拿到 (rc, out, err, mode) 返回 (ok: bool, note: str)
# ---------------------------------------------------------------------------

def check_no_block(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    if "当前无锁等待" not in out:
        return False, "输出未包含「当前无锁等待」——空结果必须明说，不能只是留白"
    return True, ""


def check_json(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    try:
        data = json.loads(out)
    except Exception as exc:
        return False, "stdout 不是合法 JSON：%r；前 200 字：%s" % (exc, out[:200])
    if data.get("skill") != "gaussdb-lockwait":
        return False, "skill 字段是 %r，期望 'gaussdb-lockwait'" % data.get("skill")
    return True, ""


def check_limit1(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    return True, ""


def check_timeout(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    has_notice = "无法设置语句超时" in err
    if mode == "api" and not has_notice:
        return False, "api 模式（中间件协议没有语句超时旋钮）stderr 里没有" \
                      "「无法设置语句超时」的提示，用户会误以为 --timeout 生效了"
    if mode == "gsql" and has_notice:
        return False, "gsql 模式能真正设置语句超时，stderr 却出现了" \
                      "「无法设置语句超时」——这条提示只该出现在 api 模式"
    return True, ""


def check_bad_conn(rc, out, err, mode):
    if rc != 2:
        return False, "rc=%d（期望 2：连接名不存在应被干净拒绝）：%s" \
                      % (rc, (err or out)[:200])
    return True, ""


# name, conn_override（None 表示用该模式默认连接）, extra_args, expect_reject, checker
BASELINE_CASES = (
    ("无堵塞时", None, [], False, check_no_block),
    ("--format json", None, ["--format", "json"], False, check_json),
    ("--limit 1", None, ["--limit", "1"], False, check_limit1),
    ("--timeout 5", None, ["--timeout", "5"], False, check_timeout),
    ("连接名不存在", BOGUS_CONN, [], True, check_bad_conn),
)


def _judge_floor(rc, out, err, expect_reject):
    """两条通用底线；命中即返回 (ok=False, note)，都没命中返回 None。"""
    blob = out + err
    if _TRACEBACK_MARK in blob:
        return False, "输出含 Traceback（无论期望是什么，一律判 FAIL）"
    if expect_reject and rc == 0:
        return False, "期望被拒绝的用例却以 rc=0 成功退出——静默放行比报错更危险"
    return None


def run_baseline(rows: list) -> None:
    for mode, default_conn, env_fn in MODES:
        try:
            env = env_fn()
        except ModeSetupError as exc:
            for name, *_ in BASELINE_CASES:
                rows.append((name, mode, "FAIL", "模式环境没搭好：%s" % exc))
            continue
        for name, conn_override, args, expect_reject, checker in BASELINE_CASES:
            conn = conn_override or default_conn
            rc, out, err, dt = run_lockwait(env, conn, args)
            floor = _judge_floor(rc, out, err, expect_reject)
            if floor is not None:
                ok, note = floor
            else:
                ok, note = checker(rc, out, err, mode)
            rows.append((name, mode, "PASS" if ok else "FAIL", note))
            print("%-14s %-5s %-5s %5.1fs %s"
                 % (name, mode, "PASS" if ok else "FAIL", dt, note[:70]))


def run_chain_case(rows: list) -> None:
    name = "三层堵塞根定位"
    try:
        with chain_e2e.build_three_level_chain(ORCH_CONN) as (sid_a, sid_b, sid_c):
            for mode, conn, env_fn in MODES:
                try:
                    env = env_fn()
                except ModeSetupError as exc:
                    rows.append((name, mode, "FAIL", "模式环境没搭好：%s" % exc))
                    continue
                rc, out, err, dt = run_lockwait(env, conn, [])
                floor = _judge_floor(rc, out, err, expect_reject=False)
                if floor is not None:
                    ok, note = floor
                else:
                    root_a = chain_e2e.root_of(out, sid_a)
                    targets = chain_e2e.kill_target_sessionids(out)
                    if rc != 0:
                        ok, note = False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
                    elif root_a != str(sid_c):
                        ok, note = False, ("A（会话 %s）根算成 %r，期望 C（会话 %s）"
                                          % (sid_a, root_a, sid_c))
                    elif targets != {str(sid_c)}:
                        ok, note = False, ("kill 目标集合是 %r，期望只有 C（会话 %s）"
                                          % (targets, sid_c))
                    else:
                        ok, note = True, ""
                rows.append((name, mode, "PASS" if ok else "FAIL", note))
                print("%-14s %-5s %-5s %5.1fs %s"
                     % (name, mode, "PASS" if ok else "FAIL", dt, note[:70]))
    except chain_e2e.ChainSetupError as exc:
        for mode, *_ in MODES:
            rows.append((name, mode, "FAIL", "链条没搭起来，探测作废：%s" % exc))
    except chain_e2e.ResidueError as exc:
        # 链条本身搭起来了、用例可能已经跑完，但清理阶段发现残留——
        # 这不是「用例失败」，是数据库状态存疑，必须显式报出来而不是吞掉。
        rows.append((name, "cleanup", "FAIL", "清理后仍有残留，需人工核查：%s" % exc))


def main() -> int:
    if not LOCKWAIT_PY.exists():
        print("lockwait.py 不存在于已部署目录：%s\n"
             "先部署：bash install-opencode.sh --dest %s" % (LOCKWAIT_PY, SK),
             file=sys.stderr)
        return 2

    rows: list = []
    run_baseline(rows)
    run_chain_case(rows)

    fails = [r for r in rows if r[2] == "FAIL"]
    print("\n" + "=" * 78)
    print("共 %d 例（%d 用例 × 2 模式），PASS %d，FAIL %d"
         % (len(rows), len(rows) // 2, len(rows) - len(fails), len(fails)))
    for name, mode, verdict, note in fails:
        print("  [FAIL] %s / %s —— %s" % (name, mode, note))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
