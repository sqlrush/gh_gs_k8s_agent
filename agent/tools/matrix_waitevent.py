#!/usr/bin/env python3
"""`gaussdb-waitevent` 的双模式端到端矩阵：同一批用例分别走 `api`
（`-c og-grmp`，中间件）与 `gsql`（`-c og-gsql`，直连）两条真实进程路径各跑
一次，逐例断言退出码与关键内容。结构照抄 `tools/matrix_lockwait.py`。

**为什么两条路径都要跑，不能只信其中一条：** `--timeout` 那条用例特意在
两条路径上断言相反的 stderr 内容——`common/access.py` 里语句超时的处理是
一个已知的、**有意为之**的行为分叉（中间件协议没有这个旋钮），api 模式
该提示"无法设置语句超时"，gsql 模式不该提示。两边断言同一句话会漏掉
这条分叉本身要不要还在。

**本矩阵额外守两件 `gaussdb-waitevent` 特有的事，普通冒烟测试不会管：**

  1. **报告不能暗示已经证伪的包含关系。** `tools/probe_dbtime_containment.py`
     实测过 `CPU_TIME+DATA_IO_TIME+NET_SEND_TIME<=EXECUTION_TIME` 在全部
     窗口都不成立（超出 15%~24%），所以 `waitevent.py` 把 DB time 九项一律
     平铺渲染，不画树——画树会诱导读者拿子项减父项去凑"其余"，在不成立
     的那一半上得到无意义的数字，且没有任何报错提示。默认窗口用例断言
     输出里出现平铺表头「平铺，互不隶属」这个渲染签名。
  2. **跨实例重启的窗口必须报"不可用"，不能算出任何百分比。** 计数器清零
     重来后，后一快照减前一快照会是负数；这类窗口如果照常算比例，得到
     的数字看起来和正常结果一样，却是假的。`tests/test_waitevent_entry_units.py`
     已经用构造数据单测过 markdown 渲染分支；本矩阵尝试在真实快照序列里
     找一个真的跨了重启的窗口，两条路径各跑一次做端到端确认——**找不到
     就如实说找不到，不拿别的用例的通过来冒充这条覆盖**。

**运行本工具前必须先部署**（把仓库当前代码同步到 `GDAA_SKILLS_DIR` 指向
的目录），否则测的是上一次部署时的旧代码：

    ssh <用户>@<测试机> \\
      "cd ~/gh_skill && bash opencode_skill-main-v2-0729/install-opencode.sh \\
         --dest ~/.config/opencode/skills-dev"
    ssh <用户>@<测试机> \\
      "cd /tmp && python3 ~/gh_skill/opencode_skill-main-v2-0729/tools/matrix_waitevent.py"

**部署目标只认 skills-dev，绝不是 skills**：后者是用户在别的工具里正在用
的目录，本工具默认也只指向 skills-dev；换正式目录是运维操作，改
GDAA_SKILLS_DIR 环境变量即可，不改这份代码。

**真实快照 id 一律运行时取，不硬编码**：`wdr.snapshots` 会随时间推进，
写死的 id 迟早指向一个已经不在窗口里的快照，而这类失败不报错、只是
"begin/end 相同"或"查不到行"，很容易被误读成脚本本身的问题。取快照 id
用的是编排连接 `og`（`driver: psycopg2`，直连），与 `matrix_lockwait.py`
搭三层锁链用的是同一条连接、同一个 GSDB_HOME 选取逻辑。

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

# GSDB_HOME 决定 common.config.resolve() 去哪个目录找 config.yaml/凭据——
# 编排连接 "og"（psycopg2，直连）用来在主进程里现取真实快照 id、扫重启窗口。
# 必须在 import common.access 之前定下来。子进程用例各自用独立的 env dict，
# 不受这一行影响。
_GDAA_HOME = os.path.expanduser(os.environ.get("GDAA_HOME_FOR_MATRIX", "~/.gdaa"))
os.environ.setdefault("GSDB_HOME", _GDAA_HOME)

from common import access  # noqa: E402  （编排用：取真实快照 id、扫重启窗口）

# 已安装 skill 的根目录。**这是唯一允许写死的路径**——切正式环境时只改
# 这一个环境变量，不改代码。绝不能默认指向 ~/.config/opencode/skills：
# 那是用户正在用的目录。
SK = os.environ.get("GDAA_SKILLS_DIR",
                    os.path.expanduser("~/.config/opencode/skills-dev"))
WAITEVENT_PY = pathlib.Path(SK) / "gaussdb-waitevent" / "scripts" / "waitevent.py"
PY = sys.executable or "python3"

API_CONN = "og-grmp"
GSQL_CONN = "og-gsql"
ORCH_CONN = "og"          # 编排用：psycopg2，直接查 wdr.snapshots / instance_time
BOGUS_CONN = "no_such_conn_zzz"   # 明确不存在，两种模式下都该被拒

GSQL_HOME = "/tmp/gsql-probe"

RUN_TIMEOUT_S = 60
# 扫多少个最近快照去找一个真的跨了重启的窗口。当前实测环境里快照总数在
# 200 个量级，300 足够覆盖全部，读时间也可控（每对一次轻量聚合查询）。
RESTART_SCAN_LIMIT = 300

_TRACEBACK_MARK = "Traceback (most recent call last)"
_NO_TIMEOUT_NOTICE = "无法设置语句超时"


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


def run_waitevent(env: dict, conn: str, extra_args: list):
    argv = [PY, str(WAITEVENT_PY), "-c", conn] + list(extra_args)
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT_S, env=env)
        return p.returncode, p.stdout, p.stderr, time.time() - t0
    except subprocess.TimeoutExpired:
        return -99, "", "TIMEOUT after %ds" % RUN_TIMEOUT_S, time.time() - t0


# ---------------------------------------------------------------------------
# 编排：现取真实快照 id、扫一个真的跨了重启的窗口 —— 都是只读 SELECT，
# 只碰 snapshot.*，不碰 gsbench_e2e_20260801_100g.plan_data。
# ---------------------------------------------------------------------------

def recent_snapshot_ids(limit: int = RESTART_SCAN_LIMIT) -> list:
    runner = access.for_conn(ORCH_CONN)
    rows = runner.run("wdr.snapshots", {"limit": int(limit)})
    return sorted(int(r["snapshot_id"]) for r in rows)


def _is_negative(raw) -> bool:
    """扫描用的宽松判断：解析不出来就当作"不是负数"，跳过而不是误判为重启。
    真正的重启判定标准在 `dbtime.breakdown()`（十项里任一项 delta_us<0），
    这里只是主进程侧找一个真实样本，不是被测代码。
    """
    try:
        return int(raw) < 0
    except (TypeError, ValueError):
        return False


def find_restart_window(ids: list):
    """在真实快照序列里找一个十项时间模型出现负增量的相邻窗口——一次
    真实的实例重启。找不到就返回 None，调用方必须如实报告覆盖不到，
    不能拿别的用例的通过来冒充这条端到端验证。
    """
    runner = access.for_conn(ORCH_CONN)
    for b, e in zip(ids, ids[1:]):
        rows = runner.run("waitevent.instance_time", {"b": b, "e": e})
        if any(_is_negative(row.get("delta_us")) for row in rows):
            return (b, e)
    return None


# ---------------------------------------------------------------------------
# 逐用例判定：每个 checker 拿到 (rc, out, err, mode) 返回 (ok: bool, note: str)
# ---------------------------------------------------------------------------

def _judge_floor(rc, out, err, expect_reject):
    """两条通用底线；命中即返回 (ok=False, note)，都没命中返回 None。"""
    blob = out + err
    if _TRACEBACK_MARK in blob:
        return False, "输出含 Traceback（无论期望是什么，一律判 FAIL）"
    if expect_reject and rc == 0:
        return False, "期望被拒绝的用例却以 rc=0 成功退出——静默放行比报错更危险"
    return None


# DB time 九项名字，取自 dbtime.py 的 _ITEM_ORDER（`waitevent.py` 的
# `_items_table()` 就是按这个顺序把 `bd.items` 铺成表格行）。本矩阵是黑盒
# 验收，刻意不 import 被测代码的私有常量，独立维护这份拷贝——两处顺序不
# 用一致，这里只要求"九个名字都出现在同一段连续区块里"，不比对次序。
_DB_TIME_ITEM_NAMES = (
    "EXECUTION_TIME", "CPU_TIME", "DATA_IO_TIME", "NET_SEND_TIME",
    "PARSE_TIME", "PLAN_TIME", "REWRITE_TIME",
    "PL_EXECUTION_TIME", "PL_COMPILATION_TIME",
)
_DB_TIME_LINE_PREFIX = "DB_TIME = "         # waitevent.py: "DB_TIME = %d us"
_EVENTS_HEADING_PREFIX = "### 等待事件"      # waitevent.py: _events_block() 的开头


def _check_items_are_flat(out: str):
    """DB time 九项必须落在「DB_TIME = ...」这一行与等待事件小节之间的**同一段
    连续区块**里，区块内不能出现任何标题行（#/##/### ...）——出现标题行说明
    九项被重新分组、加了层级，读者会去对着标题做包含关系的减法，在
    `CPU_TIME+DATA_IO_TIME+NET_SEND_TIME<=EXECUTION_TIME`（已实测不成立）
    那一侧得到无意义的数字，且没有任何报错提示。

    只断言表头文案「平铺，互不隶属」测不出这种回归：换掉表头文案本身、或者
    保留表头但在 CPU/IO/NET 周围插一个小标题，纯文本 substring 匹配一样会
    通过——这正是本检查要单独补上的那一半。
    """
    lines = out.splitlines()
    db_time_idx = next((i for i, l in enumerate(lines)
                        if l.startswith(_DB_TIME_LINE_PREFIX)), None)
    if db_time_idx is None:
        return False, "输出里找不到「%s」这一行，没法定位九项区块" % _DB_TIME_LINE_PREFIX
    events_idx = next((i for i, l in enumerate(lines)
                       if l.startswith(_EVENTS_HEADING_PREFIX)), None)
    if events_idx is None:
        return False, "输出里找不到「%s」，没法定位九项区块的终点" % _EVENTS_HEADING_PREFIX
    if events_idx <= db_time_idx:
        return False, "「%s」出现在「%s」之前，窗口结构不对" % (_EVENTS_HEADING_PREFIX, _DB_TIME_LINE_PREFIX)

    block = lines[db_time_idx:events_idx]
    heading_lines = [l for l in block if l.lstrip().startswith("#")]
    if heading_lines:
        return False, ("DB_TIME 与等待事件小节之间出现了标题行 %r——九项可能被"
                       "重新分组加了层级，不再是一张平铺表" % heading_lines[:3])

    missing = [name for name in _DB_TIME_ITEM_NAMES
              if not any(name in l for l in block)]
    if missing:
        return False, ("DB_TIME 与等待事件之间的区块里缺少 %r——九项不在同一段"
                       "连续区块里" % missing)
    return True, ""


def check_default(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    if "DB_TIME" not in out:
        return False, "输出未包含 DB_TIME"
    # 这是本矩阵要守的第一件事：DB time 九项必须平铺渲染，不能暗示一棵
    # 已被证伪的包含树。表头文案只能测出粗粒度回归（连表头一起被改掉/
    # 删掉的整体改版）；结构检查测的是细粒度回归（表头不变、但在九项
    # 中间插了标题或分组）——两者互补，缺一个都会漏掉对应那一种回归。
    if "平铺，互不隶属" not in out:
        return False, ("输出里没有「平铺，互不隶属」这个渲染签名——"
                       "DB time 九项可能被画成了树，读者会去做无意义的减法")
    ok, note = _check_items_are_flat(out)
    if not ok:
        return False, note
    return True, ""


def check_rc0(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    return True, ""


def check_json(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    try:
        data = json.loads(out)
    except Exception as exc:
        return False, "stdout 不是合法 JSON：%r；前 200 字：%s" % (exc, out[:200])
    if data.get("skill") != "gaussdb-waitevent":
        return False, "skill 字段是 %r，期望 'gaussdb-waitevent'" % data.get("skill")
    for f in data.get("findings", []):
        if not isinstance(f.get("severity"), int):
            return False, "finding %r 的 severity 不是 int：%r" % (f.get("code"), f.get("severity"))
    return True, ""


def check_snapshots1(rc, out, err, mode):
    if rc != 2:
        return False, "rc=%d（期望 2：快照数为 1 应显式报错）：%s" % (rc, (err or out)[:200])
    if "至少需要 2 个快照" not in err:
        return False, "stderr 未包含「至少需要 2 个快照」——空报告比报错更危险"
    return True, ""


def check_timeout_given(rc, out, err, mode):
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


def check_timeout_absent(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    if _NO_TIMEOUT_NOTICE in err:
        return False, "没给 --timeout 却出现了「无法设置语句超时」——两种模式都不该有这条提示"
    return True, ""


def check_bad_conn(rc, out, err, mode):
    if rc != 2:
        return False, "rc=%d（期望 2：连接名不存在应被干净拒绝）：%s" \
                      % (rc, (err or out)[:200])
    return True, ""


def _build_baseline_cases(ids: list):
    """id 依赖运行时取到的真实快照序列——「显式窗口」用例取最近两个。"""
    begin2, end2 = ids[-2], ids[-1]
    # name, conn_override（None 表示用该模式默认连接）, extra_args, expect_reject, checker
    return (
        ("默认窗口", None, [], False, check_default),
        ("指定快照数", None, ["--snapshots", "3"], False, check_rc0),
        ("显式窗口", None, ["--begin", str(begin2), "--end", str(end2)], False, check_rc0),
        ("JSON输出", None, ["--format", "json"], False, check_json),
        ("快照数为1", None, ["--snapshots", "1"], True, check_snapshots1),
        ("显式超时", None, ["--timeout", "5"], False, check_timeout_given),
        ("不给超时", None, [], False, check_timeout_absent),
        ("连接名不存在", BOGUS_CONN, [], True, check_bad_conn),
    )


def run_baseline(rows: list, ids: list) -> None:
    cases = _build_baseline_cases(ids)
    for mode, default_conn, env_fn in MODES:
        try:
            env = env_fn()
        except ModeSetupError as exc:
            for name, *_ in cases:
                rows.append((name, mode, "FAIL", "模式环境没搭好：%s" % exc))
            continue
        for name, conn_override, args, expect_reject, checker in cases:
            conn = conn_override or default_conn
            rc, out, err, dt = run_waitevent(env, conn, args)
            floor = _judge_floor(rc, out, err, expect_reject)
            if floor is not None:
                ok, note = floor
            else:
                ok, note = checker(rc, out, err, mode)
            rows.append((name, mode, "PASS" if ok else "FAIL", note))
            print("%-14s %-5s %-5s %5.1fs %s"
                 % (name, mode, "PASS" if ok else "FAIL", dt, note[:70]))


def run_cross_consistency(rows: list) -> None:
    """默认参数下，两条路径查的是同一份快照数据——findings 的 code 集合
    应该一致（数值允许不同：两条访问路径走的查询逻辑一样，但取数时刻不同
    实例统计可能有细微漂移）。用默认参数而不是显式 --begin/--end，是
    因为这条用例本身就是在验证"不特意对齐窗口时，两条路径是否仍然一致"；
    两次子进程调用间隔通常只有几秒，实测快照按小时级节奏产生，撞上新快照
    落地的概率可以忽略。
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
        rc, out, err, dt = run_waitevent(env, default_conn, ["--format", "json"])
        floor = _judge_floor(rc, out, err, expect_reject=False)
        if floor is not None:
            ok, note = floor
            rows.append((name, mode, "FAIL", note))
            print("%-14s %-5s %-5s %5.1fs %s" % (name, mode, "FAIL", dt, note[:70]))
            continue
        try:
            data = json.loads(out)
            codes = {f["code"] for f in data.get("findings", [])}
            codes_by_mode[mode] = codes
            ok, note = True, "codes=%r" % sorted(codes)
        except Exception as exc:
            ok, note = False, "stdout 不是合法 JSON：%r" % exc
        rows.append((name, mode, "PASS" if ok else "FAIL", note))
        print("%-14s %-5s %-5s %5.1fs %s"
             % (name, mode, "PASS" if ok else "FAIL", dt, note[:70]))

    if len(codes_by_mode) == 2:
        api_codes, gsql_codes = codes_by_mode["api"], codes_by_mode["gsql"]
        ok = (api_codes == gsql_codes)
        note = "" if ok else ("api findings code 集合=%r 与 gsql 的=%r 不一致"
                              % (sorted(api_codes), sorted(gsql_codes)))
        rows.append((name, "cross", "PASS" if ok else "FAIL", note))
        print("%-14s %-5s %-5s %5s  %s"
             % (name, "cross", "PASS" if ok else "FAIL", "-", note[:70]))
    else:
        rows.append((name, "cross", "FAIL", "至少一个模式没能取到 findings，无法比较"))
        print("  [FAIL] %s / cross —— 至少一个模式没能取到 findings，无法比较" % name)


