#!/usr/bin/env python3
"""Pod 生命周期里的确定性动作,entrypoint.sh 逐条调用。每个子命令对应 spec §6.2 的一项措施。

只用标准库:镜像里没有别的依赖也能跑;所有输出一句话说清发生了什么,不静默。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import sys
import time
from typing import Dict, List, Mapping, Optional

OWNER_NAME = ".owner"
# 版本令牌:任何模式下取得独占标记都换一个新的随机值(固定池化靠它判断保留在槽位里的副本还能不能用)
GEN_NAME = ".gen"
PROVIDER_DEFAULT = "model-service"


class ConfigError(ValueError):
    pass


def _utc_stamp(now: Optional[str] = None) -> str:
    return now or _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------- 渲染配置

def strip_jsonc(src: str) -> str:
    """去掉 // 与 /* */ 注释和尾逗号(字符串内不动),够解析模板用。"""
    out: List[str] = []
    i, n, in_str = 0, len(src), False
    while i < n:
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


# opencode 认的输入模态(capabilities.input 的键)
_MODALITIES = ("text", "image", "audio", "video", "pdf")


def input_modalities(env: Mapping[str, str]) -> Optional[list]:
    """MODEL_INPUT_MODALITIES(如 `text,image`)→ opencode 模型配置里的 modalities.input。不设 = None。

    2026-10-22 用户反馈:知识库导入读照片时 read 报「this model does not support image input」。
    模型本身能看图(deepseek-flash 实测答对红 / 蓝两张图),是配置里只写了模型名:opencode 认不出的模型
    默认按纯文本处理,在客户端就把图片拦下了。声明了 image,read 才把图片交给模型。
    默认不声明——部署样例的 qwen3-32b 不能看图,贸然打开会让读图请求在模型服务那边报错。
    写错的项当场报错并列出可选值,不悄悄忽略(忽略了就是又一次「读不了图、不知道为什么」)。
    """
    raw = (env.get("MODEL_INPUT_MODALITIES") or "").strip()
    if not raw:
        return None
    items = [x.strip().lower() for x in raw.replace(";", ",").split(",") if x.strip()]
    bad = [x for x in items if x not in _MODALITIES]
    if bad:
        raise ConfigError("MODEL_INPUT_MODALITIES=%r 里有不认识的项 %s;可选 %s,逗号分隔,如 text,image"
                          % (raw, "、".join(bad), "、".join(_MODALITIES)))
    return list(dict.fromkeys(["text"] + items))      # text 总要有;去重保序


# 工具权限(2026-10-23 落到 opencode 配置里)。技能包的 AGENTS.md 开头也写了一组 permission,但它是作为全局规则
# (提示词)装进去的,opencode 不执行那里的权限 —— 「不能改文件、不能联网、只能调预置技能」原来只靠提示词。
# 写进 opencode.json 才是 opencode 真执行的(1.18.27 实测这些键都认)。bash 规则**最后一条匹配的生效**,
# 复合命令(管道、&&、$())逐条核对,`*` 能匹配 `/` 与空格,所以:先全拒,再放行,最后把放行里的漏洞拒回去。
ROLE_RUNTIME, ROLE_KB_IMPORT = "runtime", "kb-import"
_READ_ONLY_COMMANDS = ("cat", "grep", "head", "tail", "ls", "wc", "sort", "uniq", "cut", "date", "echo", "pwd",
                       "ps", "df", "du", "free", "find", "cd", "stat", "file", "tree", "realpath", "basename",
                       "dirname", "which", "diff", "true")
