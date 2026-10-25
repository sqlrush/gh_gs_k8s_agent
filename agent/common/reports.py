"""报告存档 —— 技能跑完把 JSON 留在本人 NAS 目录,大盘从这里读。

只在设了 GSDB_REPORTS_DIR 时工作(容器里 entrypoint 设成 $NAS_ME/reports);
没设就什么都不做,非容器线的行为一字不变。

**存档失败不能拖垮技能。** 报告已经打到 stdout 了,存档只是附加动作:
失败打一行 warn 让人知道大盘会缺这一份,然后照常返回。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import sys
import urllib.parse
from typing import Any, Dict, List, Optional

ENV_DIR = "GSDB_REPORTS_DIR"
LATEST = "latest.json"
INDEX = "index.json"
TARGETS = "targets.json"
DEFAULT_KEEP = 50
_KEY_BAD = re.compile(r"[^A-Za-z0-9._-]")


# 说明类字段:采集失败的原因、维度一句话、子技能报错、证据说明。只在这几类字段里隐去地址,
# SQL 文本、表格单元格一律不动 —— 那里的 `a/b/c`、'http://x' 是用户的数据。
_PROSE_KEYS = frozenset({"note", "headline", "error", "evidence", "reason"})
_URL = re.compile(r"(?:https?|jdbc|grpc)://[^\s（）()，,；;、\"'<>]+", re.I)
_ENDPOINT = re.compile(r"(?<![\w.])/(?:[A-Za-z0-9_-]+/){2,}[A-Za-z0-9_.-]*")


def redact(text: str) -> str:
    """红线第 5 条:中间件 / 模型服务的地址与内部接口路径是机密。大盘没有模型那道输出自检,
    存档时就隐去。只动说明文字,不动数据。"""
    return _ENDPOINT.sub("[接口已隐去]", _URL.sub("[地址已隐去]", text))


def _redact_prose(obj: Any, key: str = "") -> Any:
    if isinstance(obj, dict):
        return {k: _redact_prose(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_prose(v, key) for v in obj]
    if isinstance(obj, str) and key in _PROSE_KEYS:
        return redact(obj)
    return obj


def instance_key(conn: str) -> str:
    """实例键:conn 规整成能当路径段的串。

    三个大盘(健康 / Top SQL / WDR)都是按库统计的,报告必须按实例分目录 —— 否则一个人上午
    看 A 库下午看 B 库,「最近 12 次巡检」会把两个库串成一条线。容器里 conn 形如
    api/10-0-0-9-postgres(会话句柄解析出来的,已含 dataIp 与库名),直连模式是配置里的连接名。
    """
    key = _KEY_BAD.sub("_", (conn or "").strip())
    return key or "_default"


def utc_stamp(now: Optional[str] = None) -> str:
    return now or _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def reports_dir() -> Optional[pathlib.Path]:
    raw = (os.environ.get(ENV_DIR) or "").strip()
    return pathlib.Path(raw).expanduser() if raw else None


def _warn(msg: str) -> None:
    print("[warn] 报告未存档:%s" % msg, file=sys.stderr)


def _write_atomic(path: pathlib.Path, text: str) -> None:
    """同 common/kb/atomic.py:读的一方(Pod 内只读端口)任何时刻拿到的都是完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _counts(payload: Any) -> Dict[str, int]:
    out: Dict[str, int] = {}
    findings = payload.get("findings") if isinstance(payload, dict) else None
    for f in findings or []:
        sev = str(f.get("severity")) if isinstance(f, dict) else None
        if sev is not None:
            out[sev] = out.get(sev, 0) + 1
    return out


def _dims_summary(payload: Any) -> Dict[str, Any]:
    """index 里记下这份报告有几个维度、几个不可用。

    健康检查全部采集失败时 overall 是 0(没有发现),历史条若只看 overall 就画成绿色「正常」——
    用户会以为库是健康的。有了这两个数,大盘能把「没采到」和「正常」分开画。
    """
    dims = payload.get("dims") if isinstance(payload, dict) else None
    if not isinstance(dims, list):
        return {}
    na = sum(1 for x in dims if isinstance(x, dict) and x.get("available") is False)
    return {"dims": {"total": len(dims), "na": na}}


