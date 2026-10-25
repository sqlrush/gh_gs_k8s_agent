#!/usr/bin/env python3
"""`gaussdb-vacuum` 的双模式端到端矩阵：同一批用例分别走 `api`
（`-c og-grmp`，中间件）与 `gsql`（`-c og-gsql`，直连）两条真实进程路径各跑
一次，逐例断言退出码与关键内容。结构照抄 `tools/matrix_lockwait.py` /
`tools/matrix_waitevent.py`。

**为什么两条路径都要跑，不能只信其中一条：** `--timeout` 那条用例特意在
两条路径上断言相反的 stderr 内容——`common/access.py` 里语句超时的处理是
一个已知的、**有意为之**的行为分叉（中间件协议没有这个旋钮），api 模式
该提示"无法设置语句超时"，gsql 模式不该提示。两边断言同一句话会漏掉
这条分叉本身要不要还在。

**本矩阵额外守五件 `gaussdb-vacuum` 特有的事，普通冒烟测试不会管：**

  1. **`plan_data` 必须出现在风险表里，且带着命中的规则码。**
     `gsbench_e2e_20260801_100g.plan_data` 是这一阶段唯一现实的高死元组
     夹具（约 2000 万死元组，从没被 autovacuum 服务过），是本矩阵能验证
     "真数据触发真规则"的唯一现场；这一行以及 `--format json` 里对应的
     finding 都必须出现。
  2. **报告里绝不能出现空间回收量的预估措辞。** 与
     `tests/test_vacuum_entry_units.py` 的
     `test_report_never_estimates_reclaimable_space` 是同一条不变量，
     这里对**真实取数路径**（而不是假 runner）再做一次端到端确认。
  3. **「回收阻塞源」小节必须出现在输出里。** 这是 R4 gate 之外单独补上的
     报告要求——`vacuum.oldest_xmin` 有没有行，不看任何表有没有命中 R4；
     这里只断言小节标题出现（真实实例当前是否有长事务/复制槽会随时间
     变化，不作为矩阵的强约束条件，见 `tests/test_vacuum_entry_units.py`
     里用假 runner 钉死的无条件行为）。
  4. **两条路径查的是同一份数据库状态，findings 的 code 集合应该一致。**
     这条曾经有过一个已确认、可 100% 复现的例外：`scripts/registry/vacuum/
     oldest_xmin.yaml` 的 `long_xact` 分支原先只判断 `connection_info <>
     ''`，而 gsql/libpq 直连驱动会给**自己这个 session** 填上
     `connection_info = {"driver_name":"libpq",...}`，于是这条 SELECT
     在 gsql 模式下**永远会把执行查询的这个会话自己**当成一条"活跃事务"
     命中——`pid` 精确等于 `pg_backend_pid()`，`xmin_age_s` 恒为 0；
     psycopg2（`-c og` 用的驱动）对自己这一行的 `connection_info` 是空串，
     不会自证。已用 `common.access.connection_for()` 直接查证过两条路径的
     `pg_stat_activity` 自身行，见 task-17-report.md「Fix round 1」一节。
     `oldest_xmin.yaml` 的 `long_xact` 分支现已补上
     `AND pid <> pg_backend_pid()`，这条例外已撤销，本矩阵恢复严格相等；
     用例 5（下面第 5 点）专门守住这个修复不被悄悄撤销。
  5. **gsql 模式绝不能把执行查询本身的会话当成阻塞源上报。** 这正是第 4
     点里那个 bug 的直接、独立守护——即便哪天 `run_cross_consistency` 因为
     别的原因被改弱，这条用例依然单独盯着这一件事。见
     `run_self_blocker_case()` 的判据，以及函数 docstring 里留痕的第一版
     失败教训：**判据不能是"查报出来的 pid 现在还活不活"。**
     `DirectRunner.run()`（`common/grmp/runner.py`）每次开连接、执行完
     立刻关闭，自证幽灵产生它的那条连接在报告打印出来那一刻已经关闭，
     "查不到就是幽灵"听起来是个干净的判据——但实测直接翻车：本实例的
     pid/线程号池很小、回收很快，矩阵自己在同一次运行里已经连了十几条
     连接，故意撤掉守护重跑一遍后，这条用例反而给出了假阳性的
     PASS——那个已关闭的幽灵 pid，检查那一刻已被矩阵自己后续某条连接
     复用。现在的判据改成**内容签名**：自证幽灵这一行的 `detail` 带着
     `pg_stat_activity.query` 的原始文本，而那正是 `oldest_xmin.yaml`
     自己那条 SQL，`detail` 里必然原样出现它的起始片段
     `'long_xact' AS source`——这个子串只可能来自这一处，不受任何时序或
     pid 复用影响。

**运行本工具前必须先部署**（把仓库当前代码同步到 `GDAA_SKILLS_DIR` 指向
的目录），否则测的是上一次部署时的旧代码：

    ssh <用户>@<测试机> \\
      "cd ~/gh_skill && bash opencode_skill-main-v2-0729/install-opencode.sh \\
         --dest ~/.config/opencode/skills-dev"
    ssh <用户>@<测试机> \\
      "cd /tmp && python3 ~/gh_skill/opencode_skill-main-v2-0729/tools/matrix_vacuum.py"

**部署目标只认 skills-dev，绝不是 skills**：后者是用户在别的工具里正在用
的目录，本工具默认也只指向 skills-dev；换正式目录是运维操作，改
GDAA_SKILLS_DIR 环境变量即可，不改这份代码。

**数据库安全：本矩阵只读。** `vacuum.py` 本身评估、不执行任何
VACUUM/ANALYZE；本矩阵同样绝不对 `plan_data` 或任何其他表执行
VACUUM/VACUUM FULL/ANALYZE，也不调用任何 `pg_cancel_*`/`pg_terminate_*`。

**判定的两条底线（对每一行都成立，不分哪条用例）：**

    1. 输出（stdout+stderr）里出现 Traceback 一律判 FAIL，无论期望是什么——
       未捕获异常意味着这条路径没人设计过。
    2. 期望被拒的用例（rc 应为非 0）如果退出码是 0，一律判 FAIL——
       静默放行比明确报错更危险。
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

# GSDB_HOME 决定 common.config.resolve() 去哪个目录找 config.yaml/凭据。
# 本矩阵不需要在主进程里建真实连接（不像 matrix_lockwait 要编排三层锁链、
# matrix_waitevent 要现取快照 id）——vacuum 只读，子进程各自用独立的 env
# dict 建连接，「gsql 不自证阻塞源」用例判定用的是报告文本里的内容签名
# （见 `_SELF_REFERENCE_SIGNATURE`），同样不需要主进程另开真实连接。
_GDAA_HOME = os.path.expanduser(os.environ.get("GDAA_HOME_FOR_MATRIX", "~/.gdaa"))
os.environ.setdefault("GSDB_HOME", _GDAA_HOME)

# 已安装 skill 的根目录。**这是唯一允许写死的路径**——切正式环境时只改
# 这一个环境变量，不改代码。绝不能默认指向 ~/.config/opencode/skills：
# 那是用户正在用的目录，本工具跑起来会产生大量真实连接，不该碰用户正在
# 用的安装。
SK = os.environ.get("GDAA_SKILLS_DIR",
                    os.path.expanduser("~/.config/opencode/skills-dev"))
VACUUM_PY = pathlib.Path(SK) / "gaussdb-vacuum" / "scripts" / "vacuum.py"
PY = sys.executable or "python3"

API_CONN = "og-grmp"
GSQL_CONN = "og-gsql"
BOGUS_CONN = "zz-vacuum-matrix-nope"   # 明确不存在，两种模式下都该被拒

GSQL_HOME = "/tmp/gsql-probe"

RUN_TIMEOUT_S = 60

_TRACEBACK_MARK = "Traceback (most recent call last)"
_NO_TIMEOUT_NOTICE = "无法设置语句超时"

# 与 tests/test_vacuum_entry_units.py 的 _FORBIDDEN_RECLAIM_PHRASES 保持
# 同一份清单——两处各自独立维护，一处漏改不会让另一处也跟着失效。
_FORBIDDEN_RECLAIM_PHRASES = ("可回收", "预计释放", "预计可回收", "可释放")

# 这一阶段唯一现实的高死元组夹具：约 2000 万死元组，从未被 autovacuum
# 服务过。绝不对它执行 VACUUM/VACUUM FULL/ANALYZE——见文件头注释。
PLAN_DATA_TABLE = "plan_data"


class ModeSetupError(RuntimeError):
    """某个访问模式的运行环境本身有问题（凭据/令牌缺失等），矩阵不跑。"""


def _grmp_token() -> str:
    """从 `~/.gdaa/grmp.env` 里取 `GRMP_AUTH_TOKEN`。

    该文件是给 shell `source` 用的，子进程的 env 是一个 dict，没有 shell
    语义可以真的执行 source——这里改用正则直接抽取值。只认这一个变量，
    不打印、不落进任何输出。
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