# 无害的重定向后缀:丢弃输出或把错误并进标准输出。模型几乎每条命令都爱加 `2>/dev/null` / `2>&1`,
# 一律拒掉时它会白撞一次再去掉重试(2026-10-24 测试环境导入工单时连撞三次)。只给放行过的命令补这几种结尾;
# 一条命令里有两个以上 `>` 一律拒(`cat > x.py 2>/dev/null` 这类借安全结尾夹带写文件的)。
_SAFE_REDIRECTS = ("2>/dev/null", "2> /dev/null", "2>&1", ">/dev/null", "> /dev/null")
_BASH_DENY_FINAL = (
    "*>*>*",            # 两个以上重定向:借安全结尾夹带写文件
    "*/../../*",        # 借技能目录跳出去执行别处的文件(技能之间的同级引用只有一层 ../)
    "*/proc/*", "*environ*",            # 读别的进程 / 自己的环境(带了安全结尾也拒)
    # find 能执行命令、删文件、把结果写进文件
    "find *-exec*", "find *-execdir*", "find *-ok*", "find *-delete*", "find *-fprint*", "find *-fls*",
    # export / declare 写了也拦不住:opencode 只给语法树里的「命令」节点生成匹配项,声明命令不核对
    # (scripts/check-opencode-permissions.py 实测)。环境里不放凭据(凭据代理)才是兜底。
    "env", "env *", "printenv", "printenv *", "set", "set *", "export", "export *", "declare *",
)


def opencode_permissions(env: Mapping[str, str]) -> dict:
    """按容器角色给 opencode 的工具权限。AGENT_ROLE 由镜像定(kb-import 镜像里是 kb-import)。

    诊断容器:不能编辑文件;导入容器:kb-init 要用 write 工具写草稿(draft/NNN.json),编辑放行(只有知识库管理员进得来)。
    两者都不能联网、只能用 gaussdb-* 技能;bash 只放行技能脚本与几个只读命令。凭据本来就不在这个容器里
    (凭据代理,docker/credproxy.py),这一层是纵深防御。
    """
    role = (env.get("AGENT_ROLE") or ROLE_RUNTIME).strip()
    if role not in (ROLE_RUNTIME, ROLE_KB_IMPORT):
        raise ConfigError("AGENT_ROLE=%r 不认识,只能是 %s / %s" % (role, ROLE_RUNTIME, ROLE_KB_IMPORT))
    skills = (env.get("XDG_CONFIG_HOME") or "/data/config").rstrip("/") + "/opencode/skills"
    # 顺序就是规则:先全拒 → 放行技能脚本与只读命令 → 拒掉一切重定向 → 给放行过的命令补安全结尾 → 最后再拒漏洞。
    allowed = ["%s %s/gaussdb-*" % (py, skills) for py in ("python3", "python")]
    allowed += [p for cmd in _READ_ONLY_COMMANDS for p in (cmd, cmd + " *")]
    bash: Dict[str, str] = {"*": "deny"}
    bash.update((p, "allow") for p in allowed)
    bash["*>*"] = "deny"                                # 重定向写文件:`cat > x.py` 写出脚本再借技能路径执行
    bash.update((p + suffix, "allow") for p in allowed if p.endswith("*") for suffix in _SAFE_REDIRECTS)
    bash.update((p, "deny") for p in _BASH_DENY_FINAL)
    return {
        "edit": "deny" if role == ROLE_RUNTIME else "allow",
        "webfetch": "deny", "websearch": "deny",               # 数据不出域
        "skill": {"*": "deny", "gaussdb-*": "allow"},
        "read": "allow", "glob": "allow", "grep": "allow", "list": "allow",
        "external_directory": "allow",          # 知识库 /nas/kb 在项目目录之外,模型要能读规则原文
        "bash": bash,
    }


def render_opencode_config(env: Mapping[str, str]) -> dict:
    """opencode.json。设了 MODEL_PROXY_URL(凭据代理)时模型 Key 不进这个文件 —— 它在模型能读到的容器里。"""
    proxy = (env.get("MODEL_PROXY_URL") or "").strip()
    need = ("MODEL_ID",) if proxy else ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_ID")
    missing = [k for k in need if not env.get(k)]
    if missing:
        raise ConfigError("缺少环境变量:%s(模型服务地址、密钥、模型 id 由平台注入)" % "、".join(missing))
    name = env.get("MODEL_PROVIDER_NAME") or PROVIDER_DEFAULT
    model = env["MODEL_ID"]
    model_def = {"name": model}
    mods = input_modalities(env)
    if mods:
        model_def["modalities"] = {"input": mods, "output": ["text"]}
    options = ({"baseURL": proxy, "apiKey": "credproxy"} if proxy          # 占位:真 Key 由代理加
               else {"baseURL": env["MODEL_BASE_URL"], "apiKey": env["MODEL_API_KEY"]})
    return {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",                              # 绝不把对话分享到 opencode.ai
        "autoupdate": False,                              # 内网,且版本由镜像钉死
        "permission": opencode_permissions(env),
        "provider": {
            name: {
                "npm": "@ai-sdk/openai-compatible",
                "name": name,
                "options": options,
                "models": {model: model_def},
            }
        },
        "model": "%s/%s" % (name, model),
    }


