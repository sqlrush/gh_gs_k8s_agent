"""把注册好的白名单导成可读的 Markdown。

script_config.db 是 SQLite，GitHub 上只显示成二进制、点不开。而这份清单
恰恰是最该被人看见的东西 —— 客户要照着它做变更评审，DBA 要知道 agent
到底能跑哪些 SQL。所以额外导一份文本。

用法：
    python3 -m grmp_middleware.dump_whitelist \\
        --db ~/.gdaa/grmp/script_config.db \\
        --out docs/delivery/whitelist.md
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

DEFAULT_DB = "~/.gdaa/grmp/script_config.db"
DEFAULT_OUT = "docs/delivery/whitelist.md"


def _params(raw: str) -> str:
    """parameter_config 是 JSON 串，渲染成一行行的表。"""
    if not raw:
        return "无参数"
    try:
        items = json.loads(raw)
    except ValueError:
        return "（parameter_config 解析失败：%s）" % raw[:80]
    if not items:
        return "无参数"
    lines = ["| 参数 | 类型 |", "|---|---|"]
    for it in items:
        lines.append("| `%s` | %s |" % (it.get("key", "?"), it.get("type", "?")))
    return "\n".join(lines)


def render(db_path: pathlib.Path) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT * FROM script_config ORDER BY CAST(id AS INTEGER)"))

    # script_local 存 readonly 声明 —— 客户的 21 列里没有这个字段，
    # 是本实现额外要求脚本作者表态的（见 script.py）
    local = {}
    try:
        for r in conn.execute("SELECT script_name, readonly FROM script_local"):
            local[r["script_name"]] = r["readonly"]
    except sqlite3.OperationalError:
        pass
    conn.close()

    out = [
        "# GRMP 白名单脚本清单",
        "",
        "由 `grmp_middleware/fixtures/script_config.db` 导出（那是 SQLite，",
        "GitHub 上点不开）。**这份清单就是 agent 在客户环境能执行的全部 SQL**",
        "—— 白名单之外的一条都递不进去。",
        "",
        "重新生成：",
        "",
        "```bash",
        "python3 -m grmp_middleware.dump_whitelist",
        "```",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 脚本总数 | %d |" % len(rows),
        "| id 范围 | %s ~ %s |" % (rows[0]["id"], rows[-1]["id"]) if rows else "| id 范围 | — |",
        "",
        "> `id` 是**环境相关数据，不是契约**。skill 从不持有它 —— 运行时调",
        "> 接口一按 `cmd_name` 现查。客户环境重新发布后 id 会不同，属正常。",
        "",
        "## 按命名空间",
        "",
    ]

    by_ns = {}
    for r in rows:
        by_ns.setdefault(r["script_name"].split(".", 1)[0], []).append(r)

    out.append("| 命名空间 | 条数 | 脚本 |")
    out.append("|---|---|---|")
    for ns in sorted(by_ns):
        names = ", ".join("`%s`" % r["script_name"].split(".", 1)[1]
                          for r in by_ns[ns])
        out.append("| **%s** | %d | %s |" % (ns, len(by_ns[ns]), names))
    out.append("")
    out.append("---")
    out.append("")
    out.append("## 全部脚本")
    out.append("")

    for r in rows:
        name = r["script_name"]
        ro = local.get(name)
        ro_txt = {1: "只读", 0: "可写", None: "未声明"}.get(ro, str(ro))
        out += [
            "### `%s`" % name,
            "",
            "- id `%s` · 类型 `%s` · 会话 **%s** · is_valid `%s` · 异步 `%s`"
            % (r["id"], r["script_type"], ro_txt, r["is_valid"], r["is_asyn"]),
            "",
            _params(r["parameter_config"]),
            "",
            "```sql",
            (r["script_content"] or "").strip(),
            "```",
            "",
        ]

    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出白名单为 Markdown")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    db = pathlib.Path(args.db).expanduser()
    if not db.is_file():
        print("库不存在：%s" % db, file=sys.stderr)
        return 2

    out = pathlib.Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    text = render(db)
    out.write_text(text, encoding="utf-8")
    print("已写出：%s（%d 行）" % (out, text.count("\n")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
