#!/usr/bin/env python3
"""`gaussdb-health` 汇总层的双模式端到端矩阵：同一批用例分别走 `api`
（`-c og-grmp`，中间件）与 `gsql`（`-c og-gsql`，直连）两条真实进程路径各跑
一次，逐例断言退出码与关键内容。结构照抄 `tools/matrix_vacuum.py` /
`tools/matrix_lockwait.py`。

**本矩阵守的是 Task 18–20 的汇总层承诺，普通冒烟测试不会管：**

  1. **正常汇总：报告顶部两段固定小节必须出现，不依赖是否命中风险。**
     「本次未采集到的维度」在全部成功时必须明说"全部采集成功"并点齐三个
     子 skill 的名字；「未纳入汇总的能力」必须无条件列出 5 个需要指定
     SQL/对象的 skill——这两段的存在本身就是断言对象，一份"干净得什么都
     没提"的报告在这里判 FAIL。
  2. **子 skill 的 finding 必须带来源指针。** `gsbench_e2e_20260801_100g.
     plan_data`（约 2000 万死元组、从未被 autovacuum 服务过）是稳定的活体
     夹具，`gaussdb-vacuum` 必然命中——所以「（详见 gaussdb-vacuum）」必须
     出现在 Deterministic Findings 里。lockwait 有没有 finding 取决于当时
     有没有真实锁堵塞，**不作为断言条件**（没有堵塞的干净结果是合法的）。
  3. **子 skill 不可用时：rc=3，但报告必须照常完整打印。** 把已部署目录里
     lockwait 的脚本临时改名再跑——顶部小节点名它和原因、Collection Notes
     出现降级行、其余两个子 skill 的 findings 不受影响（vacuum 的来源指针
     仍在）、报告的固定结构一节不少。rc=3 是附加信息不是替代输出，这条
     用例两头都盯：退出码对、报告也全。跑完无论成败都还原改名（finally）。
  4. **`--include locks` 的 scope 语义：报告只纳入 lockwait。** 注意这里
     断言的是 scope（`sub_skills` 只列 gaussdb-lockwait、findings 全部来自
     它、本地维度为空），**不是子进程数**——`aggregate.collect_all()` 目前
     三个都跑、结果在渲染前过滤，这是 Task 19 §12.4 实测确认过正确性、
     协调者裁决延后的性能优化项，本矩阵不把它当 bug 报。

**运行本工具前必须先部署**（把仓库当前代码同步到 `GDAA_SKILLS_DIR` 指向
的目录），否则测的是上一次部署时的旧代码：

    ssh <用户>@<测试机> \\
      "cd ~/gh_skill && bash opencode_skill-main-v2-0729/install-opencode.sh \\
         --dest ~/.config/opencode/skills-dev"
    ssh <用户>@<测试机> \\
      "cd /tmp && python3 ~/gh_skill/opencode_skill-main-v2-0729/tools/matrix_health_aggregate.py"

**部署目标只认 skills-dev，绝不是 skills**：后者是用户在别的工具里正在用
的目录。用例 3 会临时改名已部署目录里的脚本——绝不能对用户正在用的安装
做这件事，这也是"只认 skills-dev"在本矩阵里比其他矩阵更硬的原因。

**数据库安全：本矩阵只读。** health 及其三个子 skill 全部只读；本矩阵不
执行任何 VACUUM/ANALYZE/kill，也不制造锁堵塞。

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

_GDAA_HOME = os.path.expanduser(os.environ.get("GDAA_HOME_FOR_MATRIX", "~/.gdaa"))
os.environ.setdefault("GSDB_HOME", _GDAA_HOME)

# 已安装 skill 的根目录。**这是唯一允许写死的路径**——切正式环境时只改
# 这一个环境变量，不改代码。绝不能默认指向 ~/.config/opencode/skills：
# 用例 3 要临时改名这个目录里的脚本，碰用户正在用的安装是不可接受的。
SK = os.environ.get("GDAA_SKILLS_DIR",
                    os.path.expanduser("~/.config/opencode/skills-dev"))
HEALTH_PY = pathlib.Path(SK) / "gaussdb-health" / "scripts" / "health.py"
LOCKWAIT_PY = pathlib.Path(SK) / "gaussdb-lockwait" / "scripts" / "lockwait.py"
PY = sys.executable or "python3"

API_CONN = "og-grmp"
GSQL_CONN = "og-gsql"
BOGUS_CONN = "zz-health-matrix-nope"   # 明确不存在，两种模式下都该被拒

GSQL_HOME = "/tmp/gsql-probe"

# health 自己 8 个本地维度 + 顺序跑 3 个子 skill 子进程，比单 skill 矩阵慢
# 一个量级；api 模式每条查询还要过一趟中间件。取值有依据：aggregate.py 给
# 每个子进程的上限是 DEFAULT_SKILL_TIMEOUT_SECONDS(30) + 15 = 45s，三个顺序
# 跑最坏 135s，再留出本地 8 个维度的时间——低于这个数会把"子 skill 慢"误报
# 成"矩阵超时"。
RUN_TIMEOUT_S = 240

_TRACEBACK_MARK = "Traceback (most recent call last)"

# 与 aggregate.SUB_SKILLS / aggregate.NEEDS_TARGET 保持同一份清单——特意
# 在这里独立复制一份、不 import：矩阵的期望值如果跟着实现走，实现里少了
# 一项，期望也会跟着少一项，矩阵就看不见这条回归了。
SUB_SKILLS = ("gaussdb-lockwait", "gaussdb-waitevent", "gaussdb-vacuum")
NEEDS_TARGET = ("gaussdb-explain", "gaussdb-sqltune", "gaussdb-sqlreview",
                "gaussdb-sqlfetch", "gaussdb-proctune")

# 稳定的活体夹具：plan_data 约 2000 万死元组，gaussdb-vacuum 必然命中，
# 它的 finding 混进 health 主表后必须带这个来源指针。
_VACUUM_SOURCE_MARK = "（详见 gaussdb-vacuum）"


class ModeSetupError(RuntimeError):
    """某个访问模式的运行环境本身有问题（凭据/令牌缺失等），矩阵不跑。"""


def _grmp_token() -> str:
    """从 `~/.gdaa/grmp.env` 里取 `GRMP_AUTH_TOKEN`。只认这一个变量，
    不打印、不落进任何输出。"""
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


def run_health(env: dict, conn: str, extra_args: list):
    argv = [PY, str(HEALTH_PY), "-c", conn] + list(extra_args)
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT_S, env=env)
        return p.returncode, p.stdout, p.stderr, time.time() - t0
    except subprocess.TimeoutExpired:
        return -99, "", "TIMEOUT after %ds" % RUN_TIMEOUT_S, time.time() - t0


def _section(out: str, title: str) -> str:
    """取 markdown 输出里 `## <title>` 到下一个 `## ` 之间的内容；找不到
    小节返回空串——调用方对空串断言失败时能直接看出是"小节整个没了"。"""
    lines = out.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## " + title)
    except StopIteration:
        return ""
    body = []
    for l in lines[start + 1:]:
        if l.startswith("## "):
            break
        body.append(l)
    return "\n".join(body)


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


def check_default(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    missing = _section(out, "本次未采集到的维度")
    if not missing:
        return False, "报告里没有「## 本次未采集到的维度」小节——固定结构缺了"
    if "全部采集成功" not in missing:
        return False, "三个子 skill 都在的情况下，「本次未采集到的维度」没有写明" \
                      "全部采集成功：%r" % missing[:200]
    for skill in SUB_SKILLS:
        if skill not in missing:
            return False, "「本次未采集到的维度」的成功文案没有点到 %s——" \
                          "读者无法知道这次到底纳入了谁" % skill
    uncovered = _section(out, "未纳入汇总的能力")
    if not uncovered:
        return False, "报告里没有「## 未纳入汇总的能力」小节——固定结构缺了"
    for skill in NEEDS_TARGET:
        if skill not in uncovered:
            return False, "「未纳入汇总的能力」漏了 %s——5 个需要指定对象的 " \
                          "skill 必须无条件点齐" % skill
    if "## Deterministic Findings" not in out:
        return False, "报告里没有「## Deterministic Findings」小节"
    if _VACUUM_SOURCE_MARK not in out:
        return False, "Deterministic Findings 里没有 %r——plan_data 活体夹具" \
                      "必然让 gaussdb-vacuum 命中，它的 finding 必须带来源指针" \
                      % _VACUUM_SOURCE_MARK
    if "## Collection Notes" not in out:
        return False, "报告里没有「## Collection Notes」小节"
    return True, ""


def check_json(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    try:
        data = json.loads(out)
    except Exception as exc:
        return False, "stdout 不是合法 JSON：%r；前 200 字：%s" % (exc, out[:200])
    subs = data.get("sub_skills", [])
    got = {s.get("skill") for s in subs}
    if got != set(SUB_SKILLS):
        return False, "sub_skills 应恰好是三个子 skill，实际 %r" % sorted(got)
    for s in subs:
        if s.get("ok") is not True:
            return False, "sub_skills 里 %s 的 ok=%r（error=%r）——正常汇总用例" \
                          "里三个都该成功" % (s.get("skill"), s.get("ok"), s.get("error"))
    if set(data.get("uncovered_capabilities", [])) != set(NEEDS_TARGET):
        return False, "uncovered_capabilities 应恰好是 5 个需要指定对象的 skill，" \
                      "实际 %r" % data.get("uncovered_capabilities")
    findings = data.get("findings", [])
    for f in findings:
        if not isinstance(f.get("severity"), int):
            return False, "finding %r 的 severity 不是 int：%r" \
                          % (f.get("code"), f.get("severity"))
    if not any(f.get("skill") == "gaussdb-vacuum" for f in findings):
        return False, "findings 里没有任何一条来自 gaussdb-vacuum——plan_data " \
                      "活体夹具应该稳定命中，汇总可能丢了子 skill 的结果"
    return True, ""


def check_include_locks(rc, out, err, mode):
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    try:
        data = json.loads(out)
    except Exception as exc:
        return False, "stdout 不是合法 JSON：%r；前 200 字：%s" % (exc, out[:200])
    subs = data.get("sub_skills", [])
    got = {s.get("skill") for s in subs}
    if got != {"gaussdb-lockwait"}:
        return False, "--include locks 之后 sub_skills 应只列 gaussdb-lockwait，" \
                      "实际 %r——scope 过滤可能失效" % sorted(got)
    if data.get("dims"):
        return False, "--include locks 没有任何本地维度在范围内，dims 应为空，" \
                      "实际 %d 个" % len(data.get("dims", []))
    bad = [f.get("code") for f in data.get("findings", [])
           if f.get("skill") != "gaussdb-lockwait"]
    if bad:
        return False, "--include locks 之后 findings 里混入了别家的：%r" % bad
    return True, ""


def check_no_sub_skill_scope(rc, out, err, mode):
    """`--include overview`：一个子 skill 都不在范围内，一个子进程都不该起。

    这条盯的是"没查"与"查过没事"的区别在真实进程路径上还在不在——报告里
    没有锁/等待/膨胀任何信息，唯一告诉读者为什么的就是那句话。单测
    （test_an_empty_scope_is_said_out_loud_not_left_blank）钉的是渲染层，
    这里钉的是端到端：`health.py: main()` 里"没有子 skill 在范围内就不调
    collect_all()"那条捷径，不能顺手把这句话也一起跳过。
    """
    if rc != 0:
        return False, "rc=%d（期望 0）：%s" % (rc, (err or out)[:200])
    missing = _section(out, "本次未采集到的维度")
    if not missing:
        return False, "报告里没有「## 本次未采集到的维度」小节——不能因为这次" \
                      "没有子 skill 在范围内就把这一段整个省掉"
    if "没有子 skill 纳入范围" not in missing:
        return False, "一个子 skill 都不在范围内，这一段却没说清楚是被 " \
                      "--include/--exclude 排除的：%r" % missing[:200]
    if "全部采集成功" in missing:
        return False, "一个子 skill 都没跑，却写着「全部采集成功」——" \
                      "把「没查」念成了「查过没事」"
    if _VACUUM_SOURCE_MARK in out:
        return False, "--include overview 不该纳入 gaussdb-vacuum，报告里却出现了" \
                      "它的来源指针 %r" % _VACUUM_SOURCE_MARK
    if "## 未纳入汇总的能力" not in out:
        return False, "「## 未纳入汇总的能力」是结构性说明，与本次跑了谁无关，" \
                      "不该消失"
    return True, ""


def check_bad_conn(rc, out, err, mode):
    if rc != 2:
        return False, "rc=%d（期望 2：连接名不存在应被干净拒绝）：%s" \
                      % (rc, (err or out)[:200])
    return True, ""


# name, conn_override（None 表示用该模式默认连接）, extra_args, expect_reject, checker
BASELINE_CASES = (
    ("正常汇总（markdown）", None, [], False, check_default),
    ("--format json", None, ["--format", "json"], False, check_json),
    ("--include locks", None, ["--include", "locks", "--format", "json"],
     False, check_include_locks),
    ("--include overview（无子 skill）", None, ["--include", "overview"],
     False, check_no_sub_skill_scope),
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
            rc, out, err, dt = run_health(env, conn, args)
            floor = _judge_floor(rc, out, err, expect_reject)
            if floor is not None:
                ok, note = floor
            else:
                ok, note = checker(rc, out, err, mode)
            rows.append((name, mode, "PASS" if ok else "FAIL", note))
            print("%-24s %-5s %-5s %5.1fs %s"
                 % (name, mode, "PASS" if ok else "FAIL", dt, note[:70]))


def check_disabled_sub_skill(rc, out, err, mode):
    """rc=3 是附加信息不是替代输出——退出码和报告完整性两头都要对。"""
    if rc != 3:
        return False, "rc=%d（期望 3：有子 skill 采集失败时报告照常打印、" \
                      "退出码升为 3）：%s" % (rc, (err or out)[:200])
    if "# Health Evidence" not in out:
        return False, "rc=3 但报告抬头没了——3 不该意味着报告被截断"
    missing = _section(out, "本次未采集到的维度")
    if not missing:
        return False, "rc=3 但报告里没有「## 本次未采集到的维度」小节"
    bullet = next((l for l in missing.splitlines()
                   if l.startswith("- **gaussdb-lockwait**：")), "")
    if not bullet:
        return False, "「本次未采集到的维度」没有点名 gaussdb-lockwait：%r" \
                      % missing[:200]
    if not bullet.split("：", 1)[1].strip():
        return False, "点名了 gaussdb-lockwait 但没给原因——读者不知道该修什么"
    notes = _section(out, "Collection Notes")
    if "gaussdb-lockwait" not in notes:
        return False, "Collection Notes 里没有 gaussdb-lockwait 的降级行"
    if "## Deterministic Findings" not in out:
        return False, "rc=3 但「## Deterministic Findings」小节没了——报告不完整"
    if _VACUUM_SOURCE_MARK not in out:
        return False, "lockwait 不可用不该影响其余两个子 skill——" \
                      "gaussdb-vacuum 的来源指针 %r 从报告里消失了" % _VACUUM_SOURCE_MARK
    return True, ""


def run_disabled_sub_skill(rows: list) -> None:
    """把已部署目录里 lockwait 的脚本临时改名，验证 rc=3 且报告完整；
    无论成败都还原（finally）。改名只发生在 skills-dev 部署目录，不碰仓库。"""
    name = "子 skill 不可用→rc=3"
    # 不用 with_suffix()：那是"替换扩展名"的语义，这里要的是在完整文件名
    # 后面追加一截，让 aggregate.script_path() 解析出来的路径落空。
    disabled = LOCKWAIT_PY.parent / (LOCKWAIT_PY.name + ".matrix-disabled")
    for mode, default_conn, env_fn in MODES:
        try:
            env = env_fn()
        except ModeSetupError as exc:
            rows.append((name, mode, "FAIL", "模式环境没搭好：%s" % exc))
            print("  [FAIL] %s / %s —— %s" % (name, mode, exc))
            continue
        if not LOCKWAIT_PY.exists():
            rows.append((name, mode, "FAIL",
                         "部署目录里没有 %s，没法演这条故障" % LOCKWAIT_PY))
            print("  [FAIL] %s / %s —— 缺 lockwait.py" % (name, mode))
            continue
        LOCKWAIT_PY.rename(disabled)
        try:
            rc, out, err, dt = run_health(env, default_conn, [])
        finally:
            disabled.rename(LOCKWAIT_PY)
        floor = _judge_floor(rc, out, err, expect_reject=True)   # rc 必须非 0
        if floor is not None:
            ok, note = floor
        else:
            ok, note = check_disabled_sub_skill(rc, out, err, mode)
        rows.append((name, mode, "PASS" if ok else "FAIL", note))
        print("%-24s %-5s %-5s %5.1fs %s"
             % (name, mode, "PASS" if ok else "FAIL", dt, note[:70]))


def main() -> int:
    if not HEALTH_PY.exists():
        print("health.py 不存在于已部署目录：%s\n"
             "先部署：bash install-opencode.sh --dest %s" % (HEALTH_PY, SK),
             file=sys.stderr)
        return 2

    rows: list = []
    run_baseline(rows)
    run_disabled_sub_skill(rows)

    fails = [r for r in rows if r[2] == "FAIL"]
    print("\n" + "=" * 78)
    print("共 %d 例（%d 用例 × 2 模式 + 1 故障注入 × 2 模式），PASS %d，FAIL %d"
         % (len(rows), len(BASELINE_CASES), len(rows) - len(fails), len(fails)))
    for name, mode, verdict, note in fails:
        print("  [FAIL] %s / %s —— %s" % (name, mode, note))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