_ALIAS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_ALIAS_LINE = re.compile(r"^\s*(?P<name>[^=\s]+)\s*=\s*(?P<ip>[^/\s]+)\s*/\s*(?P<db>[^/\s]+)\s*$")


def parse_instance_aliases(text: str):
    """GRMP_INSTANCE_ALIASES → (别名清单, 写错的行)。每项 `别名=实例IP/库名  # 说明`,换行或分号分隔。

    写错的行**不让 Pod 起不来**(只影响这一个功能),但也不悄悄丢掉:原文进 instance_aliases_errors,
    login --list 时说出来。别名重复(不分大小写)只认第一个。
    """
    items, errors, seen = [], [], set()
    parts = [p for line in (text or "").splitlines() for p in line.split(";")]
    for i, raw in enumerate(p for p in parts if p.strip()):
        body, _, note = raw.partition("#")
        m = _ALIAS_LINE.match(body)
        if not m:
            errors.append("第 %d 项「%s」:要写成 别名=实例IP/库名" % (i + 1, raw.strip()))
            continue
        name, ip, db = m.group("name"), m.group("ip"), m.group("db")
        if not _ALIAS_NAME.match(name):
            errors.append("第 %d 项「%s」:别名只能用字母、数字、._-" % (i + 1, raw.strip()))
        elif name.lower() in seen:
            errors.append("第 %d 项「%s」:别名 %s 重复,只认第一个" % (i + 1, raw.strip(), name))
        else:
            seen.add(name.lower())
            items.append({"name": name, "ip": ip, "database": db, "note": note.strip()})
    return items, errors


def render_gdaa_config(env: Mapping[str, str]) -> str:
    host = env.get("GRMP_API_HOST")
    if not host:
        raise ConfigError("缺少环境变量 GRMP_API_HOST(中间件地址由平台注入)")
    port = int(env.get("GRMP_API_PORT") or 8080)
    # 2026-09-18 中间件加固:配了 Appkey 就必须有 SM2 私钥,启动时就拒——拖到第一次请求会表现成
    # 中间件鉴权失败,排查方向被带到中间件那边。旋钮(GRMP_SIGN_*)由 skill 直接读环境变量,这里不落盘。
    # 走凭据代理(GRMP_PROXY_SOCKET)时私钥在代理那边,本容器本来就没有,由代理核对。
    appkey = env.get("GRMP_APPKEY", "")
    if appkey and not env.get("GRMP_SM2_PRIVATE_KEY") and not env.get("GRMP_PROXY_SOCKET"):
        raise ConfigError("配了 GRMP_APPKEY=%s 却没有 GRMP_SM2_PRIVATE_KEY(SM2 私钥由平台以 Secret grmp-sm2 注入)" % appkey)
    text = ("# 由 podctl 在 Pod 启动时按环境变量生成;不要手工改,重启即覆盖。\n"
            "connection_mode: api\n"
            "api_connection:\n"
            "  - host: %s\n"
            "    port: %d\n"
            "    token_env: GRMP_AUTH_TOKEN\n" % (host, port)
            + ("    appkey_env: GRMP_APPKEY\n" if appkey else ""))
    aliases, errors = parse_instance_aliases(env.get("GRMP_INSTANCE_ALIASES", ""))
    # json.dumps 的结果是合法的 YAML 流式写法:中文、引号、冒号都不用另外转义
    if aliases:
        text += "instance_aliases:\n" + "".join("  - %s\n" % json.dumps(a, ensure_ascii=False) for a in aliases)
    if errors:
        text += "instance_aliases_errors:\n" + "".join("  - %s\n" % json.dumps(e, ensure_ascii=False) for e in errors)
    return text


