#!/usr/bin/env python3
"""生成白名单的**增量导入包** —— 相对行方已导入的那一版,只含新增与变更。

    python3 agent/tools/delivery_delta.py --base agent-v1.4.6 --date 2026-11-05 --out docs/delivery/scripts-delta-20261105.sql

(2026-11-05 起在本仓库维护:gh_skill 不再同步,交付 DML 在仓库根的 docs/delivery/,脚本定义在 agent/scripts/registry/。
--base 给行方在用的那一版的发布标签。)

**为什么要有这个工具。** 2026-10-18 第一次出增量包时,新增/变更清单是手写的,
结果漏了 4 条正文变更 —— 其中 wdr.file_io 修的是物理读少报 62%、写得最多的那个
文件在报告里完全不存在。手写清单靠记忆,而记忆恰恰会漏掉「顺手改过的那几条」。

所以清单一律**自动算**:拿 `--base` 那个 git 版本里的 docs/delivery/scripts.sql
(= 行方在用的那份)对比当前的 docs/delivery/scripts.sql(= 交付集),逐条比脚本名
与 SQL 正文。说明文字从 docs/delivery/delta-notes.yaml 读;**任何一条新增或变更
在那里没有说明,直接报错退出** —— 不许静默地出一个缺条目的包。

输出的 DML 用**行方真实形态**(与 scripts.sql 一致:表 grmp.script_config、
id 走 seq.nextval、uuid()、now()…),不是 grmp_register 的生成器形态 ——
后者行方导不进去(同一天踩过,见 tests/test_delivery_drift_units.py 末尾那段)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys

_AGENT = pathlib.Path(__file__).resolve().parents[1]
_ROOT = _AGENT.parent                     # 仓库根:docs/delivery/ 在这里
sys.path.insert(0, str(_AGENT))

import yaml  # noqa: E402

from common.grmp.script import load_script  # noqa: E402

SHIPPED = "docs/delivery/scripts.sql"
NOTES = _ROOT / "docs" / "delivery" / "delta-notes.yaml"

COLS = ('id, script_type, script_name, database_type, refered_appbusiness, kernel_version, '
        'region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, '
        'parameter_config, scene, is_valid, create_user, create_time, last_modify_user, '
        'last_modify_time, is_asyn, "extend", compliance_mode, uuid')

_NAME = re.compile(r"VALUES \([^,]+, '[A-Z]+', '([a-z_]+\.[a-z_0-9]+)'")
_BODY = re.compile(r"'centralization', '(.*?)', '\[", re.S)
_PARAMS = re.compile(r"'centralization', '.*?', '(\[.*?\])', 'AGENT'", re.S)
_DBTYPE = re.compile(r"VALUES \([^,]+, '[A-Z]+', '[a-z_]+\.[a-z_0-9]+', '([a-z]+)'")


class _NoDupLoader(yaml.SafeLoader):
    """同一层里重复的键直接报错(yaml 默认后一个静默覆盖前一个)。"""


def _no_dup_mapping(loader, node, deep=False):
    keys = set()
    for k, _v in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in keys:
            raise ValueError("%s 里 %r 写了两次:后一条会静默顶替前一条(v1.4.14 的增量说明就这样写错过),只留最新的一条"
                             % (NOTES.name, key))
        keys.add(key)
    return loader.construct_mapping(node, deep=deep)


_NoDupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


def load_notes(text: str) -> dict:
    """读增量说明;有重复的脚本名就报错,不许旧说明顶替新说明(2026-11-19)。"""
    return yaml.load(text, Loader=_NoDupLoader) or {}


def bodies(dml: str) -> dict:
    """DML 文本 → {脚本名: SQL 正文}。正文里的 '' 还原成 '。"""
    out = {}
    for b in re.split(r"(?=INSERT INTO )", dml):
        n, m = _NAME.search(b), _BODY.search(b)
        if n and m:
            out[n.group(1)] = m.group(1).replace("''", "'")
    return out


