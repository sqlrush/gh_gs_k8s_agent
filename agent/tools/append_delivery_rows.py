"""把 registry 里指定脚本按**客户真实 DML 形态**追加进 docs/delivery/scripts.sql。

为什么单独有这个工具:scripts.sql 已不是 grmp_register 的输出——它在 main 上被手改成客户的
DML 形态(单层 grmp.script_config、id 走 script_config_seq.nextval、create_user 填工号、多一列 uuid),
而生成器 grmp_mock/dml.py 仍出老形态;重新生成会把客户要的形态抹掉。改了 registry 之后只能
按这份的形态逐条追加。本工具就是「逐条追加」的确定性版本:SQL 正文与 parameter_config 直接取自
registry(ScriptRecord.parameter_config 与客户样例逐字一致),其余列照抄同族既有行。

用法:
    python3 tools/append_delivery_rows.py explain.kernel_funcs vacuum.kernel_info ...
    python3 tools/append_delivery_rows.py --create-user 999999999 --database-type explain=appbusiness ...
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.grmp.script import load_script  # noqa: E402

DELIVERY = ROOT / "docs" / "delivery" / "scripts.sql"
REGISTRY = ROOT / "scripts" / "registry"

_COLS = ("id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, "
         "deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, "
         "scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "
         "\"extend\", compliance_mode, uuid")


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _row(name: str, create_user: str, database_type: str) -> str:
    path = REGISTRY / name.split(".", 1)[0] / (name.split(".", 1)[1] + ".yaml")
    rec = load_script(path)
    return (f"INSERT INTO grmp.script_config ({_COLS}) VALUES (grmp.script_config_seq.nextval, 'SQL', "
            f"{_q(rec.script_name)}, {_q(database_type)}, 1, 'ALL', NULL, NULL, NULL, 'centralization', "
            f"{_q(rec.script_content)}, {_q(rec.parameter_config)}, 'AGENT', 1, {_q(create_user)}, now(), "
            f"{_q(create_user)}, NULL, 0, NULL, 'ALL', uuid());\n")


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
    ap.add_argument("names", nargs="+")
    ap.add_argument("--create-user", default="999999999")
    ap.add_argument("--database-type", action="append", default=[],
                    help="按前缀指定 database_type,如 explain=appbusiness;默认 postgres")
    ap.add_argument("--replace", action="store_true",
                    help="脚本已存在时先删旧行再按 registry 现状重写(SQL 正文或参数变了用这个)")
    args = ap.parse_args(argv)
    dbtype = dict(kv.split("=", 1) for kv in args.database_type)

    text = DELIVERY.read_text(encoding="utf-8")
    have = _existing_names(text)
    added = []
    for name in args.names:
        if name in have and args.replace:
            text = _drop_rows(text, name)
            print(f"替换(先删旧行):{name}")
        elif name in have:
            print(f"跳过(已存在):{name}")
            continue
        prefix = name.split(".", 1)[0]
        text += _row(name, args.create_user, dbtype.get(prefix, "postgres"))
        added.append(name)
    DELIVERY.write_text(text, encoding="utf-8")
    print(f"追加 {len(added)} 条:{', '.join(added) or '无'};现共 {len(_existing_names(text))} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