def _write_private(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------- .owner 独占

def _read_owner(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_owner(path: pathlib.Path, pod: str) -> None:
    _write_private(path, json.dumps({"pod": pod, "ts": time.time()}))


def owner_acquire(dir_: pathlib.Path, pod: str, stale_s: int = 90, wait_s: int = 55) -> bool:
    """同一个用户目录同一时刻只许一个 Pod 打开 opencode.db。新旧 Pod 交接时最多等 wait_s 秒。"""
    path = pathlib.Path(dir_) / OWNER_NAME
    deadline = time.time() + wait_s
    while True:
        cur = _read_owner(path)
        age = time.time() - float(cur.get("ts") or 0)
        if not cur or cur.get("pod") == pod or age >= stale_s:
            _write_owner(path, pod)
            # 按需模式也要换:否则这个人在按需模式下用过之后,池化里他的旧保留副本会被当成最新的
            _write_private(pathlib.Path(dir_) / GEN_NAME, secrets.token_hex(16))
            how = "接管过期标记 %s" % cur.get("pod") if cur and cur.get("pod") != pod else "新建"
            print("独占标记: 本 Pod %s 持有(%s)" % (pod, how))
            return True
        if time.time() >= deadline:
            print("独占标记: 另一个 Pod %s 仍持有 %s(%d 秒前刷新),不启动" % (cur.get("pod"), path, int(age)))
            return False
        time.sleep(5)


def owner_refresh(dir_: pathlib.Path, pod: str) -> bool:
    path = pathlib.Path(dir_) / OWNER_NAME
    if _read_owner(path).get("pod") != pod:
        return False
    _write_owner(path, pod)
    return True


def owner_release(dir_: pathlib.Path, pod: str) -> None:
    path = pathlib.Path(dir_) / OWNER_NAME
    if _read_owner(path).get("pod") == pod:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- SQLite

def _integrity_ok(db: pathlib.Path) -> bool:
    """**空文件不算通过。**

    SQLite 把 0 字节文件当成一个合法的空库,`integrity_check` 照样回 ok。
    如果拿它当备份恢复,结果是「用一个空库覆盖,并打印恢复成功」——
    用户看到的是正常启动加一个空的对话列表,没有任何报错。
    所以先看文件头:真库前 16 字节是 `SQLite format 3\\0`。
    """
    try:
        if db.stat().st_size == 0:
            return False
        with open(db, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return False
    except OSError:
        return False
    try:
        con = sqlite3.connect(db)
        try:
            row = con.execute("pragma integrity_check").fetchone()
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False
    return bool(row) and row[0] == "ok"


# 备份文件名:opencode.db.<UTC 时间戳>,时间戳形如 20260102T000000Z。
# **不能用 `opencode.db.*` 通配。** 那个通配还会命中 SQLite 的侧车文件
# `opencode.db.<时间戳>-wal` / `-shm`,而 `-wal` 按字典序排在同名备份之后 ——
# 「取最新一份」于是取到侧车。侧车常是 0 字节,配合上面那个空库洞,
# 结果是「恢复成功」+ 历史全空。它同时还会把轮转的份数算错。
_BACKUP_RE = re.compile(r"^opencode\.db\.\d{8}T\d{6}Z$")


def _backups(bdir: pathlib.Path) -> List[pathlib.Path]:
    if not bdir.is_dir():
        return []
    return sorted(p for p in bdir.iterdir() if _BACKUP_RE.match(p.name))


def db_check(db: pathlib.Path, backup_dir: pathlib.Path) -> str:
    """返回 ok / absent / restored / reset。损坏时留证、恢复、报出——不静默。"""
    db = pathlib.Path(db)
    if not db.exists():
        print("数据库自检: %s 不存在,首次启动,opencode 将新建" % db)
        return "absent"
    if _integrity_ok(db):
        print("数据库自检: 完整性检查通过(%s)" % db)
        return "ok"
    corrupt = db.with_name("opencode.db.corrupt-%s" % _utc_stamp())
    for suffix in ("", "-wal", "-shm"):
        src = db.with_name(db.name + suffix)
        if src.exists():
            src.rename(corrupt.with_name(corrupt.name + suffix))
    print("数据库自检: %s 损坏,已改名为 %s 留证" % (db.name, corrupt.name))
    for cand in reversed(_backups(pathlib.Path(backup_dir))):
        if _integrity_ok(cand):
            shutil.copy2(cand, db)
            print("数据库自检: 已从备份 %s 恢复;该备份之后的对话丢失" % cand.name)
            return "restored"
        print("数据库自检: 备份 %s 也损坏,跳过" % cand.name)
    print("数据库自检: 没有可用备份,以空库启动;历史对话丢失,请从 NAS 快照找回")
    return "reset"


def db_checkpoint(db: pathlib.Path) -> None:
    db = pathlib.Path(db)
    if not db.exists():
        return
    con = sqlite3.connect(db)
    try:
        con.execute("pragma wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    print("WAL 已合并回主库(%s)" % db)


def db_backup(db: pathlib.Path, backup_dir: pathlib.Path, keep: int = 3, now: Optional[str] = None) -> pathlib.Path:
    db = pathlib.Path(db)
    bdir = pathlib.Path(backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    target = bdir / ("opencode.db.%s" % _utc_stamp(now))
    src = sqlite3.connect(db)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)                       # 一致性快照,不依赖文件系统复制
    finally:
        dst.close()
        src.close()
    os.chmod(target, 0o600)
    for old in (_backups(bdir)[:-keep] if keep > 0 else []):
        old.unlink()
    print("退出备份: %s(保留最近 %d 份)" % (target.name, keep))
    return target


def log_prune(dir_: pathlib.Path, days: int = 7) -> List[pathlib.Path]:
    dir_ = pathlib.Path(dir_)
    if not dir_.is_dir():
        return []
    cutoff = time.time() - days * 86400
    removed = []
    for p in dir_.glob("*.log"):
        if p.stat().st_mtime < cutoff:
            p.unlink()
            removed.append(p)
    return removed


# ---------------------------------------------------------------- 报告只读端口

_REPORT_TYPES = {".json": "application/json; charset=utf-8",
                 ".jsonl": "application/x-ndjson; charset=utf-8",
                 ".html": "text/html; charset=utf-8"}


def make_reports_server(root: pathlib.Path, port: int, user_id: str = "", kb_dir: Optional[pathlib.Path] = None,
                        token: str = ""):
    """本人 reports/ 目录的只读静态服务(4097),大盘经网关按工号读它。

    **只做三件事以外的一律 404**:GET、本目录之内、三种后缀。它和 opencode 同一个 Pod,
    但不共用端口 —— opencode 的 API 有口令,这个端口的鉴权靠网关只把本人的请求路由过来。
    唯一不是文件的路径是 /whoami.json:回 GSDB_USER_ID(平台按 Pod 注入),大盘侧栏显示工号用。
    不从目录里读 —— 目录里的东西用户自己能改,工号不能让他改。
    另一条不是文件的路径是 /_kb/catalog.json:大盘知识库页签的目录,由 kbcatalog 从共享知识库现读现算
    (只读案例 / 现行条款 / 已确认的边,不读收件目录与原件)。没给 kb_dir 就没有这条路。
    「目录」页签按渐进式披露一层层取(kboutline,2026-10-25):/_kb/outline.json(L0 + L1)、
    /_kb/list.json(L1 下钻,分页)、/_kb/item/<ID>.json(L2)、/_kb/evidence/<ID>.json(L3;客户条目只给出处,不给原件)。
    知识库里的文件本身不能经这个端口直接取 —— 只端算好的目录。

    token 非空时(固定池化,2026-10-23)每个请求都要带 `Authorization: Bearer <token>`,否则 401:
    同一个 Pod 的 8 个槽位共用 localhost,只靠「网关只把本人的请求路由过来」挡不住同 Pod 的别人。
    令牌每次绑定 / 恢复都换,由网关注入,不到浏览器。
    """
    import http.server
    import posixpath
    import urllib.parse
    root = pathlib.Path(root).resolve()
    catalog = None
    outline = None
    if kb_dir is not None:
        # 同目录的 kbcatalog.py / kboutline.py;不给 kb_dir 时连 import 都不做(注释单独一行:镜像内容守卫按整行匹配 import)
        import kbcatalog
        import kboutline
        catalog = kbcatalog.Cached(pathlib.Path(kb_dir))
        outline = kboutline.Outline(pathlib.Path(kb_dir))

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # 不打访问日志:每次刷新大盘都是几条请求
            pass

        def do_GET(self):
            if token:
                import hmac
                got = self.headers.get("Authorization") or ""
                if not hmac.compare_digest(got.encode("utf-8"), ("Bearer " + token).encode("utf-8")):
                    return self._status(401)
            rel = posixpath.normpath(urllib.parse.unquote(self.path.split("?", 1)[0])).lstrip("/")
            if rel.startswith("_kb/") and rel != "_kb/catalog.json" and outline is not None:
                return self._outline(rel)
            if rel == "_kb/catalog.json" and catalog is not None:
                try:
                    data = json.dumps(catalog.get(), ensure_ascii=False).encode("utf-8")
                except Exception as exc:        # noqa: BLE001  知识库文件坏了也只影响这一页
                    data = json.dumps({"attached": False, "reason": "知识库目录读取失败:%s" % exc}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", _REPORT_TYPES[".json"])
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if rel == "whoami.json":
                data = json.dumps({"user_id": user_id}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", _REPORT_TYPES[".json"])
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            target = (root / rel).resolve() if rel else root
            ctype = _REPORT_TYPES.get(target.suffix)
            if (not rel or ctype is None or not str(target).startswith(str(root) + os.sep)
                    or not target.is_file()):
                return self._status(404)
            try:
                data = target.read_bytes()
            except OSError:
                return self._status(404)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _outline(self, rel: str):
            """「目录」页签的四条路;ID 只在目录里查,不拼路径。"""
            import re
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            arg = lambda k, d="": (query.get(k) or [d])[0]                       # noqa: E731
            try:
                if rel == "_kb/outline.json":
                    obj = outline.outline()
                elif rel == "_kb/list.json":
                    obj = outline.list(docset=arg("docset"), path=arg("path"), topic=arg("topic"),
                                       offset=int(arg("offset", "0") or 0), limit=int(arg("limit", "100") or 100))
                else:
                    m = re.fullmatch(r"_kb/(item|evidence)/(.+)\.json", rel)
                    obj = (outline.item(m.group(2)) if m.group(1) == "item" else outline.evidence(m.group(2))) if m else None
                    if obj is None:
                        return self._status(404)
            except (ValueError, OSError) as exc:
                obj = {"error": str(exc)}
            except Exception as exc:            # noqa: BLE001  知识库文件坏了也只影响这一页
                obj = {"attached": False, "reason": "知识库目录读取失败:%s" % exc}
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", _REPORT_TYPES[".json"])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _status(self, code: int):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            self._status(405)

        do_PUT = do_DELETE = do_PATCH = do_POST

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    return srv


# ---------------------------------------------------------------- CLI

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="podctl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render-opencode-config")
    p.add_argument("--out", required=True)
    p = sub.add_parser("render-gdaa-config")
    p.add_argument("--out", required=True)
    for name in ("owner-acquire", "owner-refresh", "owner-release"):
        p = sub.add_parser(name)
        p.add_argument("--dir", required=True)
        p.add_argument("--pod", required=True)
        if name == "owner-acquire":
            p.add_argument("--stale-s", type=int, default=90)
            p.add_argument("--wait-s", type=int, default=55)
    p = sub.add_parser("db-check")
    p.add_argument("--db", required=True)
    p.add_argument("--backup-dir", required=True)
    p = sub.add_parser("db-checkpoint")
    p.add_argument("--db", required=True)
    p = sub.add_parser("db-backup")
    p.add_argument("--db", required=True)
    p.add_argument("--backup-dir", required=True)
    p.add_argument("--keep", type=int, default=3)
    p = sub.add_parser("log-prune")
    p.add_argument("--dir", required=True)
    p.add_argument("--days", type=int, default=7)
    # 2026-09-21 本地态策略:启动 load、运行中定期 sync、收尾 finish。详见 statesync.py
    for name in ("state-load", "state-sync", "state-finish"):
        p = sub.add_parser(name)
        p.add_argument("--nas", required=True, help="$NAS_ME")
        p.add_argument("--local", required=True, help="本地态根目录(/data/state)")
    # 报告只读端口:大盘的数据口(entrypoint 在 opencode 之后拉起)
    # 固定池化(2026-10-23):槽位容器的 1 号进程(先清盘再等管家的指令)与管家的控制接口,参数来自环境变量
    sub.add_parser("pool-slot")
    sub.add_parser("pool-steward")
    # 凭据代理(2026-10-23):按需模式 Pod 的伴生容器,中间件令牌、SM2 私钥、模型 Key 只在它那里
    sub.add_parser("serve-credproxy")
    p = sub.add_parser("serve-reports")
    p.add_argument("--dir", required=True)
    p.add_argument("--port", type=int, default=4097)
    p.add_argument("--kb-dir", default="", help="共享知识库目录;给了才有 /_kb/catalog.json")
    # 网页剪藏接收(2026-10-27,只在导入容器):网关把管理员剪藏的网页转到这里,落进知识库收件目录。参数来自环境变量
    p = sub.add_parser("serve-clip")
    p.add_argument("--port", type=int, default=4095)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    a = _parser().parse_args(argv)
    try:
        if a.cmd == "render-opencode-config":
            _write_private(pathlib.Path(a.out),
                           json.dumps(render_opencode_config(os.environ), ensure_ascii=False, indent=2) + "\n")
        elif a.cmd == "render-gdaa-config":
            _write_private(pathlib.Path(a.out), render_gdaa_config(os.environ))
        elif a.cmd == "owner-acquire":
            return 0 if owner_acquire(pathlib.Path(a.dir), a.pod, a.stale_s, a.wait_s) else 1
        elif a.cmd == "owner-refresh":
            return 0 if owner_refresh(pathlib.Path(a.dir), a.pod) else 1
        elif a.cmd == "owner-release":
            owner_release(pathlib.Path(a.dir), a.pod)
        elif a.cmd == "db-check":
            db_check(pathlib.Path(a.db), pathlib.Path(a.backup_dir))
        elif a.cmd == "db-checkpoint":
            db_checkpoint(pathlib.Path(a.db))
        elif a.cmd == "db-backup":
            if pathlib.Path(a.db).exists():
                db_backup(pathlib.Path(a.db), pathlib.Path(a.backup_dir), a.keep)
        elif a.cmd == "log-prune":
            n = len(log_prune(pathlib.Path(a.dir), a.days))
            print("日志清理: 删除 %d 个超过 %d 天的 .log" % (n, a.days))
        elif a.cmd == "serve-reports":
            # 令牌经环境变量传(不放命令行:同容器里 ps 能看到命令行)
            make_reports_server(pathlib.Path(a.dir), a.port, user_id=os.environ.get("GSDB_USER_ID", ""),
                                kb_dir=pathlib.Path(a.kb_dir) if a.kb_dir else None,
                                token=os.environ.get("REPORTS_TOKEN", "")).serve_forever()
        elif a.cmd == "pool-slot":
            import slotrun
            return slotrun.main()
        elif a.cmd == "serve-credproxy":
            import credproxy
            return credproxy.main()
        elif a.cmd == "serve-clip":
            import clipd
            return clipd.main(a.port)
        elif a.cmd == "pool-steward":
            import poolsteward
            return poolsteward.serve()
        elif a.cmd in ("state-load", "state-sync", "state-finish"):
            import statesync
            lay = statesync.Layout(nas=pathlib.Path(a.nas), local=pathlib.Path(a.local))
            if a.cmd == "state-load":
                statesync.load(lay)
                statesync.mark_dirty(lay)     # load 之后才打标记:load 失败时不要留下假痕迹
            elif a.cmd == "state-sync":
                print(statesync.sync(lay).line())
            else:
                statesync.finish(lay)
    except ConfigError as exc:
        print("配置错误: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