def params(dml: str) -> dict:
    """DML 文本 → {脚本名: parameter_config 原文}。"""
    out = {}
    for b in re.split(r"(?=INSERT INTO )", dml):
        n, m = _NAME.search(b), _PARAMS.search(b)
        if n and m:
            out[n.group(1)] = m.group(1).replace("''", "'")
    return out


def dbtypes(dml: str) -> dict:
    """DML 文本 → {脚本名: database_type}。"""
    out = {}
    for b in re.split(r"(?=INSERT INTO )", dml):
        n, m = _NAME.search(b), _DBTYPE.search(b)
        if n and m:
            out[n.group(1)] = m.group(1)
    return out


def routing_changed(base: str, names, reg: dict) -> set:
    """执行库(database_type)变了的脚本(2026-11-11 起按 registry,见 common/grmp/routing.py)。连同 parameter_config 一起更新:
    行方测试环境已手工给原先 appbusiness 的记录都加了 dbName,改回 postgres 的那些也要把 dbName 去掉。"""
    tb = dbtypes(base)
    return {k for k in names if k in tb and tb[k] != reg[k].database_type}


def _param_keys(text: str) -> list:
    """参数定义只比「有哪些参数、什么类型」:value / autoAcquire 这些占位字段不算变更。"""
    try:
        return [(p.get("key"), p.get("type")) for p in json.loads(text or "[]")]
    except (ValueError, AttributeError):
        return [("?", text)]


def registry() -> dict:
    return {load_script(p).script_name: load_script(p)
            for p in sorted((_AGENT / "scripts" / "registry").glob("*/*.yaml"))}


def _base_dml(base_ref: str) -> str:
    return subprocess.run(["git", "show", "%s:%s" % (base_ref, SHIPPED)], cwd=_ROOT,
                          capture_output=True, text=True, check=True).stdout


def diff(base_ref: str):
    """(新增, 变更, 消失) —— 三个排好序的脚本名列表。正文或参数定义变了都算变更。"""
    base = _base_dml(base_ref)
    now = (_ROOT / SHIPPED).read_text(encoding="utf-8")
    b, n, reg = bodies(base), bodies(now), registry()
    added = sorted(set(n) - set(b))
    gone = sorted(set(b) - set(n))
    pchanged = params_changed(base_ref, sorted(set(n) & set(b)), base=base, reg=reg)
    tchanged = routing_changed(base, sorted(set(n) & set(b)), reg)
    changed = sorted(k for k in set(n) & set(b)
                     if b[k].strip() != reg[k].script_content.strip() or k in pchanged or k in tchanged)
    return added, changed, gone, len(b), len(n)


def params_changed(base_ref: str, names, base: str = "", reg: dict = None) -> set:
    """参数定义变了的脚本。**只改正文不改参数,行方的中间件不认新参数** —— 2026-10-24 health.stale_stats
    加了 min_pages,第一版增量包只 UPDATE 了 script_content,导进去之后健康检查一调就会被拒。"""
    pb = params(base or _base_dml(base_ref))
    reg = reg or registry()
    return {k for k in names if k in pb and _param_keys(pb[k]) != _param_keys(reg[k].parameter_config)}


def _esc(sql: str) -> str:
    s = sql.replace("'", "''")
    return s if s.endswith("\n") else s + "\n"


def routing_note(name: str, reg: dict) -> str:
    """只改了执行库(database_type 与 dbName)的脚本:说明文字自动生成,不必逐条写进 delta-notes.yaml。"""
    if reg[name].database_type == "appbusiness":
        return "执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)"
    return "执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)"


def _stored(sql: str) -> str:
    """导入后库里存的正文(UPDATE / INSERT 写进去的那个字面量还原后)。"""
    return sql if sql.endswith("\n") else sql + "\n"