def _window_summary(payload: Any) -> Dict[str, Any]:
    """评估窗口抄进索引:报告的 at 是**存档时间**,大盘按时间段筛选时要按窗口筛。

    WDR 一直有;时段体检与时段 Top SQL 也有(它们今天存档,说的却可能是昨天那一段)。
    `mode` 一并抄进去:大盘据此把「时段报告」与「此刻巡检」分开比较、分开画走势。
    """
    win = payload.get("window") if isinstance(payload, dict) else None
    mode = payload.get("mode") if isinstance(payload, dict) else None
    out = {"mode": mode} if isinstance(mode, str) and mode else {}
    if not isinstance(win, dict) or not win.get("begin_ts"):
        return out
    return {**out, "window": {k: win.get(k) for k in ("begin_id", "end_id", "begin_ts", "end_ts")}}


def _dbtime_summary(payload: Any) -> Dict[str, Any]:
    """WDR 的 DB time 抄进索引,让大盘能画「窗口间 DB time 趋势」。

    为什么要进索引:大盘画趋势只读 index.json(一份文件),不会把几十份报告全下载下来。
    索引里没有这个数,就只能画「发现条数」的趋势 —— 而用户想知道的是「是不是越来越卡」,
    发现条数回答不了那个问题(阈值没变,数量可以一直是 3)。

    与 _dims_summary / _window_summary 同一个做法:只认已知的维度名与列名,取不到就不写这一项。
    """
    dims = payload.get("dims") if isinstance(payload, dict) else None
    if not isinstance(dims, list):
        return {}
    for d in dims:
        if not isinstance(d, dict) or d.get("dimension") != "Load Profile":
            continue
        headers, rows = d.get("headers") or [], d.get("rows") or []
        if not headers or not rows or not isinstance(rows[0], list):
            return {}
        try:
            i = next(k for k, h in enumerate(headers) if str(h).startswith("DB time"))
            return {"db_time_s": float(rows[0][i])}
        except (StopIteration, IndexError, TypeError, ValueError):
            return {}
    return {}