def run_restart_case(rows: list) -> None:
    """本矩阵要守的第二件事：跨实例重启的窗口必须报"不可用"，不能算出
    任何百分比。先在真实快照序列里找一个真的跨了重启的窗口（十项时间
    模型里任一项后减前算出负数），找到了才两条路径各跑一次做端到端确认；
    找不到就显式记一行 SKIP 并说明原因，不拿别的用例的通过来冒充这条覆盖——
    `tests/test_waitevent_entry_units.py::test_restarted_window_markdown_shows_
    unavailable_not_percentages` 已经用构造数据单测过同一段渲染代码。
    """
    name = "重启窗口端到端"
    try:
        ids = recent_snapshot_ids(limit=RESTART_SCAN_LIMIT)
    except Exception as exc:
        note = "取快照序列失败，无法搜索重启窗口：%s" % exc
        rows.append((name, "n/a", "SKIP", note))
        print("  [SKIP] %s —— %s" % (name, note))
        return

    try:
        window = find_restart_window(ids)
    except Exception as exc:
        note = "搜索重启窗口时查询失败：%s" % exc
        rows.append((name, "n/a", "SKIP", note))
        print("  [SKIP] %s —— %s" % (name, note))
        return

    if window is None:
        note = ("在最近 %d 个真实快照（id %d..%d）里没有找到十项时间模型出现"
                "负增量的相邻窗口——当前数据里没有可用的重启窗口，本矩阵这条"
                "端到端验证跳过，不代表重启分支缺自动化覆盖（见单测 "
                "test_restarted_window_markdown_shows_unavailable_not_percentages）。"
                % (len(ids), ids[0], ids[-1]))
        rows.append((name, "n/a", "SKIP", note))
        print("  [SKIP] %s —— %s" % (name, note[:120]))
        return

    b, e = window
    args = ["--begin", str(b), "--end", str(e)]
    for mode, default_conn, env_fn in MODES:
        try:
            env = env_fn()
        except ModeSetupError as exc:
            rows.append((name, mode, "FAIL", "模式环境没搭好：%s" % exc))
            print("  [FAIL] %s / %s —— %s" % (name, mode, exc))
            continue
        rc, out, err, dt = run_waitevent(env, default_conn, args)
        floor = _judge_floor(rc, out, err, expect_reject=False)
        if floor is not None:
            ok, note = floor
        elif rc != 0:
            ok, note = False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
        elif "该窗口跨越了实例重启，数据不可用" not in out:
            ok, note = False, "输出未包含「该窗口跨越了实例重启，数据不可用」"
        elif "%" in out:
            ok, note = False, "重启窗口的输出里出现了 %——算出来的比例是假的，不该展示"
        else:
            ok, note = True, "窗口 %d→%d" % (b, e)
        rows.append((name, mode, "PASS" if ok else "FAIL", note))
        print("%-14s %-5s %-5s %5.1fs %s"
             % (name, mode, "PASS" if ok else "FAIL", dt, note[:70]))


