#!/usr/bin/env python3
"""场景矩阵 —— 对**已安装**的 skill 跑一遍端到端用例，出通过/失败表。

单元测试验的是函数，这个验的是「命令跑起来会怎样」：参数边界、非法输入、
子命令、拒绝路径。两者不重叠 —— 本脚本抓到过两个单测全绿却真实存在的缺陷：

  · explain 的主 except 漏了 QueryError，SQL 打错字直接吐 Traceback
  · explain 取到计划后无条件 db.close()，而模板路径下 db 是 None

用法：

    python3 tools/scenario_matrix.py            # 用当前会话的连接
    python3 tools/scenario_matrix.py og-grmp    # 指定连接（会先登录）

**换环境必须先改下面这几个环境变量**，否则用例引用的库对象不存在，
会跑出一片假红：

    SM_SKILLS_DIR   已安装的 skill 目录（默认 ~/.config/opencode/skills）
    SM_APP          登录用的应用名（默认 og5）
    SM_TABLE        一张真实存在的表，schema 限定（默认 gaussdb.bench_reviews）
    SM_COLUMN       上表的一个列名（默认 product_id）
    SM_SCHEMA       一个真实 schema（默认 gaussdb）
    SM_SQLID        一个真实存在的 unique_sql_id（默认取 topsql 第一条）
    SM_PROC         一个真实存储过程，schema 限定（可空，空则跳过相关用例）

判定三类：

    ok      应当成功：退出码 0 且有实质输出
    reject  应当被拒：**干净地**拒 —— 有明确错误信息，不是崩
    nocrash 成不成功都行，但绝不能 Traceback

**铁律：任何 Traceback 一律记为 BUG**，无论期望是什么。未捕获异常意味着
那条出错路径没人设计过，用户看到的是一段栈而不是能照做的提示。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import time

SKILLS = pathlib.Path(os.environ.get(
    "SM_SKILLS_DIR", pathlib.Path.home() / ".config/opencode/skills"))
APP = os.environ.get("SM_APP", "og5")
TABLE = os.environ.get("SM_TABLE", "gaussdb.bench_reviews")
COLUMN = os.environ.get("SM_COLUMN", "product_id")
SCHEMA = os.environ.get("SM_SCHEMA", "gaussdb")
PROC = os.environ.get("SM_PROC", "sqltune_demo.proc_megabatch")
PY = sys.executable or "python3"

# 拒绝类用例：输出里出现下列任一片段就算「干净地拒了」。
# 这是有意放宽的 —— 各 skill 的措辞不统一，硬要求统一前缀会让这个矩阵
# 变成措辞检查器，而它要管的是「有没有说人话」。
_REJECT_MARKS = ("error", "错误", "不存在", "not found", "需要", "detected",
                 "Multiple", "拒绝", "失败", "No matching", "未命中", "无法")


def skill(name: str) -> str:
    return str(SKILLS / ("gaussdb-" + name) / "scripts" / (name + ".py"))


def discover_sqlid() -> str:
    """没给 SM_SQLID 就从 topsql 取第一条 —— 硬编码一个 id 换环境必然失效。"""
    given = os.environ.get("SM_SQLID")
    if given:
        return given
    out = subprocess.run([PY, skill("topsql"), "--limit", "1"],
                         capture_output=True, text=True, timeout=120).stdout
    hit = re.search(r"^\|\s*1\s*\|\s*(-?\d+)\s*\|", out, re.MULTILINE)
    return hit.group(1) if hit else ""


def build_cases(sqlid: str):
    cases = []

    def case(group, name, expect, argv, stdin=None):
        cases.append((group, name, expect, argv, stdin))

    lg = skill("login")
    case("login", "列出可选连接", "ok", [lg, "--list"])
    case("login", "查看会话状态", "ok", [lg, "--status"])
    case("login", "不存在的应用", "reject", [lg, "--app", "nope", "--conn", "x"])
    case("login", "不存在的连接", "reject", [lg, "--app", APP, "--conn", "nope"])

    ts = skill("topsql")
    for by in ("time", "avg", "calls", "reads", "rows"):
        case("topsql", "排序 --by %s" % by, "ok", [ts, "--by", by, "--limit", "2"])
    case("topsql", "limit 0（边界）", "nocrash", [ts, "--limit", "0"])
    case("topsql", "limit 负数", "nocrash", [ts, "--limit", "-5"])
    case("topsql", "非法排序键", "reject", [ts, "--by", "nosuch"])
    case("topsql", "json 格式", "ok", [ts, "--limit", "2", "--format", "json"])

    ss = skill("slowsql")
    case("slowsql", "阈值 500", "ok", [ss, "--threshold", "500", "--limit", "3"])
    case("slowsql", "阈值 0（边界）", "nocrash", [ss, "--threshold", "0", "--limit", "2"])
    case("slowsql", "阈值极大", "ok", [ss, "--threshold", "99999999"])
    case("slowsql", "json 格式", "ok", [ss, "--threshold", "500", "--format", "json"])
    case("slowsql", "export false 陷阱", "nocrash",
         [ss, "--threshold", "500", "--export", "false"])

    sf = skill("sqlfetch")
    if sqlid:
        case("sqlfetch", "有效 sql_id", "ok", [sf, sqlid])
    case("sqlfetch", "不存在的 id", "reject", [sf, "999999999"])
    case("sqlfetch", "非数字 id", "reject", [sf, "abc"])
    case("sqlfetch", "负数 id", "nocrash", [sf, "-1"])

    ex = skill("explain")
    case("explain", "正常 SELECT", "ok", [ex, "--sql-stdin"],
         "SELECT count(*) FROM %s" % TABLE)
    case("explain", "DML 应拒", "reject", [ex, "--sql-stdin"],
         "UPDATE %s SET %s=1 WHERE %s=1" % (TABLE, COLUMN, COLUMN))
    case("explain", "DDL 应拒", "reject", [ex, "--sql-stdin"], "CREATE TABLE zzz(i int)")
    case("explain", "多语句应拒", "reject", [ex, "--sql-stdin"], "SELECT 1; SELECT 2;")
    case("explain", "字面量含分号不误判", "ok", [ex, "--sql-stdin"], "SELECT 'a;b' AS x")
    case("explain", "空 SQL", "reject", [ex, "--sql-stdin"], "")
    # 下面两条是这个矩阵的成名作 —— 曾经各吐一次 Traceback
    case("explain", "语法错误的 SQL", "reject", [ex, "--sql-stdin"], "SELEKT * FROM t")
    case("explain", "不存在的表", "reject", [ex, "--sql-stdin"],
         "SELECT * FROM nosuchtable_zzz")
    case("explain", "json 格式", "ok", [ex, "--sql-stdin", "--format", "json"], "SELECT 1")

    he = skill("health")
    case("health", "全量", "ok", [he])
    case("health", "裁剪 --include", "ok", [he, "--include", "locks,conn"])
    case("health", "排除 --exclude", "ok", [he, "--exclude", "wdr"])
    case("health", "非法维度名", "nocrash", [he, "--include", "nosuchdim"])
    case("health", "json 格式", "ok", [he, "--format", "json"])

    sr = skill("sqlreview")
    case("sqlreview", "--stdin", "ok", [sr, "--stdin"], "SELECT * FROM t")
    case("sqlreview", "--schema", "ok", [sr, "--schema", SCHEMA])
    case("sqlreview", "--top 3", "ok", [sr, "--top", "3"])
    if sqlid:
        case("sqlreview", "--sql-id", "ok", [sr, "--sql-id", sqlid])
    case("sqlreview", "两个输入源应拒", "reject", [sr, "--stdin", "--top", "3"])
    case("sqlreview", "零个输入源应拒", "reject", [sr])
    case("sqlreview", "不存在的 schema", "nocrash", [sr, "--schema", "nosuch_zzz"])

    st = skill("sqltune")
    if sqlid:
        case("sqltune", "按 sql_id", "ok", [st, sqlid])
    case("sqltune", "--sql-stdin", "ok", [st, "--sql-stdin"],
         "SELECT * FROM %s WHERE %s = 1" % (TABLE, COLUMN))
    case("sqltune", "不存在的 sql_id", "reject", [st, "999999999"])
    case("sqltune", "语法错误的 SQL", "reject", [st, "--sql-stdin"], "SELEKT * FROM t")
    case("sqltune", "DML 应拒", "reject", [st, "--sql-stdin"],
         "DELETE FROM %s WHERE %s=1" % (TABLE, COLUMN))

    if PROC:
        case("procinfo", "真实存储过程", "ok", [skill("procinfo"), PROC])
        case("proctune", "collect", "ok", [skill("proctune"), "collect", PROC])
        case("proctune", "tune-cursor", "ok", [skill("proctune"), "tune-cursor", PROC])
    case("procinfo", "不存在的过程", "reject", [skill("procinfo"), "nosuch.nosuch"])
    case("topproc", "列出耗时过程", "nocrash", [skill("topproc"), "--limit", "3"])

    wd = skill("wdr")
    case("wdr", "snaps", "ok", [wd, "snaps"])
    case("wdr", "collect 假快照应拒", "reject", [wd, "collect", "--begin", "1", "--end", "2"])
    case("wdr", "begin>end 应拒", "reject", [wd, "collect", "--begin", "9", "--end", "3"])

    ma = skill("memanalyze")
    case("memanalyze", "snapshot", "ok", [ma, "snapshot", "--top", "3"])
    case("memanalyze", "history", "ok", [ma, "history", "--top", "3"])
    case("memanalyze", "watch count 太小应拒", "reject",
         [ma, "watch", "--count", "2", "--interval", "1"])

    kb = skill("kb")
    case("kb", "search 缺关键词应拒", "reject", [kb, "search"])
    # 刻意不测 `kb contract`：安装目录里它**必然**报 stale ——
    # install-opencode.sh 会把 {kbDir} 替换成真实路径，契约块内容随之改变，
    # 一致性校验就判成过期。那条只能对仓库跑。
    return cases


def run(argv, stdin):
    t0 = time.time()
    try:
        p = subprocess.run(argv, input=(stdin or ""), capture_output=True,
                           text=True, timeout=180)
        return p.returncode, p.stdout, p.stderr, time.time() - t0
    except subprocess.TimeoutExpired:
        return -99, "", "TIMEOUT after 180s", time.time() - t0


def judge(expect, rc, out, err):
    blob = out + err
    if "Traceback (most recent call last)" in blob:
        return "BUG", "未捕获异常（Traceback）"
    if expect == "nocrash":
        return "PASS", ""
    if expect == "ok":
        if rc != 0:
            return "FAIL", "期望成功，退出码 %d：%s" % (rc, (err or out).strip()[:70])
        if len(out.strip()) < 10:
            return "FAIL", "退出码 0 但几乎没有输出"
        return "PASS", ""
    if rc == 0 and not any(k in blob for k in _REJECT_MARKS):
        return "FAIL", "期望被拒，却静默成功了"
    return "PASS", ""


def main() -> int:
    if not SKILLS.exists():
        print("skill 目录不存在：%s（用 SM_SKILLS_DIR 指定）" % SKILLS,
              file=sys.stderr)
        return 2

    conn = sys.argv[1] if len(sys.argv) > 1 else ""
    if conn:
        # 会话按句柄分文件之后,登录不再覆盖上一次:同一个 GSDB_HOME 里连跑两个连接的矩阵会留下两个会话,
        # 不带句柄的用例就会被「沙箱里有 2 个会话」拒掉——那是产品的正确行为,不是失败。
        # 矩阵模拟的是单用户换库,所以登录前先清掉本目录的会话。
        subprocess.run([PY, skill("login"), "--logout", "--all"], capture_output=True, text=True)
        login = subprocess.run([PY, skill("login"), "--app", APP, "--conn", conn],
                               capture_output=True, text=True)
        if login.returncode != 0:
            print("登录 %s/%s 失败，矩阵不跑 —— 否则 68 条会一起红，"
                  "看不出是环境问题还是代码问题。" % (APP, conn), file=sys.stderr)
            return 2
        # 2026-09-11 起会话只属于本对话:不带句柄一律「本对话未登录」,不再有「唯一会话自动用」。
        # 矩阵里后续用例靠环境变量继承句柄——这正是平台按用户注入 GSDB_SESSION 的那条路。
        hit = re.search(r"--session ([a-z0-9]{4,16})", login.stdout)
        if not hit:
            print("登录输出里没有会话句柄，矩阵不跑。", file=sys.stderr)
            return 2
        os.environ["GSDB_SESSION"] = hit.group(1)

    sqlid = discover_sqlid()
    if not sqlid:
        print("警告：取不到可用的 unique_sql_id，相关用例已跳过"
              "（用 SM_SQLID 指定）", file=sys.stderr)

    rows = []
    for group, name, expect, argv, stdin in build_cases(sqlid):
        rc, out, err, dt = run([PY] + argv, stdin)
        verdict, note = judge(expect, rc, out, err)
        rows.append((group, name, expect, verdict, note))
        print("%-11s %-26s %-8s %-5s %5.1fs %s"
              % (group, name[:26], expect, verdict, dt, note[:60]))

    bugs = [r for r in rows if r[3] == "BUG"]
    fails = [r for r in rows if r[3] == "FAIL"]
    print("\n" + "=" * 78)
    print("连接 %s：共 %d 例，PASS %d，FAIL %d，BUG %d"
          % (conn or "(当前会话)", len(rows),
             len(rows) - len(fails) - len(bugs), len(fails), len(bugs)))
    for r in bugs + fails:
        print("  [%s] %s / %s —— %s" % (r[3], r[0], r[1], r[4]))
    return 1 if (bugs or fails) else 0


if __name__ == "__main__":
    raise SystemExit(main())
