#!/usr/bin/env python3
"""部署后自检:装完技能包、导完白名单、登录之后跑一次,把「不是代码问题、但会让功能用不了」的环境问题一次查出来。

2026-11-08 行方测试报告后加。那份报告里一半的「失败 / 受限」不是代码 bug,而是环境:新包下载了没装进正在用的技能目录、
白名单停在 133 条没导全、中间件跑不了一条脚本里的多条语句、受限账号对别的库没权限、track_functions 关着、
知识库目录不存在 —— 每一样都是用到时才以「HTTP 400 agent诊断脚本执行失败」或「列为空」的形态冒出来,排查要一层层倒推。

    python3 {baseDir}/scripts/selfcheck.py --session <句柄>

只跑白名单里的只读脚本,每条 LIMIT 1 或等价的极小结果;不改任何东西。输出每一项的结论、原因和该找谁。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Dict, List, NamedTuple, Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
for _anc in _HERE.parents:
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

import render  # noqa: E402
from common import access, cli, config  # noqa: E402
from common.version import package_version  # noqa: E402
from common.grmp import routing  # noqa: E402
from common.grmp.registry import Registry  # noqa: E402

OK, WARN, FAIL = "✓", "！", "✕"
DEPLOY, MIDDLEWARE, OPS = "部署方", "中间件团队", "运维 DBA"
BANK_OPAQUE = "agent诊断脚本执行失败"      # 行方 GRMP 执行失败时只回这一句,数据库的原始报错留在中间件日志里

# 关键脚本试跑:(脚本, 参数, 新版本才有的列, 用途)。只挑每类能力的入口与现场出过问题的几条,不逐条跑全部 138 条
PROBES = (
    ("health.overview", {}, (), "健康检查总览"),
    ("health.storage", {}, (), "健康检查 Storage(库与表空间占用)"),
    ("health.slow_sql", {"threshold_ms": 2147483647, "limit": 1}, ("user_name",), "健康检查 Slow SQL"),
    ("topsql.top_sql", {"order": "total_elapse_time DESC", "limit": 1},
     ("cpu_sec", "io_sec", "rows_written", "user_name"), "Top SQL"),
    ("memanalyze.wlm_sql", {"limit": 1}, (), "内存分析 L4(SQL 级)"),
)
# 数据库开关:(参数, 期望, 关掉时受影响的功能)
SWITCHES = (
    ("enable_stmt_track", "on", "慢 SQL / Top SQL / SQL 调优取不到语句统计"),
    ("track_functions", "!none", "存储过程排行(gaussdb-topproc)为空"),
    ("enable_resource_track", "on", "内存分析 L4 / L5(SQL 级、算子级)取不到"),
    ("enable_wdr_snapshot", "on", "WDR 时段报告没有快照可用"),
)


class Item(NamedTuple):
    what: str
    mark: str
    detail: str
    owner: str = ""


def _err(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    if BANK_OPAQUE in text:
        return "中间件只回了「%s」,数据库的原始报错在中间件日志里" % BANK_OPAQUE
    return text[:160]


def check_install() -> List[Item]:
    ver = package_version()
    items = [Item("技能包版本", OK if ver else WARN,
                  ver or "读不到 common/VERSION:正在用的技能目录可能还是旧包(v1.4.10 之前的包没有这个文件)",
                  "" if ver else DEPLOY)]
    skills = os.environ.get("GSDB_SKILLS_DIR", "")
    items.append(Item("GSDB_SKILLS_DIR", OK if skills else FAIL,
                      skills or "没设:根提示词里的命令按它找脚本", "" if skills else DEPLOY))
    try:
        from common.kb.config import resolve_kb_dir
        kb = resolve_kb_dir(None)
        items.append(Item("知识库目录", OK if kb.is_dir() else WARN,
                          str(kb) if kb.is_dir() else "%s 不存在:gaussdb-kb 不可用,诊断报告里没有本行知识引用" % kb,
                          "" if kb.is_dir() else DEPLOY))
    except Exception as exc:  # noqa: BLE001 —— 自检本身不因为某一项出错而中断
        items.append(Item("知识库目录", WARN, _err(exc), DEPLOY))
    return items


def used_names(registry: Registry, skills: Optional[pathlib.Path] = None) -> set:
    """有技能调用的脚本:逻辑名全串出现在技能代码里(与 grmp_register 交付时剔掉「没有任何 skill 调用」的规则一致)。

    容器镜像里带着全部定义(含 13 条不交付的),技能包里只带交付的 138 条 —— 按「有人调用」比,两种部署结论一致,
    不会把没交付的那几条报成「缺」。交付规则只搜各技能目录,所以这里也跳过部署后放进技能目录的 ``common/``:
    里面的 ``common/grmp/routing.py`` 是切库脚本的名单(数据,不是调用),含 2 条不交付的,
    不跳过的话容器里会把那 2 条报成「白名单缺」(11-11 测试环境实测查出)。
    注意:技能代码(含注释)里别写不交付脚本的全名 —— 交付按全名出现与否决定带哪些定义。"""
    skills = skills or pathlib.Path(__file__).resolve().parents[2]
    me = pathlib.Path(__file__).name
    text = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in skills.rglob("*.py")
                     if f.name != me and f.relative_to(skills).parts[0] != "common")
    return {n for n in registry.names() if n in text}


def check_whitelist(conn, registry: Registry) -> List[Item]:
    try:
        ops = access.runner_for(conn).client.list_operations()
    except Exception as exc:  # noqa: BLE001
        return [Item("中间件与白名单", FAIL, "命令清单取不到:%s" % _err(exc), MIDDLEWARE)]
    have = {str(d.get("cmd_name", "")) for d in ops if d.get("cmd_name")}
    if not have:
        return [Item("白名单", FAIL, "中间件对这个实例返回的脚本清单为空:清单按实例属性(部署形态、内核版本等)匹配返回,"
                     "我方登记的是集中式实例,这个实例的属性多半与之不匹配", MIDDLEWARE)]
    want = used_names(registry)
    missing = sorted(want - have)
    if not missing:
        return [Item("白名单", OK, "本版本用到的 %d 条都已登记(中间件共 %d 条)" % (len(want), len(have)))]
    return [Item("白名单", FAIL, "缺 %d 条(中间件 %d 条,本版本要 %d 条):%s%s。按补充说明「SQL 白名单」一节导入增量"
                 % (len(missing), len(have), len(want), "、".join(missing[:8]), "等" if len(missing) > 8 else ""),
                 MIDDLEWARE)]


def check_scripts(runner) -> List[Item]:
    items = []
    try:
        rows = runner.run("explain.multi_stmt_probe", {})
        ok = bool(rows) and str(next(iter(rows[0].values()), "")).strip() == "1"
    except Exception:  # noqa: BLE001
        ok = False
    items.append(Item("中间件:一条脚本多条语句", OK if ok else WARN,
                      "支持" if ok else "不支持:执行计划与调优改为补全表名后单条执行(已兼容),不影响使用"))
    for name, values, new_cols, what in PROBES:
        try:
            rows = runner.run(name, values)
        except Exception as exc:  # noqa: BLE001
            items.append(Item(what, FAIL, "%s 执行失败:%s" % (name, _err(exc)), MIDDLEWARE + " / " + OPS))
            continue
        gone = [c for c in new_cols if rows and c not in rows[0]]
        if gone:
            items.append(Item(what, WARN, "%s 是旧版本(缺列 %s):相关列显示「—」,导入本版本的白名单增量后恢复"
                              % (name, "、".join(gone)), MIDDLEWARE))
        elif name == "health.storage" and any(r.get("bytes") in (None, "") for r in rows or []):
            n = sum(1 for r in rows if r.get("bytes") in (None, ""))
            items.append(Item(what, WARN, "执行账号对 %d 个库 / 表空间没有权限查看占用,这几项报告里写「无权限查看」" % n, OPS))
        else:
            items.append(Item(what, OK, "%s 正常" % name))
    return items


def check_routing(conn, runner) -> List[Item]:
    """切到业务库(dbName,2026-11-11 行方切库方案):本会话切过去没有;行方白名单里的标注与本版是否一致。"""
    client = getattr(runner, "client", None)
    if client is None:
        return []
    try:
        reg = {n for n in routing.BUSINESS_DB_SCRIPTS if client.registered(n)}
        missing = sorted(n for n in reg if not client.declares(n, routing.PARAM))
        others = sorted(n for n in client.names() if n not in routing.BUSINESS_DB_SCRIPTS
                        and client.declares(n, routing.PARAM))
    except Exception as exc:  # noqa: BLE001
        return [Item("业务库切换", WARN, "命令清单取不到:%s" % _err(exc), MIDDLEWARE)]
    items = []
    if conn.route_db:
        try:
            got = str((runner.run(routing.PROBE) or [{}])[0].get("db") or "")
        except Exception as exc:  # noqa: BLE001
            got = "(调用失败:%s)" % _err(exc)
        ok = got.lower() == conn.route_db.lower()
        items.append(Item("业务库切换", OK if ok else FAIL,
                          "已切到「%s」:表、索引、执行计划、统计信息、存储过程在这个库上取" % got if ok else
                          "会话记的是「%s」,探针带 dbName 回的是「%s」:中间件没按 dbName 切库" % (conn.route_db, got),
                          "" if ok else MIDDLEWARE))
    elif not reg or not any(client.declares(n, routing.PARAM) for n in reg):
        items.append(Item("业务库切换", WARN, "行方白名单还没有带 dbName 的脚本:对象类取数(执行计划、表结构、索引等)只能在 postgres 上做。"
                          "按本版增量白名单导入后,登录时带 --database <业务库> 即可切库", MIDDLEWARE))
    else:
        items.append(Item("业务库切换", WARN, "本会话没有切到业务库(登录时没给业务库名,或核实没通过 —— 见登录输出)。"
                          "要诊断某个业务库的表、执行计划,登录时带 --database <业务库>", ""))
    if missing or others:
        parts = []
        if missing:
            parts.append("%d 条该切库的没带 dbName(%s%s),它们仍在 postgres 上执行"
                         % (len(missing), "、".join(missing[:4]), "等" if len(missing) > 4 else ""))
        if others:
            parts.append("%d 条读 SQL 历史 / WDR 快照 / 实例级视图的带了 dbName(%s%s),本版对它们明确带 postgres,照常可用"
                         % (len(others), "、".join(others[:4]), "等" if len(others) > 4 else ""))
        items.append(Item("白名单切库标注", WARN, ";".join(parts) + "。按本版增量白名单导入后一致", MIDDLEWARE))
    elif reg:
        items.append(Item("白名单切库标注", OK, "与本版一致(%d 条在业务库执行)" % len(reg)))
    return items


def check_switches(runner) -> List[Item]:
    try:
        rows = runner.run("config.settings", {})
    except Exception as exc:  # noqa: BLE001
        return [Item("数据库监控开关", WARN, "读不到参数:%s" % _err(exc), MIDDLEWARE)]
    got: Dict[str, str] = {str(r.get("name")): str(r.get("setting") or "") for r in rows or []}
    items = []
    for name, expect, effect in SWITCHES:
        if name not in got:
            continue                      # 这个内核没有这个参数:不报
        val = got[name]
        good = (val.lower() != expect[1:]) if expect.startswith("!") else (val.lower() == expect)
        items.append(Item("参数 %s" % name, OK if good else WARN,
                          "= %s" % val if good else "= %s:%s;需要时由运维开启" % (val, effect), "" if good else OPS))
    return items


def render_report(items: List[Item]) -> str:
    bad = [i for i in items if i.mark != OK]
    head = "# 部署自检\n\n" + ("全部正常。\n\n" if not bad else "%d 项需要处理(✕ 功能不可用,！ 部分受限)。\n\n" % len(bad))
    return head + render.table(["检查项", "结果", "说明", "找谁"], [[i.what, i.mark, i.detail, i.owner or "—"] for i in items])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--conn", default="", help="连接名(省略则用 gaussdb-login 建立的会话)")
    cli.add_session_arg(ap)
    args = ap.parse_args(argv)
    cli.apply_session_arg(args)
    items = check_install()
    try:
        conn = config.resolve(args.conn or None)
        runner = access.runner_for(conn)
    except Exception as exc:  # noqa: BLE001
        items.append(Item("登录会话", FAIL, "%s。先用 gaussdb-login 登录再自检" % _err(exc), ""))
        print(render_report(items), end="")
        return 1
    if conn.driver == "grmp":
        items += check_whitelist(conn, Registry()) + check_routing(conn, runner)
    items += check_scripts(runner) + check_switches(runner)
    print(render_report(items), end="")
    return 0 if all(i.mark != FAIL for i in items) else 1


if __name__ == "__main__":
    raise SystemExit(main())
