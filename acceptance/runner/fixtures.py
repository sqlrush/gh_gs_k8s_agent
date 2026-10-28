"""把题目变成真正发出去的那句话:替换占位符、贴夹具、放附件、建库里的夹具、剪藏批次。"""
from __future__ import annotations

import base64
import datetime as _dt
import http.cookiejar
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

REPO = pathlib.Path(__file__).resolve().parents[2]
_PH = re.compile(r"\{([^{}]+)\}")
LANG = {".sql": "sql", ".log": "text", ".md": "markdown"}
WIKI_PAGE = "/display/OPS/lock-incident"
WIKI_IMAGE = "/download/attachments/42/lock-chain.png"
WIKI_LOGIN = ("ops", "ops-wiki")              # mock-wiki.py 里写死的演示账号,不是任何真实口令


class FixtureError(Exception):
    pass


def minute(db_time: str, ceil: bool = False) -> str:
    """库时间 → 'YYYY-MM-DD HH:MM';结束时间向上取整,保证问的时段盖住整个窗口。"""
    t = _dt.datetime.fromisoformat(str(db_time).strip()[:19].replace("/", "-"))
    if ceil and (t.second or t.microsecond):
        t += _dt.timedelta(minutes=1)
    return t.strftime("%Y-%m-%d %H:%M")


def old_moment(days: int, now: Optional[_dt.datetime] = None) -> str:
    d = (now or _dt.datetime.now()) - _dt.timedelta(days=days)
    return d.strftime("%Y-%m-%d 03:00")


def render(text: str, values: Mapping[str, str]) -> Tuple[str, List[str]]:
    """替换占位符;返回 (结果, 取不到值的占位符)。"""
    missing: List[str] = []

    def sub(m: "re.Match[str]") -> str:
        v = values.get(m.group(1), "")
        if not v:
            missing.append(m.group(1))
            return m.group(0)
        return v
    return _PH.sub(sub, text), missing


def resolve_file(spec: str, faultlab_dir: pathlib.Path, generated: pathlib.Path) -> pathlib.Path:
    if spec.startswith("faultlab:"):
        return faultlab_dir / spec.split(":", 1)[1]
    return REPO / spec


def strip_sql_comments(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("--")).strip() + "\n"


def paste_block(path: pathlib.Path, strip: bool) -> str:
    body = path.read_text(encoding="utf-8")
    if strip:
        body = strip_sql_comments(body)
    return "\n\n```%s\n%s\n```" % (LANG.get(path.suffix, "text"), body.rstrip("\n"))


def place_upload(src: pathlib.Path, kb_nas: pathlib.Path, kb_pod: str, folder: str) -> str:
    """附件放进收件目录(客户环境里由网关落盘),返回提问里要说的那句话。"""
    dst_dir = kb_nas / "inbox" / "uploads" / folder
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / src.name)
    subprocess.run(["chmod", "-R", "a+rwX", str(dst_dir)], capture_output=True)
    return "\n\n(附件已上传到收件目录:%s/inbox/uploads/%s/%s)" % (kb_pod, folder, src.name)


def make_generated(out: pathlib.Path, python: str = sys.executable) -> None:
    p = subprocess.run([python, str(REPO / "acceptance" / "fixtures" / "make_fixtures.py"), "--out", str(out)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise FixtureError("生成夹具失败:%s" % (p.stderr or p.stdout)[-300:])


# ---------------------------------------------------------------- K2:模拟内网 Wiki → 剪藏批次

def start_wiki(port: int, log: pathlib.Path) -> subprocess.Popen:
    fh = log.open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(REPO / "scripts" / "k8s" / "mock-wiki.py"), str(port)],
                            stdout=fh, stderr=subprocess.STDOUT)
    time.sleep(1.5)
    if proc.poll() is not None:
        raise FixtureError("模拟 Wiki 起不来(端口 %d 被占?),看 %s" % (port, log))
    return proc


def clip_payload(wiki: str, fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """照书签的做法取页面:登录 → 取 HTML → 同源图片转 data URL,跨源图片记跳过。"""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    form = urllib.parse.urlencode({"user": WIKI_LOGIN[0], "password": WIKI_LOGIN[1], "next": WIKI_PAGE}).encode()
    op.open(wiki + "/login.action", data=form, timeout=30).read()
    html = op.open(wiki + WIKI_PAGE, timeout=30).read().decode("utf-8")
    img = op.open(wiki + WIKI_IMAGE, timeout=30).read()
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    other = re.search(r'<img src="(http://127\.0\.0\.1:\d+/logo\.png)"', html)
    return {"kind": "page", "url": wiki + WIKI_PAGE, "title": title.group(1).strip() if title else "",
            "fetched_at": fetched_at or _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "html": html, "images": [{"src": wiki + WIKI_IMAGE,
                                      "data": "data:image/png;base64," + base64.b64encode(img).decode()}],
            "skipped": [{"src": other.group(1), "reason": "跨站图片,读不到"}] if other else []}


def upload_clip(admin_portal: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """经网关的剪藏上传接口交给本人导入容器;导入容器会自己开会话发「导入剪藏 clips/<批次>」。"""
    req = urllib.request.Request(admin_portal + "/kb-import/clip/upload",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "X-KB-Clip": "1"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise FixtureError("剪藏上传被拒(%d):%s" % (e.code, e.read(300).decode("utf-8", "replace"))) from e
    if not out.get("batch") or not out.get("session"):
        raise FixtureError("剪藏上传没拿到批次或会话:%s" % json.dumps(out, ensure_ascii=False)[:300])
    return out


# ---------------------------------------------------------------- C4:库里的夹具过程

def db_fixture(path: pathlib.Path, sql: Callable[[str], str]) -> str:
    out = sql(path.read_text(encoding="utf-8"))
    if "ERROR" in out:
        raise FixtureError("建夹具 %s 失败:%s" % (path.name, out[-300:]))
    return out


def db_cleanup(path: pathlib.Path, sql: Callable[[str], str]) -> None:
    """夹具文件末尾的「收尾」注释里写着删除语句。"""
    stmts = [l.strip().lstrip("-").strip() for l in path.read_text(encoding="utf-8").splitlines()
             if re.match(r"^\s*--\s+DROP\s", l)]
    if stmts:
        sql("\n".join(stmts))
