"""kb 网页剪藏子命令(只在导入环境,2026-10-27):需要登录的内网网页,由管理员在浏览器里点书签剪藏进来。

    clip status [--url <链接>] [--json]   剪藏开没开、允许哪些网站、安装页链接、本人剪藏过没有、某链接在不在名单里
    clip list                             剪藏批次(最近的在前)
    clip ingest [<批次>]                  批次 → page.md(找正文、去噪、保留结构);不给批次 = 本人最近一次
    clip add <文件> [--url <原网页地址>]  浏览器另存的 .mhtml / .html → 剪藏批次(没有剪藏页、或书签用不了时)
    clip clean <批次>... [--discard]      处理完后删掉批次目录(还没导入的要加 --discard)

分工:浏览器负责「读得到」(书签在已登录的页面里取 DOM 与图片),导入环境的接收服务落盘,
本脚本把网页变成干净的 md;之后由模型按材料类型走现有流程(工单 → kb-init 六要素;规范 → ingest;
其余 → 八类模板),原始快照 attach 成 L3 证据。模型不访问链接 —— 它没有联网能力,也带不上用户的登录状态。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import shutil
import sys
from typing import Dict, List

from common.kb import config as kbconfig
from common.kb import webclip

INSTALL_PATH = "/kb-import/clip/install"


class ClipCmdError(Exception):
    pass


def _kb(args: argparse.Namespace) -> pathlib.Path:
    kb = kbconfig.resolve_kb_dir(getattr(args, "kb", None))
    if not kb.is_dir():
        raise ClipCmdError(f"知识库目录不存在:{kb}")
    return kb


def _kb_init_script() -> pathlib.Path:
    skills = pathlib.Path(__file__).resolve().parents[1]
    return skills / "gaussdb-kb-init" / "scripts" / "kb_init.py"


def _when(meta: Dict) -> str:
    raw = str(meta.get("fetched_at") or meta.get("received_at") or "")
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw[:16]


def _state(batch: pathlib.Path) -> str:
    return "已转成 md" if (batch / webclip.PAGE_MD).is_file() else "未导入"


def status_info(kb: pathlib.Path, url: str = "") -> Dict[str, object]:
    origins = webclip.allowed_origins(os.environ.get("KB_CLIP_ORIGINS", ""))
    user = os.environ.get("GSDB_USER_ID", "")
    mine = [(p, m) for p, m in webclip.list_batches(kb) if not user or m.get("user_id") in (user, "", None)]
    info: Dict[str, object] = {
        "enabled": bool(origins),
        "origins": origins,
        "receiver": os.environ.get("KB_CLIP_RECEIVER") == "1",
        "install_url": INSTALL_PATH,
        "user_id": user,
        "my_clips": len(mine),
        "latest": ({"batch": mine[0][0].name, "title": mine[0][1].get("title"), "url": mine[0][1].get("url"),
                    "at": _when(mine[0][1]), "state": _state(mine[0][0])} if mine else None),
    }
    if url:
        o = webclip.origin_of(url.strip())
        info["url"] = {"url": url, "origin": o, "allowed": bool(o) and o.lower() in origins}
    return info


def cmd_status(args: argparse.Namespace) -> int:
    info = status_info(_kb(args), args.url or "")
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if info["enabled"]:
        print("剪藏        : 已开启(允许的网站 %d 个:%s)" % (len(info["origins"]), "、".join(info["origins"])))
    else:
        print("剪藏        : **未开启** —— 平台要在 agent-config 里配 KB_CLIP_ORIGINS(允许剪藏的内网网站,"
              "如 https://wiki.内网),改完重启网关与导入环境")
    print("接收服务    : %s" % ("在运行" if info["receiver"] else "没有(单机版没有剪藏页;容器版是导入镜像太旧,请平台升级)"))
    if not (info["enabled"] and info["receiver"]):
        print("另一条路    : 浏览器「另存为 → 网页,单个文件(.mhtml)」,上传到收件目录后 kb.py clip add <文件>,再 clip ingest")
    print("安装书签    : [打开安装页](%s)(把页面上的「导入知识库」拖到书签栏)" % info["install_url"])
    if info["latest"]:
        lt = info["latest"]
        print("你的剪藏    : %d 次;最近一次 %s 「%s」(%s,%s)" % (info["my_clips"], lt["at"], lt["title"] or lt["url"],
                                                          lt["batch"], lt["state"]))
    else:
        print("你的剪藏    : 还没有(第一次用要先装书签,按 SKILL.md「网页剪藏」一步一步引导)")
    if "url" in info:
        u = info["url"]
        if not u["origin"]:
            print("这个链接    : 不是 http(s) 地址:%s" % u["url"])
        elif u["allowed"]:
            print("这个链接    : %s 在允许名单里 —— 请用户在浏览器里打开它,点书签「导入知识库」" % u["origin"])
        else:
            print("这个链接    : %s **不在**允许名单里 —— 请平台把它加进 agent-config 的 KB_CLIP_ORIGINS" % u["origin"])
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = webclip.list_batches(_kb(args))
    if not rows:
        print("还没有剪藏批次")
        return 0
    for p, m in rows:
        imgs = len(m.get("images") or [])
        print("%s  %s  %-8s %s  %s(图片 %d)" % (p.name, _when(m), _state(p), m.get("user_id") or "-",
                                             m.get("title") or m.get("url") or m.get("file_name") or "", imgs))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    kb = _kb(args)
    try:
        batch = webclip.batch_path(kb, args.batch) if args.batch else webclip.latest(kb, os.environ.get("GSDB_USER_ID", ""))
        meta = webclip.load_meta(batch)
        res = webclip.convert(batch, kb)
    except webclip.ClipError as exc:
        raise ClipCmdError(str(exc)) from exc
    print("剪藏批次    : %s(%s)" % (batch.name, batch))
    print("来源        : 「%s」 %s(抓取 %s)" % (res.title or "无标题", res.url or meta.get("file_name") or "", _when(meta)))
    # 书签没取回的跨站图片既在跳过清单里,也是正文里指不到本地的图片:按地址去重,不要数两遍
    miss = len(set(res.images_missing) | {str(x.get("src") or "") for x in res.skipped if isinstance(x, dict)})
    print("正文        : %d 字 · %d 个小节 · %d 张表 · 图片 %d 张%s" % (
        res.chars, res.headings, res.tables, res.images_used,
        ("(另有 %d 张没取回:跨站或受限的图片读不到,需要时请用户截图上传)" % miss) if miss else ""))
    if res.changed_from:
        print("内容有更新  : 这个网页以前剪藏过(%s),这次内容不同 —— 已入库的条目要**按新版更新**(add --replace),不要另起一条"
              % res.changed_from)
    print("转出        : %s" % res.page_md)
    if res.images_used:
        print("页内图片    : %s/images/(截图里的报错、监控曲线需要时用 read 工具看图)" % batch)
    print()
    print("Next: 读 page.md,先判断这是什么材料(SKILL.md「网页剪藏」第 4 步),筛掉与数据库运维无关的内容:")
    print("  - 故障处理记录 / 事件复盘 / 工单页 → python3 %s scan %s(六要素标准化;一页多起事件拆成多单)"
          % (_kb_init_script(), res.page_md))
    print("  - 规范 / 制度 / 配置要求        → kb.py ingest %s(条款化)" % res.page_md)
    print("  - 手册 / 已知缺陷 / 基线 / 画像 / 产品文档 / Wiki → kb.py template <类型>,写草稿后 kb.py add")
    print("  入库后:kb.py attach %s --to <条目 ID>(原始网页存为 L3 证据);kb.py index;处理完 kb.py clip clean %s"
          % (res.snapshot, batch.name))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    kb = _kb(args)
    src = pathlib.Path(args.file).expanduser()
    if not src.is_file():
        raise ClipCmdError("文件不存在:%s(用户电脑上的文件要先通过对话界面上传到收件目录,再用沙箱内路径)" % src)
    try:
        batch = webclip.add_file(kb, src, os.environ.get("GSDB_USER_ID", ""), args.url or "")
    except webclip.ClipError as exc:
        raise ClipCmdError(str(exc)) from exc
    meta = webclip.load_meta(batch)
    print("剪藏批次    : %s(%s)" % (batch.name, batch))
    print("来源        : %s" % (meta.get("url") or "文件里没写原网页地址 —— 入库时出处请向用户问清,或重新加上 --url"))
    print()
    print("Next: python3 %s clip ingest %s" % (pathlib.Path(__file__).with_name("kb.py"), batch.name))
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    kb = _kb(args)
    bad = 0
    for raw in args.batches:
        try:
            batch = webclip.batch_path(kb, raw)
        except webclip.ClipError as exc:
            bad += 1
            print("[跳过 ] %s" % exc)
            continue
        if not (batch / webclip.PAGE_MD).is_file() and not args.discard:
            bad += 1
            print("[跳过 ] %s 还没导入(kb.py clip ingest %s);确认这次剪错了要丢弃,加 --discard" % (batch.name, batch.name))
            continue
        if batch.resolve().parent != webclip.clips_dir(kb).resolve():
            bad += 1
            print("[跳过 ] %s 不在剪藏目录下,不删" % batch)
            continue
        shutil.rmtree(batch)
        print("[已清理] inbox/clips/%s" % batch.name)
    return 2 if bad else 0


def add_subcommands(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("clip", help="网页剪藏:需要登录的内网网页由管理员在浏览器里点书签剪藏进来,再转成 md 导入")
    csub = p.add_subparsers(dest="clip_cmd", required=True)
    q = csub.add_parser("status", help="剪藏开没开、允许的网站、安装页链接、本人剪藏过没有")
    q.add_argument("--url", help="用户给的链接:看它在不在允许名单里")
    q.add_argument("--json", action="store_true")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_status)
    q = csub.add_parser("list", help="剪藏批次")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_list)
    q = csub.add_parser("ingest", help="批次 → page.md;不给批次 = 本人最近一次")
    q.add_argument("batch", nargs="?")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_ingest)
    q = csub.add_parser("add", help="浏览器另存的 .mhtml / .html → 剪藏批次(没有剪藏页、或书签用不了时)")
    q.add_argument("file")
    q.add_argument("--url", help="原网页地址(文件里没记时补上,入库后作为出处)")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_add)
    q = csub.add_parser("clean", help="删掉处理完的剪藏批次")
    q.add_argument("batches", nargs="+")
    q.add_argument("--discard", action="store_true", help="还没导入的也删(这次剪错了)")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_clean)

