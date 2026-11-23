"""把 registry 里指定脚本按**行方真实 DML 形态**追加进 docs/delivery/scripts.sql。

为什么单独有这个工具:scripts.sql 已不是 grmp_register 的输出——它在 main 上被手改成行方的
DML 形态(单层 grmp.script_config、id 走 script_config_seq.nextval、create_user 填工号、多一列 uuid),
而生成器 grmp_mock/dml.py 仍出老形态;重新生成会把行方要的形态抹掉。改了 registry 之后只能
按这份的形态逐条追加。本工具就是「逐条追加」的确定性版本:SQL 正文与 parameter_config 直接取自
registry(ScriptRecord.parameter_config 与现场样例逐字一致),其余列照抄同族既有行。

用法:
    python3 tools/append_delivery_rows.py explain.kernel_funcs vacuum.kernel_info ...
    python3 tools/append_delivery_rows.py --replace sqltune.plan_text         # 正文 / 参数变了,按 registry 重写这一条
    python3 tools/append_delivery_rows.py --sync-routing                      # 全部行的 database_type 与 parameter_config 对齐 registry

database_type 一律取自 registry(2026-11-11 起 YAML 是唯一事实源,见 common/grmp/routing.py):以前各工具按自己的默认值写
(增量写死 appbusiness、这里默认 postgres、替换时沿用旧行),交付全量与增量有 8 条对不上,52 条 appbusiness 里 6 条读 SQL 历史 /
WDR 快照 —— 行方中间件按 dbName 切库后,这 6 条切到业务库就查不到。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.grmp.script import load_script  # noqa: E402

# 交付 DML 在仓库根的 docs/delivery/(本仓库);技能包单独成仓时在 agent 根下
DELIVERY = next((p for p in (ROOT.parent / "docs" / "delivery" / "scripts.sql", ROOT / "docs" / "delivery" / "scripts.sql")
                 if p.is_file()), ROOT.parent / "docs" / "delivery" / "scripts.sql")
REGISTRY = ROOT / "scripts" / "registry"

_COLS = ("id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, "
         "deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, "
         "scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "
         "\"extend\", compliance_mode, uuid")


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _record(name: str):
    return load_script(REGISTRY / name.split(".", 1)[0] / (name.split(".", 1)[1] + ".yaml"))


def _row(name: str, create_user: str) -> str:
    rec = _record(name)
    return (f"INSERT INTO grmp.script_config ({_COLS}) VALUES (grmp.script_config_seq.nextval, 'SQL', "
            f"{_q(rec.script_name)}, {_q(rec.database_type)}, 1, 'ALL', NULL, NULL, NULL, 'centralization', "
            f"{_q(rec.script_content)}, {_q(rec.parameter_config)}, 'AGENT', 1, {_q(create_user)}, now(), "
            f"{_q(create_user)}, NULL, 0, NULL, 'ALL', uuid());\n")


def sync_routing(text: str):
    """原地把每一行的 database_type 与 parameter_config 改成 registry 的值(行序、其余列不动)。返回 (新文本, 改了的脚本名)。"""
    blocks = re.split(r"(?=^INSERT INTO grmp\.script_config )", text, flags=re.M)
    out, changed = [], []
    for b in blocks:
        m = re.search(r"'SQL', '([a-z_]+\.[a-z_0-9]+)', '([a-z]+)'", b)
        if not m:
            out.append(b)
            continue
        rec = _record(m.group(1))
        nb = b[:m.start(2)] + rec.database_type + b[m.end(2):]
        p = re.search(r"'centralization', '.*?', '(\[.*?\])', 'AGENT'", nb, re.S)
        nb = nb[:p.start(1)] + rec.parameter_config.replace("'", "''") + nb[p.end(1):]
        if nb != b:
            changed.append(rec.script_name)
        out.append(nb)
    return "".join(out), changed


def _existing_names(text: str) -> set:
    return set(re.findall(r"VALUES \([^,]+, '[A-Z]+', '([a-z_]+\.[a-z_0-9]+)'", text))


def _drop_rows(text: str, name: str) -> str:
    """删掉该脚本已有的 INSERT 块(块从行首 INSERT 开始、到下一个行首 INSERT 之前;SQL 正文跨多行)。
    按块切再按名过滤,不用跨块的非贪婪正则——那种写法会从上一块的 INSERT 一路吞到本块。"""
    blocks = re.split(r"(?=^INSERT INTO grmp\.script_config )", text, flags=re.M)
    marker = re.compile(r"'SQL', '" + re.escape(name) + r"'")
    return "".join(b for b in blocks if not marker.search(b))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--create-user", default="999999999")
    ap.add_argument("--replace", action="store_true",
                    help="脚本已存在时先删旧行再按 registry 现状重写(SQL 正文或参数变了用这个)")
    ap.add_argument("--sync-routing", action="store_true",
                    help="全部行的 database_type 与 parameter_config 原地对齐 registry")
    args = ap.parse_args(argv)

    text = DELIVERY.read_text(encoding="utf-8")
    if args.sync_routing:
        text, changed = sync_routing(text)
        DELIVERY.write_text(text, encoding="utf-8")
        print(f"对齐 {len(changed)} 条的 database_type / parameter_config")
        if not args.names:
            return 0
    have = _existing_names(text)
    added = []
    for name in args.names:
        if name in have and args.replace:
            text = _drop_rows(text, name)
            print(f"替换(先删旧行):{name}")
        elif name in have:
            print(f"跳过(已存在):{name}")
            continue
        text += _row(name, args.create_user)
        added.append(name)
    DELIVERY.write_text(text, encoding="utf-8")
    print(f"追加 {len(added)} 条:{', '.join(added) or '无'};现共 {len(_existing_names(text))} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
