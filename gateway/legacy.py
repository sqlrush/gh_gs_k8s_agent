"""老浏览器兼容补丁(v1.5.3):转发对话页时注入 static/legacy-compat.js,脚本由网关自己出。

现场 Windows 7 上的 Edge 109、360 企业安全浏览器 V13(Chromium 108 内核)打开对话页,行内代码(SQL 编号、函数名、
技能名)显示成黑块:opencode 网页的样式先把背景设成与文字同色兜底,再用 color-mix() 调浅;color-mix 要 Chrome 111+,
老内核丢掉这一句只剩兜底 —— 黑底黑字(2026-11-26 两张现场照片,内容没丢,选中可见)。opencode 1.18.27 的样式里
这样的 color-mix() 有 94 处,另有 18 处 light-dark()(123+)。补丁在老浏览器上把它们换算成老内核认的颜色,
新浏览器上什么都不做。不改 opencode、不改技能,只在网关这一层。

只动一种响应:对话路由上 GET 回来的 200、``text/html``、未压缩的页面(opencode 的单页入口,约 3 KB)。
接口、事件流、报告页、大盘、带 Content-Encoding 的响应一律原样转发。

- 引用放在 ``<head>`` **前**:head 里有 opencode 自己的主题预载脚本(从本地存储装主题样式),补丁要先于它执行;
  HTML 解析器会把它归进 head,行为与写在 head 里一致(dsh-docker 的兼容版同样做法,真 Chromium 108 上验过)。
- 同源外链、不内联:opencode 的 CSP 是 ``script-src 'self'`` 加一个固定 hash,内联脚本会被浏览器拦下。
- 关:``GATEWAY_LEGACY_COMPAT=0``(默认开)。
"""
from __future__ import annotations

import pathlib
import re
from typing import Optional

SCRIPT_PATH = "/__agent/legacy-compat.js"
TAG = ('<script src="%s"></script>' % SCRIPT_PATH).encode("ascii")
MAX_HTML = 2 * 1024 * 1024          # 超过这个大小的页面不改(对话页入口约 3 KB;大的多半不是入口页)
_SCRIPT_FILE = pathlib.Path(__file__).with_name("static") / "legacy-compat.js"
_HEAD = re.compile(rb"<head(?:\s[^>]*)?>", re.I)
_DOCTYPE = re.compile(rb"\s*<!doctype[^>]*>", re.I)
_script_cache: Optional[bytes] = None


def script() -> bytes:
    """补丁脚本的内容(读一次缓存)。文件随网关镜像发布,读不到说明镜像不完整,让它报错。"""
    global _script_cache
    if _script_cache is None:
        _script_cache = _SCRIPT_FILE.read_bytes()
    return _script_cache


def applies(method: str, status: int, content_type: Optional[str], content_encoding: Optional[str]) -> bool:
    """这个响应要不要改:GET、200、HTML、未压缩。"""
    if method != "GET" or status != 200:
        return False
    if not (content_type or "").lower().startswith("text/html"):
        return False
    return (content_encoding or "identity").strip().lower() in ("", "identity")


def inject(html: bytes) -> bytes:
    """在第一个 ``<head>`` 标签前插一行引用;没有 head 就插在 doctype 之后(再没有就放最前)。已经有了不重复插。"""
    if SCRIPT_PATH.encode("ascii") in html:
        return html
    m = _HEAD.search(html)
    if m:
        at = m.start()
    else:
        d = _DOCTYPE.match(html)
        at = d.end() if d else 0
    return html[:at] + TAG + b"\n" + html[at:]
