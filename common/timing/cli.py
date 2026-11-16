#!/usr/bin/env python3
"""交互耗时报告:一次提问里每一步花了多长时间、慢在哪(v1.4.14)。纯脚本统计,不调模型、不联网、不改任何数据。

在智能体所在的机器 / 容器里运行(读本机 opencode 会话库与 GRMP 计时日志):

    python3 $GSDB_SKILLS_DIR/common/timing/cli.py                       # 最近 1 次提问,文本摘要打到终端
    python3 $GSDB_SKILLS_DIR/common/timing/cli.py --last 3 --html /tmp/耗时报告.html
    python3 $GSDB_SKILLS_DIR/common/timing/cli.py --since "2026-11-16 14:00" --until "2026-11-16 15:00" --html r.html
    python3 $GSDB_SKILLS_DIR/common/timing/cli.py --list                # 列出最近的提问,挑时间段用
    加 --detail 出详细版:带思考原文、命令参数与工具输出(令牌、会话句柄、IP 自动遮掉),只在内部查看,不要外传。

会话数据:默认找 $XDG_DATA_HOME/opencode 或 ~/.local/share/opencode(新版 opencode.db、旧版 storage/ 都认),或 --db 指定。
GRMP 计时:默认读技能状态目录下的 logs/grmp-timing.log(v1.4.14 起技能自动写),或 --grmp-log 指定;没有时工具时间不拆分。
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))     # 技能目录:让 common 可导入

from common.timing import analysis, grmplog, render, store  # noqa: E402


def _tz(offset: float) -> datetime.timezone:
    return datetime.timezone(datetime.timedelta(hours=offset))


def _when(text: str, tz: datetime.tzinfo) -> int:
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(text, f).replace(tzinfo=tz).timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit("时间格式不对:%r(写成 2026-11-16 14:00 或 2026-11-16 14:00:00)" % text)


def _list(sessions, tz) -> str:
    rows = []
    for s in sessions:
        if s.parent:
            continue
        for _, m, ps in s.messages:
            if m.get("role") == "user":
                txt = " ".join(p.get("text", "") for _, _, p in ps if p.get("type") == "text").strip().replace("\n", " ")
                when = datetime.datetime.fromtimestamp(m["time"]["created"] / 1000, tz).strftime("%Y-%m-%d %H:%M:%S")
                rows.append((m["time"]["created"], "%s  会话 %s  %s" % (when, s.id[:16], txt[:40])))
    return "\n".join(r for _, r in sorted(rows)[-30:]) or "(没有提问记录)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="交互耗时报告(纯脚本统计,不调模型)")
    ap.add_argument("--db", help="opencode.db 或 opencode 数据目录(默认自动找)")
    ap.add_argument("--grmp-log", help="GRMP 计时日志(默认技能状态目录下 logs/grmp-timing.log)")
    ap.add_argument("--last", type=int, default=1, help="最近几次提问(默认 1)")
    ap.add_argument("--since", help="起始时间,如 2026-11-16 14:00(给了时间段就忽略 --last)")
    ap.add_argument("--until", help="截止时间")
    ap.add_argument("--session", help="只看这个会话(会话 id 或前缀)")
    ap.add_argument("--html", help="HTML 报告写到这个文件")
    ap.add_argument("--text", help="文本报告写到这个文件(不给 --html 也不给 --text 时打到终端)")
    ap.add_argument("--detail", action="store_true", help="详细版:带思考原文、命令参数与工具输出(只在内部查看)")
    ap.add_argument("--list", action="store_true", help="列出最近 30 次提问")
    ap.add_argument("--utc-offset", type=float, default=8, help="显示用的时区(相对 UTC 的小时数,默认 8 = 北京时间)")
    a = ap.parse_args(argv)
    tz = _tz(a.utc_offset)
    try:
        src = store.locate(a.db)
        sessions = store.load(src)
    except store.StoreError as exc:
        print(exc, file=sys.stderr)
        return 2
    if a.list:
        print(_list(sessions, tz))
        return 0
    gpath = pathlib.Path(a.grmp_log).expanduser() if a.grmp_log else grmplog.default_path()
    grmp = grmplog.load(str(gpath))
    qs, masked = analysis.questions(sessions, a.detail, grmp, tz, a.session,
                                    _when(a.since, tz) if a.since else None, _when(a.until, tz) if a.until else None, a.last)
    if not qs:
        print("没有符合条件的提问(会话数据:%s)。用 --list 看看有哪些。" % src, file=sys.stderr)
        return 1
    meta = {"detail": a.detail, "generated": datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
            "source": "%s(%s)" % (src, "SQLite" if src.is_file() else "JSON 存储"),
            "grmp_source": "%s,%d 条" % (gpath, len(grmp)) if grmp else "没有记录(%s 不存在或为空)" % gpath,
            "has_grmp": bool(grmp), "masked": masked,
            "masked_text": "、".join("%s %d 处" % kv for kv in masked.items()), "utc_offset": a.utc_offset}
    txt = render.text(meta, qs)
    if a.html:
        pathlib.Path(a.html).write_text(render.html(meta, qs, txt), encoding="utf-8")
        print("HTML 报告:%s(%d 次提问)" % (a.html, len(qs)))
    if a.text:
        pathlib.Path(a.text).write_text(txt + "\n", encoding="utf-8")
        print("文本报告:%s" % a.text)
    if not a.html and not a.text:
        print(txt)
    else:
        for q in qs:
            print("第 %d 问 %s 总 %s · %s" % (q["idx"], q["start"], analysis.fmt(q["total"]), q["diag"][0]["title"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