def main() -> int:
    if not WAITEVENT_PY.exists():
        print("waitevent.py 不存在于已部署目录：%s\n"
             "先部署：bash install-opencode.sh --dest %s" % (WAITEVENT_PY, SK),
             file=sys.stderr)
        return 2

    try:
        ids = recent_snapshot_ids(limit=RESTART_SCAN_LIMIT)
    except Exception as exc:
        print("取真实快照 id 失败，矩阵无法构造用例：%s" % exc, file=sys.stderr)
        return 2
    if len(ids) < 2:
        print("wdr.snapshots 里快照不足 2 个，矩阵无法运行", file=sys.stderr)
        return 2

    rows: list = []
    run_baseline(rows, ids)
    run_cross_consistency(rows)
    run_restart_case(rows)

    fails = [r for r in rows if r[2] == "FAIL"]
    skips = [r for r in rows if r[2] == "SKIP"]
    passes = [r for r in rows if r[2] == "PASS"]
    print("\n" + "=" * 78)
    print("共 %d 行，PASS %d，FAIL %d，SKIP %d"
         % (len(rows), len(passes), len(fails), len(skips)))
    for name, mode, verdict, note in fails:
        print("  [FAIL] %s / %s —— %s" % (name, mode, note))
    for name, mode, verdict, note in skips:
        print("  [SKIP] %s / %s —— %s" % (name, mode, note))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