def _load_index(path: pathlib.Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _file_gone(d: pathlib.Path, entry: Dict[str, Any]) -> bool:
    """index 里登记的这份文件确实不在了。

    2026-10-24 测试环境:NAS 放在 Mac 的 /tmp 下,macOS 每天 0 点的 tmp_cleaner 删掉三天没碰过的文件 ——
    报告写一次就不再动,被删了;index.json 每次存档都重写,留了下来。大盘按索引取最新一份,整页 404。
    文件被人手删、备份恢复不全也是同一个样子,所以存档时顺手把这种条目清掉。
    **只认「文件不存在」**:权限、NFS 抖动这类读不准的,当它还在 —— 宁可索引里多一条,也不能误删登记。
    """
    try:
        os.stat(d / str(entry.get("file")))
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


def describe_instance(conn: str) -> Dict[str, str]:
    """侧栏显示的实例名与深挖首句里的登录说法。

    conn 形如 api/10-0-0-9-postgres,用户认不出来;深挖开的新会话还没登录,首句不说是哪个库,
    模型只能反过来问。红线第 5 条允许出现用户自己连接的实例 IP 与库名。

    解析不到、或解析出来的不是这个 conn(比如会话已经切到别的库),就退回 conn 本身、不给登录说法 ——
    宁可让模型去问,也不能把 A 库的名字挂到 B 库的报告上。
    """
    try:
        from common import config
    except ImportError:
        return {"label": conn, "login": ""}
    c = None
    for cand in (None, conn):
        try:
            got = config.resolve(cand)
        except Exception:        # 没登录、没配置、名字不认识 —— 都退回
            continue
        if got.qualified == conn:
            c = got
            break
    if c is None:
        return {"label": conn, "login": ""}
    if c.data_ip:
        return {"label": "%s / %s" % (c.data_ip, c.database),
                "login": "登录 %s 的 %s 库" % (c.data_ip, c.database)}
    return {"label": "%s / %s" % (c.qualified, c.database), "login": "用连接 %s 登录" % c.qualified}


def _update_targets(skill_dir: pathlib.Path, key: str, conn: str, stamp: str, count: int) -> None:
    """<skill>/targets.json:这个人跑过哪些实例、各自最近一次是什么时候 —— 大盘靠它切实例。"""
    path = skill_dir / TARGETS
    items = [t for t in _load_index(path) if t.get("key") != key]
    items.append({"key": key, "conn": conn, "last_at": stamp, "count": count, **describe_instance(conn)})
    items.sort(key=lambda t: t.get("last_at", ""), reverse=True)
    _write_atomic(path, json.dumps(items, ensure_ascii=False, indent=2))


def archive(skill: str, payload: Any, *, name: Optional[str] = None,
            keep: int = DEFAULT_KEEP, now: Optional[str] = None,
            instance: Optional[str] = None, latest: bool = True) -> Optional[pathlib.Path]:
    """写 <dir>/<skill>/[<实例键>/]<name or 时间戳>.json,同时覆盖 latest.json、维护 index.json。

    latest=False:不覆盖 latest.json(只进 index)。时段报告用它 —— 大盘不带参数打开时显示 latest,
    那应该是最近一次「此刻」巡检;让一份「昨天 10:46–11:06」的时段报告顶掉它,用户会以为库现在就这样。

    instance 给了就分实例目录并维护 <skill>/targets.json;不给(知识库,全体共享一份)则落在技能根目录。
    index 只登记本函数写过的文件,超过 keep 份时也只删登记过的 —— 目录里别的东西不动。
    登记着、文件却已不在的条目(见 _file_gone)这次一并从 index 清掉,并在标准错误说一句。
    """
    base = reports_dir()
    if base is None:
        return None
    stamp = utc_stamp(now)
    fname = (name or stamp) + ".json"
    d = base / skill
    key = instance_key(instance) if instance is not None else ""
    if key:
        d = d / key
    payload = _redact_prose(payload)
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        _write_atomic(d / fname, text)
        if latest:
            _write_atomic(d / LATEST, text)
        before = [e for e in _load_index(d / INDEX) if e.get("file") != fname]
        idx = [e for e in before if not _file_gone(d, e)]
        if len(idx) < len(before):
            print("[注意] %s:索引里有 %d 份报告的文件已不在(可能被清理过),已从大盘索引移除。"
                  % (skill, len(before) - len(idx)), file=sys.stderr)
        idx.append({"at": stamp, "file": fname,
                    "overall": payload.get("overall") if isinstance(payload, dict) else None,
                    "counts": _counts(payload), **_dims_summary(payload), **_window_summary(payload),
                    **_dbtime_summary(payload)})
        idx.sort(key=lambda e: e.get("at", ""))
        for old in idx[:-keep] if keep > 0 else []:
            try:
                (d / str(old.get("file"))).unlink()      # 只删 index 里登记过的
            except OSError:
                pass
        idx = idx[-keep:] if keep > 0 else idx
        _write_atomic(d / INDEX, json.dumps(idx, ensure_ascii=False, indent=2))
        if key:
            _update_targets(base / skill, key, instance or "", stamp, len(idx))
        return d / fname
    except (OSError, TypeError, ValueError) as exc:
        _warn("%s/%s:%s" % (skill, fname, exc))
        return None


# 有大盘页的技能 → 页面名。调优没有自己的页(出现在 Top SQL 的明细里),知识库不分实例。
# 知识库(kb)全体共享一份、不分实例,大盘上就是一个页面 /dash/kb;以前漏了它,kb.py health 只存 health.json
# 不给链接,模型只能说「入口在大盘页」(2026-10-24 用户测试)。
DASH_PAGES = {"health": "health", "topsql": "topsql", "wdr": "wdr", "kb": "kb"}


def dash_link(skill: str, path: Optional[pathlib.Path], instance: str, by: str = "",
              span: Optional[tuple] = None) -> str:
    """这一份报告在大盘上的直达地址(相对路径:大盘与对话页在同一个网关地址下)。没存档 / 没有页 → 空串。

    health / wdr 带 run=<文件> 直接打开这一份;topsql 按维度页签各取最近一份,带 by=<维度>。
    topsql 的时段 / 增量报告(文件名 `<时间戳>.<维度>.span|delta.json`)也带 run= —— 页签只取累计榜,
    不带 run= 的话用户点开看到的是另一份累计榜,不是刚才那张时段榜。

    **span=(from, to) 时额外带上时间段**(本地时间字符串,形如 `2026-10-17T10:46`)。
    用户说「生成某时间段的报告」时,给出的链接应该**已经筛在那个时间段上** ——
    否则用户点开看到的是全部历史,还得自己再去顶栏选一次时间段,而他刚刚已经说过了。
    """
    page = DASH_PAGES.get(skill)
    if path is None or page is None:
        return ""
    if skill == "kb":                       # 不分实例、不分哪一次:页面读的就是最新那份 health.json
        return "/dash/kb"
    q = [("inst", instance_key(instance))]
    if skill != "topsql" or path.name.count(".") >= 3:
        q.append(("run", path.name))
    if skill == "topsql" and by:
        q.append(("by", by))
    if span:
        a, b = (str(x or "").strip() for x in span)
        if a and b:
            q.extend([("from", a), ("to", b)])
    return "/dash/%s?%s" % (page, urllib.parse.urlencode(q))


# 大盘的对外地址。**只有部署方知道**用户是从哪个地址访问的(端口转发、Ingress、反代各不相同),
# 所以由部署注入;没注入时退回根相对地址 —— 它在对话页同源下照样打得开。
ENV_DASH_BASE = "GSDB_DASH_BASE_URL"


def dash_url(link: str) -> str:
    """相对地址 → 用户能直接打开的地址。

    **为什么需要它。** `dash_link` 给的是 `/dash/wdr?inst=…` 这样的根相对地址。
    把这一行原样贴进聊天记录,用户拿到的是一个**没有站点的路径** —— 点不开,
    复制到浏览器地址栏也打不开(实测:用户要「这个时间段的 WDR 报告」,
    拿到 `/dash/wdr?...` 之后无法跳转)。
    注入了 `GSDB_DASH_BASE_URL` 就拼成完整地址;没注入就保留相对地址,
    由下面的 Markdown 链接兜底 —— 对话页与大盘同源,相对地址点开就是对的那一份。
    """
    base = (os.environ.get(ENV_DASH_BASE) or "").strip().rstrip("/")
    if not link:
        return ""
    return base + link if base else link


def announce(skill: str, path: Optional[pathlib.Path], instance: str, by: str = "",
             span: Optional[tuple] = None) -> None:
    """存档成功后在**标准错误**打一行大盘直达链接,**已经是 Markdown 链接形态**。

    以前存档悄无声息,模型不知道有大盘(用户要「大盘」时它只能自己拼 Markdown)。走标准错误:
    JSON 模式的 stdout 必须是纯 JSON,WDR 的最终报告必须是 render 的原样输出,都不能多一行。

    **给的是 `[文字](地址)` 而不是裸地址**:裸的根相对路径贴进聊天记录是点不开的,
    而 Markdown 链接由对话页按同源解析,点一下就到那一份报告 —— 用户要的就是「能跳过去」。
    """
    link = dash_link(skill, path, instance, by, span)
    if not link:
        return
    md = "[%s](%s)" % ("打开知识库大盘" if skill == "kb" else "打开报告", dash_url(link))
    print("已存入大盘:%s —— 这是给用户看的页面,**把这个 Markdown 链接原样放进回答里**"
          "(不要只写路径,裸路径用户点不开)。" % md, file=sys.stderr)


def append_jsonl(skill: str, name: str, row: Dict[str, Any]) -> bool:
    base = reports_dir()
    if base is None:
        return False
    path = base / skill / (name + ".jsonl")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError) as exc:
        _warn("%s/%s.jsonl:%s" % (skill, name, exc))
        return False