def _fields_note(added, changed, pchanged, tchanged) -> str:
    """文件头里「这一包改了哪些字段」:按实际写,不用固定说法(2026-11-19:本版 4 条只改正文,模板却说会重写执行库与参数)。"""
    writes = [n for n in changed if n in pchanged or n in tchanged]
    rest = len(changed) - len(writes)
    lines = []
    if added:
        lines.append("本文件里的 INSERT 带完整的执行库(database_type)与参数定义(parameter_config)。")
    if changed and not writes:
        lines.append("本文件里的 UPDATE 只改脚本正文(script_content),不动执行库(database_type)与参数定义(parameter_config)。")
    elif writes:
        lines.append("本文件里 %d 条 UPDATE(%s)同时写 database_type 与 parameter_config,以本文件为准覆盖手工改过的记录%s。"
                     % (len(writes), "、".join(writes), (";其余 %d 条只改脚本正文" % rest) if rest else ""))
    return "\n".join("-- " + ln for ln in lines) if lines else "-- 本文件没有新增或更新。"


def render(added, changed, notes, n_base, n_now, date, pchanged=frozenset(), tchanged=frozenset(), upgrade: str = "") -> str:
    reg = registry()
    out = ["""-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（%(date)s）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 %(nb)d 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 %(nn)d 条）。
--%(upgrade)s
--   新增   %(na)d 条 INSERT
--   更新   %(nc)d 条 UPDATE
--
-- 全部脚本均为**只读查询**，只访问系统视图与系统表，不读业务数据、
-- 不含 DML/DDL、不修改任何对象。
--
-- 执行库（database_type）：appbusiness 的脚本 parameter_config 最前面带
--   {"key":"dbName","value":"","type":"String","autoAcquire":false}，
-- 中间件按调用时传入的 dbName 切到该业务库执行；postgres 的脚本不带 dbName，在 postgres 库执行。
%(fields)s
--
-- 导入前请确认两处与现有规范的对齐：
--   1. `create_user` / `last_modify_user` 当前填 '999999999'，与上一版保持一致；
--      若要求填真实工号，请统一替换。
--   2. id 用 `grmp.script_config_seq.nextval`，不硬编码，避免与现有记录冲突。
--
-- =====================================================================
-- 一、新增 %(na)d 条
-- =====================================================================
""" % {"date": date, "nb": n_base, "nn": n_now, "na": len(added), "nc": len(changed),
       "upgrade": "".join("\n-- " + ln for ln in upgrade.splitlines()) + ("\n--" if upgrade else ""),
       "fields": _fields_note(added, changed, pchanged, tchanged)}]
    for name in added:
        rec = reg[name]
        title, src, why = notes["added"][name]
        out.append("\n-- [%s] %s\n--   读取：%s\n--   用途：%s\n" % (name, title, src, why))
        out.append("INSERT INTO grmp.script_config (%s) VALUES "
                   "(grmp.script_config_seq.nextval, 'SQL', '%s', '%s', 1, 'ALL', "
                   "NULL, NULL, NULL, 'centralization', '%s', '%s', 'AGENT', 1, "
                   "'999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());\n"
                   % (COLS, name, rec.database_type, _esc(rec.script_content), rec.parameter_config.replace("'", "''")))
    out.append("""
-- =====================================================================
-- 二、更新 %d 条已有脚本（SQL 正文 / 参数定义 / 执行库）
-- =====================================================================
""" % len(changed))
    for name in changed:
        out.append("\n-- [%s]\n--   %s\n" % (name, (notes.get("changed") or {}).get(name) or routing_note(name, reg)))
        pset = ("       database_type    = '%s',\n       parameter_config = '%s',\n"
                % (reg[name].database_type, reg[name].parameter_config.replace("'", "''"))
                if name in pchanged or name in tchanged else "")
        out.append("UPDATE grmp.script_config\n"
                   "   SET script_content   = '%s',\n"
                   "%s"
                   "       last_modify_user = '999999999',\n"
                   "       last_modify_time = now()\n"
                   " WHERE script_name = '%s';\n" % (_esc(reg[name].script_content), pset, name))
    names = ",".join("'%s'" % n for n in added)
    # 自检按这次的包现算:每条更新后的正文的 md5(以前写死的是 10-18 那一包的特征串,换一包就失效)
    md5s = ",\n--     ".join("('%s', '%s')" % (n, hashlib.md5(_stored(reg[n].script_content).encode("utf-8")).hexdigest())
                            for n in changed) or "('-', '-')"
    pnames = ",".join("'%s'" % n for n in sorted(pchanged | set(tchanged))) or "'-'"
    shipped = set(bodies((_ROOT / SHIPPED).read_text(encoding="utf-8")))
    n_business = sum(1 for n in shipped if n in reg and reg[n].database_type == "appbusiness")
    out.append("""
-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 %d：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
%s-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     %s) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN (%s);
-- 执行库应与本版一致（appbusiness 应为 %d 条，且都以 dbName 开头）：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness';
--   SELECT script_name FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness'
--      AND parameter_config NOT LIKE '[{"key":"dbName",%%';     -- 应返回 0 行
""" % (n_now, ("-- 新增的 %d 条应全部在列：\n--   SELECT script_name FROM grmp.script_config WHERE script_name IN (%s);\n"
                 % (len(added), names)) if added else "-- 本次没有新增脚本。\n", md5s, pnames, n_business))
    return "".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True,
                    help="行方在用的那一版所在的 git 版本(发布标签,如 agent-v1.4.6)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--date", required=True, help="写进文件头的日期,如 2026-10-18")
    ap.add_argument("--upgrade", default="", help="写进文件头的升级路径,如「从 v1.4.14(及更早)升级到 v1.5.0」;更早的版本先导哪几份也写在这里")
    a = ap.parse_args(argv)

    added, changed, gone, nb, nn = diff(a.base)
    notes = load_notes(NOTES.read_text(encoding="utf-8"))
    base, reg = _base_dml(a.base), registry()
    tchanged = routing_changed(base, changed, reg)
    b = bodies(base)
    # 只改了执行库(正文没变、参数除 dbName 外没变)的,说明自动生成;其余仍必须在 delta-notes.yaml 里写
    routing_only = {n for n in changed if b.get(n, "").strip() == reg[n].script_content.strip()
                    and [k for k in _param_keys(params(base).get(n, "[]")) if k[0] != "dbName"]
                    == [k for k in _param_keys(reg[n].parameter_config) if k[0] != "dbName"]}
    miss = ([n for n in added if n not in (notes.get("added") or {})]
            + [n for n in changed if n not in (notes.get("changed") or {}) and n not in routing_only])
    if miss:
        print("error: 这些脚本有新增/变更,但 %s 里没有说明 —— 补上再生成,"
              "不许出一个缺条目的包:\n  %s" % (NOTES.name, "\n  ".join(miss)), file=sys.stderr)
        return 2
    if gone:
        # 删除脚本需要行方手工处置(DELETE 有风险,不自动生成),但必须让人看见
        print("warning: 这些脚本在行方那版里有、现在没了,增量包**不**生成 DELETE,"
              "请人工确认是否要行方删除:\n  %s" % "\n  ".join(gone), file=sys.stderr)
    pchanged = params_changed(a.base, changed) if changed else set()
    pathlib.Path(a.out).write_text(render(added, changed, notes, nb, nn, a.date, pchanged, tchanged, a.upgrade), encoding="utf-8")
    print("写出 %s:新增 %d、更新 %d(其中参数定义变了 %d、执行库变了 %d、只改执行库 %d)(基线 %s,%d → %d 条)"
          % (a.out, len(added), len(changed), len(pchanged), len(tchanged), len(routing_only), a.base, nb, nn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