def run_vacuum(env: dict, conn: str, extra_args: list):
    argv = [PY, str(VACUUM_PY), "-c", conn] + list(extra_args)
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

def _judge_floor(rc, out, err, expect_reject):
    """三条通用底线；命中即返回 (ok=False, note)，都没命中返回 None。"""
    blob = out + err
    if _TRACEBACK_MARK in blob:
        return False, "输出含 Traceback（无论期望是什么，一律判 FAIL）"
    if expect_reject and rc == 0:
        return False, "期望被拒绝的用例却以 rc=0 成功退出——静默放行比报错更危险"
    for phrase in _FORBIDDEN_RECLAIM_PHRASES:
        if phrase in out:
            return False, "输出里出现了空间回收量预估措辞 %r——本 skill 绝不给这个数字" % phrase
    return None


def check_default(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    if PLAN_DATA_TABLE not in out:
        return False, ("输出未包含 %r —— 已知的高死元组测试表没有出现在"
                       "风险表里" % PLAN_DATA_TABLE)
    if "命中规则" not in out:
        return False, "输出未包含「命中规则」列——风险表结构可能变了"
    if not any(code in out for code in ("R1", "R2", "R3", "R4")):
        return False, "输出里没有任何规则码（R1~R4）——plan_data 应该至少命中一条"
    if "回收阻塞源" not in out:
        return False, "输出里没有「回收阻塞源」小节——xmin 汇报可能被去掉了"
    return True, ""


def check_json(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    try:
        data = json.loads(out)
    except Exception as exc:
        return False, "stdout 不是合法 JSON：%r；前 200 字：%s" % (exc, out[:200])
    if data.get("skill") != "gaussdb-vacuum":
        return False, "skill 字段是 %r，期望 'gaussdb-vacuum'" % data.get("skill")
    findings = data.get("findings", [])
    if not findings:
        return False, "findings 是空的——plan_data 应该至少命中一条规则"
    for f in findings:
        if not isinstance(f.get("severity"), int):
            return False, "finding %r 的 severity 不是 int：%r" % (f.get("code"), f.get("severity"))
    codes = {f["code"] for f in findings}
    if not ({"VACUUM_OVERDUE", "VACUUM_DEAD_RATIO"} & codes):
        return False, ("plan_data 应该至少命中 R1(VACUUM_OVERDUE) 或 "
                       "R3(VACUUM_DEAD_RATIO) 之一，实际 codes=%r" % sorted(codes))
    return True, ""


def _count_risk_rows(out: str):
    """数「## 风险表」小节里的数据行数。返回 -1 表示连小节标题都找不到——
    调用方要把这个和「小节存在、但表头/数据行结构变了」区分开。

    这条解析存在的理由：`check_limit1` 原先只断言 `rc == 0`，`--limit`
    整个被忽略也照样 PASS——这正是这个计划里反复出现的「断言看不见它要
    盯的那个回归」模式。数据行数才是 `--limit` 唯一能验证到的东西：
    `--limit N` 传给的是 `vacuum.dead_tuples` 的 SQL LIMIT，风险表的行数
    是这批被 LIMIT 截断的原始行的子集（只保留命中规则的），所以
    「风险表行数 <= limit」在任何命中情况下都成立，不需要真数据恰好命中
    规则才能验证到。
    """
    lines = out.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## 风险表")
    except StopIteration:
        return -1
    header_idx = next(
        (i for i in range(start, len(lines))
         if lines[i].startswith("|") and "命中规则" in lines[i]), None)
    if header_idx is None:
        # 没有表头——是「未发现死元组风险表」分支，数据行数为 0。
        return 0
    count = 0
    for i in range(header_idx + 2, len(lines)):   # +2：跳过表头行与分隔行
        if lines[i].startswith("|"):
            count += 1
        else:
            break
    return count


def check_limit1(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    n = _count_risk_rows(out)
    if n < 0:
        return False, "输出里找不到「## 风险表」小节，无法核对 --limit 1 是否生效"
    if n > 1:
        return False, ("--limit 1 之后风险表仍有 %d 行——--limit 可能没有真正"
                       "传到底层查询" % n)
    return True, ""


def check_timeout(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    has_notice = _NO_TIMEOUT_NOTICE in err
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
    ("默认输出（含 plan_data）", None, [], False, check_default),
    ("--format json", None, ["--format", "json"], False, check_json),
    ("--limit 1", None, ["--limit", "1"], False, check_limit1),
    ("--timeout 5", None, ["--timeout", "5"], False, check_timeout),
    ("连接名不存在", BOGUS_CONN, [], True, check_bad_conn),
)


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
            rc, out, err, dt = run_vacuum(env, conn, args)
            floor = _judge_floor(rc, out, err, expect_reject)
            if floor is not None:
                ok, note = floor
            else:
                ok, note = checker(rc, out, err, mode)
            rows.append((name, mode, "PASS" if ok else "FAIL", note))
            print("%-24s %-5s %-5s %5.1fs %s"
                 % (name, mode, "PASS" if ok else "FAIL", dt, note[:70]))


def run_cross_consistency(rows: list) -> None:
    """默认参数下，两条路径查的是同一份实例状态——findings 的 code 集合
    应该一致（数值允许不同：两条访问路径走的查询逻辑一样，但取数时刻不同，
    实例统计可能有细微漂移）。抄 matrix_waitevent.py 的 run_cross_consistency，
    同样的理由：两次子进程调用间隔通常只有几秒，plan_data 的死元组量级
    （约 2000 万）不会在这几秒内漂移到"不再命中同一批规则"的地步。
    """
    name = "跨模式一致"
    codes_by_mode = {}
    for mode, default_conn, env_fn in MODES:
        try:
            env = env_fn()
        except ModeSetupError as exc:
            rows.append((name, mode, "FAIL", "模式环境没搭好：%s" % exc))
            print("  [FAIL] %s / %s —— %s" % (name, mode, exc))
            continue
        rc, out, err, dt = run_vacuum(env, default_conn, ["--format", "json"])
        floor = _judge_floor(rc, out, err, expect_reject=False)
        if floor is not None:
            ok, note = floor
            rows.append((name, mode, "FAIL", note))
            print("%-24s %-5s %-5s %5.1fs %s" % (name, mode, "FAIL", dt, note[:70]))
            continue
        try:
            data = json.loads(out)
            codes = {f["code"] for f in data.get("findings", [])}
            codes_by_mode[mode] = codes
            ok, note = True, "codes=%r" % sorted(codes)
        except Exception as exc:
            ok, note = False, "stdout 不是合法 JSON：%r" % exc
        rows.append((name, mode, "PASS" if ok else "FAIL", note))
        print("%-24s %-5s %-5s %5.1fs %s"
             % (name, mode, "PASS" if ok else "FAIL", dt, note[:70]))

    if len(codes_by_mode) == 2:
        # 曾经有一个已确认的例外（VACUUM_XMIN_BLOCKED，gsql/libpq 自证
        # bug，见文件头注释第 4 点）——oldest_xmin.yaml 的 long_xact 分支
        # 已经补上 AND pid <> pg_backend_pid()，这个例外已撤销：现在两条
        # 路径的 code 集合要求严格相等，不再排除任何 code。哪天这个 bug
        # 被悄悄带回来，这里会重新变红（用例 5「gsql 不自证阻塞源」是更
        # 直接的守护，这里是第二道）。
        api_codes, gsql_codes = codes_by_mode["api"], codes_by_mode["gsql"]
        ok = (api_codes == gsql_codes)
        note = "" if ok else ("api findings code 集合=%r 与 gsql 的=%r 不一致"
                              % (sorted(api_codes), sorted(gsql_codes)))
        rows.append((name, "cross", "PASS" if ok else "FAIL", note))
        print("%-24s %-5s %-5s %5s  %s"
             % (name, "cross", "PASS" if ok else "FAIL", "-", note[:70]))
    else:
        rows.append((name, "cross", "FAIL", "至少一个模式没能取到 findings，无法比较"))
        print("  [FAIL] %s / cross —— 至少一个模式没能取到 findings，无法比较" % name)


_LONG_XACT_PID_RE = re.compile(r"长事务（标识 (\d+)）")


def _extract_long_xact_pids(out: str) -> list:
    """从「回收阻塞源」小节里挑出「长事务」条目的标识（pid），只用于报错
    消息里报出具体是哪个标识——不用于判定本身（判定见下）。见 vacuum.py
    的 `_XMIN_SOURCE_LABELS`：`source='long_xact'` 渲染成
    「长事务（标识 <pid>）：...」。只认这一种来源——prepared_xact/
    replication_slot 两支没有 pid 列，不可能自证（见
    scripts/registry/vacuum/oldest_xmin.yaml 头部注释，已逐列核对过
    information_schema）。
    """
    return _LONG_XACT_PID_RE.findall(out)


# 自证幽灵的确定性签名。修复前，gsql/libpq 驱动会把执行 oldest_xmin 这条
# SELECT 本身的会话当成一条 long_xact 阻塞源报出来；那一行的 detail 带着
# pg_stat_activity.query 的原始文本，而那个会话当时正在执行的，就是
# oldest_xmin.yaml 自己那条 SQL——所以 detail 里会原样出现这条 SQL 的
# 起始片段 `'long_xact' AS source`。已实测核实（见
# task-17-report.md「Fix round 1」）：gsql 模式下故意撤掉
# `AND pid <> pg_backend_pid()` 之后，报出来的那一行 detail 就是
# `query=SELECT json_agg(...) FROM (SELECT 'long_xact' AS source, ...`。
_SELF_REFERENCE_SIGNATURE = "'long_xact' AS source"


def run_self_blocker_case(rows: list) -> None:
    """Finding 1 的专门守护：gsql 模式绝不能把执行查询本身的会话当成阻塞源
    上报。

    **第一版实现（已废弃，留痕说明为什么不能用）：** 曾经尝试"查报出来的
    pid 现在还活不活"——`DirectRunner.run()` 每次开连接、执行完立刻
    `close()`（见 `common/grmp/runner.py`），自证幽灵产生它的那条连接在
    报告打印出来的那一刻已经关闭，看起来是个干净的判据。实测直接翻车：
    这个环境的 pid/线程号池很小、回收很快，矩阵自己在同一次运行里已经
    连了十几条连接，故意撤掉 SQL 里的守护重新跑一遍后，"gsql 不自证
    阻塞源"这条用例反而给出了假阳性的 PASS——报出来的那个已经关闭的
    幽灵 pid，在检查那一刻已经被矩阵自己后续某条连接复用，"pid 还活着"
    这个判据本身就不成立。「跨模式一致」那条用例（靠比较两条路径的
    finding code 集合）在同一次实测里正确地报了 FAIL，证明问题确实
    在这条用例的判据上，不是随机抖动。

    **现在的判据：内容签名，不依赖任何时序。** 自证幽灵这一行的 detail
    带着 `pg_stat_activity.query` 的原始文本，而那个会话当时正在执行的
    就是 `oldest_xmin.yaml` 自己那条 SQL——`detail` 里必然原样出现这条
    SQL 的起始片段 `'long_xact' AS source`（`_SELF_REFERENCE_SIGNATURE`，
    已用故意撤掉守护后的真实输出核实过，见 task-17-report.md）。这个
    子串只可能来自这一处，不可能是巧合，也不受 pid 复用影响。
    """
    name = "gsql 不自证阻塞源"
    try:
        env = _gsql_env()
    except ModeSetupError as exc:
        rows.append((name, "gsql", "FAIL", "模式环境没搭好：%s" % exc))
        print("  [FAIL] %s / gsql —— %s" % (name, exc))
        return
    rc, out, err, dt = run_vacuum(env, GSQL_CONN, [])
    floor = _judge_floor(rc, out, err, expect_reject=False)
    if floor is not None:
        ok, note = floor
    elif rc != 0:
        ok, note = False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    elif _SELF_REFERENCE_SIGNATURE in out:
        pids = _extract_long_xact_pids(out) or ["?"]
        ok, note = False, (
            "长事务阻塞源（标识 %s）的 detail 里出现了 %r——这段 SQL 片段"
            "只可能来自 oldest_xmin.yaml 自己那条查询，说明这一行是执行"
            "查询本身的会话把自己当成了阻塞源上报（Finding 1 的 bug，见"
            "oldest_xmin.yaml 的 long_xact 分支）"
            % (pids, _SELF_REFERENCE_SIGNATURE))
    else:
        pids = _extract_long_xact_pids(out)
        ok, note = True, (
            "本次没有长事务类阻塞源（当前 DB 状态下这是预期结果）" if not pids
            else "报告了 %d 个长事务，detail 里没有自证签名" % len(pids))
    rows.append((name, "gsql", "PASS" if ok else "FAIL", note))
    print("%-24s %-5s %-5s %5.1fs %s"
         % (name, "gsql", "PASS" if ok else "FAIL", dt, note[:70]))


def main() -> int:
    if not VACUUM_PY.exists():
        print("vacuum.py 不存在于已部署目录：%s\n"
             "先部署：bash install-opencode.sh --dest %s" % (VACUUM_PY, SK),
             file=sys.stderr)
        return 2

    rows: list = []
    run_baseline(rows)
    run_cross_consistency(rows)
    run_self_blocker_case(rows)

    fails = [r for r in rows if r[2] == "FAIL"]
    print("\n" + "=" * 78)
    print("共 %d 例（%d 用例 × 2 模式 + 1 跨模式核对 + 1 gsql 自证核对），"
         "PASS %d，FAIL %d"
         % (len(rows), len(BASELINE_CASES), len(rows) - len(fails), len(fails)))
    for name, mode, verdict, note in fails:
        print("  [FAIL] %s / %s —— %s" % (name, mode, note))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
