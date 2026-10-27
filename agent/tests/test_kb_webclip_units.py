"""网页剪藏 → md(common/kb/webclip.py,2026-10-27)。

钉三件事:**只留正文**(导航、侧栏、评论、页脚不进知识库);**结构不丢**(标题层级、列表、表格、代码、
图片引用);**抽不出来不假装**(登录页、无权限页、空白页一律拒绝,不把「请登录」入库成一条知识)。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import webclip as w  # noqa: E402

URL = "https://wiki.corp/display/OPS/lock"

CONFLUENCE = """<html><head><title>锁等待处置手册 - 运维 Wiki</title></head><body>
<header id="header"><nav class="aui-header"><a href="/">首页</a><a href="/spaces">空间</a></nav></header>
<div id="sidebar" class="ia-splitter-left"><ul><li><a href="/a">目录 A</a></li><li><a href="/b">目录 B</a></li></ul></div>
<div id="main-content" class="wiki-content">
<h1>锁等待处置手册</h1>
<p>适用于 <strong>GaussDB 505</strong> 集中式实例。发现交易超时先看锁等待,不要直接重启应用。</p>
<div class="confluence-information-macro confluence-information-macro-warning"><p>执行 kill 前必须双人复核。</p></div>
<h2>排查步骤</h2>
<ol><li>查阻塞链:<code>SELECT * FROM pg_locks</code></li><li>定位根会话<ul><li>看 xact_start</li><li>看 state</li></ul></li></ol>
<pre class="syntaxhighlighter-pre" data-syntaxhighlighter-params="brush: sql; gutter: false">SELECT pid, wait_event
FROM pg_stat_activity
WHERE waiting;</pre>
<table><tbody><tr><th>现象</th><th>处置</th></tr><tr><td>锁等待 &gt; 5 分钟</td><td>通知应用 | 再 kill</td></tr></tbody></table>
<p><img src="/download/attachments/1/lock.png" alt="阻塞链截图"> 跨站图:<img src="https://cdn.corp/x.png" alt="监控"></p>
<p>另见 <a href="https://wiki.corp/x/123">相关案例</a> 与 <a href="#top">回到顶部</a>。</p>
</div>
<div id="comments-section" class="comments"><p>张三:写得好,已收藏</p></div>
<footer id="footer">版权所有 © 运维部</footer>
</body></html>"""


def test_confluence_page_keeps_only_the_body_with_its_structure():
    md, st = w.html_to_markdown(CONFLUENCE, URL, {"https://wiki.corp/download/attachments/1/lock.png": "images/img-001.png"})
    assert md.startswith("# 锁等待处置手册")
    for noise in ("首页", "目录 A", "写得好", "版权所有"):
        assert noise not in md, noise
    assert "**GaussDB 505**" in md
    assert "> **警告**: 执行 kill 前必须双人复核。" in md
    assert "## 排查步骤" in md
    assert "1. 查阻塞链:`SELECT * FROM pg_locks`" in md and "\n  - 看 xact_start" in md
    assert "```sql\nSELECT pid, wait_event\nFROM pg_stat_activity\nWHERE waiting;\n```" in md
    assert "| 现象 | 处置 |\n|---|---|\n| 锁等待 > 5 分钟 | 通知应用 \\| 再 kill |" in md
    assert "![阻塞链截图](images/img-001.png)" in md
    assert "[图片未取回:监控](https://cdn.corp/x.png)" in md
    assert "[相关案例](https://wiki.corp/x/123)" in md and "回到顶部" in md and "(#top)" not in md
    assert st["tables"] == 1 and st["headings"] == 2 and st["images_used"] == 1
    assert st["images_missing"] == ["https://cdn.corp/x.png"]
    assert w.refusal(st, URL) is None


def test_page_without_a_content_container_falls_back_to_body_and_prunes_noise():
    html = """<html><head><title>巡检经验</title></head><body>
    <div class="topbar"><a href="/">门户</a> | <a href="/me">我的</a></div>
    <div class="breadcrumb">首页 &gt; 知识 &gt; 巡检</div>
    <div class="box"><h3>每日巡检要点</h3><p>每天早上九点前完成主备复制延迟、连接数、慢 SQL 三项检查,发现复制延迟超过 30 秒立即上报值班 DBA。</p>
    <p>连接数超过 max_connections 的八成时,先看是不是连接池泄漏,再决定是否扩容。</p></div>
    <div class="share-bar">分享到 · 点赞 12</div>
    <aside>热门文章:如何升级</aside>
    </body></html>"""
    md, st = w.html_to_markdown(html, "https://portal.corp/kb/1")
    assert "每日巡检要点" in md and "复制延迟超过 30 秒" in md and "连接池泄漏" in md
    for noise in ("门户", "首页 > 知识", "点赞", "热门文章"):
        assert noise not in md, noise
    assert st["noise_removed"] >= 3
    assert md.startswith("# 巡检经验")


def test_noise_class_on_the_big_content_block_is_not_mistaken_for_noise():
    """内容容器的 class 偶尔带 nav / header 字样:占正文六成以上的块不删。"""
    body = "<p>" + "表空间使用率超过百分之八十五时要扩容。" * 20 + "</p>"
    html = '<html><body><div class="page-header-wrapper">%s</div><div class="nav">首页</div></body></html>' % body
    md, _st = w.html_to_markdown(html, "https://x.corp/p")
    assert "表空间使用率" in md and "首页" not in md


@pytest.mark.parametrize("html,url,expect", [
    ('<html><head><title>统一认证登录</title></head><body><form><input name=u><input type=password name=p>'
     '<button>登录</button></form></body></html>', "https://wiki.corp/login", "登录页"),
    ('<html><head><title>Sign in - Confluence</title></head><body><p>Please sign in to continue.</p></body></html>',
     "https://wiki.corp/x", "登录页"),
    ('<html><head><title>没有权限</title></head><body><p>您没有权限访问此页面</p></body></html>',
     "https://wiki.corp/x", "无权限"),
    ('<html><head><title>加载中</title></head><body><div id="app"></div></body></html>', "https://wiki.corp/x", "抽不到正文"),
])
def test_login_denied_and_empty_pages_are_refused(html, url, expect):
    _md, st = w.html_to_markdown(html, url)
    why = w.refusal(st, url)
    assert why and expect in why


def test_a_real_page_that_merely_mentions_login_is_not_refused():
    html = "<html><head><title>登录失败排查</title></head><body><article><h1>数据库登录失败排查</h1>" + \
           "<p>" + "应用报 password authentication failed 时,先核对 pg_hba 与账号是否被锁。" * 6 + "</p></article></body></html>"
    _md, st = w.html_to_markdown(html, "https://wiki.corp/x")
    assert w.refusal(st, "https://wiki.corp/x") is None


def test_layout_tables_are_flattened_and_data_tables_kept():
    html = """<html><body><article>
    <table><tr><td><h2>参数说明</h2><p>以下参数在业务高峰前调整。</p>
      <table><tr><th>参数</th><th>建议</th></tr><tr><td>work_mem</td><td>64MB</td></tr></table></td></tr></table>
    </article></body></html>"""
    md, st = w.html_to_markdown(html, "https://wiki.corp/p")
    assert "## 参数说明" in md and "| 参数 | 建议 |" in md and "| work_mem | 64MB |" in md
    assert st["tables"] == 1                      # 外层排版表不算、不转成表格


def test_hidden_scripts_and_styles_never_leak_into_text():
    html = """<html><body><main><h1>标题</h1><script>var token='abc';</script><style>.x{color:red}</style>
    <p style="display:none">隐藏的调试信息</p><p hidden>也隐藏</p><p>""" + "正文内容足够长。" * 30 + """</p></main></body></html>"""
    md, _ = w.html_to_markdown(html, "https://x.corp/p")
    assert "token" not in md and "color:red" not in md and "隐藏" not in md and "正文内容" in md


def _batch(kb, name="clip-20261027-103000-ab12", html=CONFLUENCE, meta=None, suffix=".html"):
    d = w.clips_dir(kb) / name
    (d / "images").mkdir(parents=True)
    raw = html if isinstance(html, bytes) else html.encode("utf-8")
    (d / (name + suffix)).write_bytes(raw)
    (d / "images" / "img-001.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    m = {"id": name, "url": URL, "title": "", "fetched_at": "2026-10-27T10:30:00+08:00", "user_id": "u0001",
         "images": [{"src": "https://wiki.corp/download/attachments/1/lock.png", "file": "img-001.png"}],
         "skipped": [{"src": "https://cdn.corp/x.png", "reason": "unreadable"}]}
    m.update(meta or {})
    (d / "meta.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return d


def test_convert_writes_page_md_with_provenance(tmp_path):
    kb = tmp_path / "kb"
    d = _batch(kb)
    res = w.convert(d, kb)
    page = (d / "page.md").read_text(encoding="utf-8")
    assert page.startswith("---\nkind: web-clip\nbatch: clip-20261027-103000-ab12\n")
    assert 'source_url: "%s"' % URL in page and "fetched_at: 2026-10-27T10:30:00+08:00" in page
    assert "extracted_by: script" in page and "sha256: %s" % res.sha256 in page
    assert "# 锁等待处置手册" in page and "![阻塞链截图](images/img-001.png)" in page
    info = json.loads((d / "ingest.json").read_text(encoding="utf-8"))
    assert info["tables"] == 1 and info["images_used"] == 1
    assert res.skipped and res.snapshot.name == d.name + ".html"


def test_convert_refuses_a_login_page_and_writes_nothing(tmp_path):
    kb = tmp_path / "kb"
    d = _batch(kb, html="<html><head><title>登录</title></head><body><form><input type=password></form></body></html>")
    with pytest.raises(w.ClipError, match="登录页"):
        w.convert(d, kb)
    assert not (d / "page.md").exists()


def test_gb2312_page_is_decoded_by_its_declared_charset(tmp_path):
    kb = tmp_path / "kb"
    html = ('<html><head><meta charset="gb2312"><title>备份规范</title></head><body><article><h1>备份规范</h1><p>'
            + "全量备份每周日凌晨执行,增量备份每天执行,备份保留三十天。" * 5 + "</p></article></body></html>")
    d = _batch(kb, html=html.encode("gb2312"))
    w.convert(d, kb)
    assert "全量备份每周日凌晨执行" in (d / "page.md").read_text(encoding="utf-8")


def test_bookmarklet_clip_is_utf8_even_when_the_page_declared_gbk(tmp_path):
    """书签交来的是 JS 字符串(接收服务存 utf-8,meta.encoding=utf-8):页面里 <meta charset=gbk> 已经不作数。"""
    kb = tmp_path / "kb"
    html = ('<html><head><meta charset="gbk"><title>备份规范</title></head><body><article><h1>备份规范</h1><p>'
            + "全量备份每周日凌晨执行,增量备份每天执行,备份保留三十天。" * 5 + "</p></article></body></html>")
    d = _batch(kb, html=html.encode("utf-8"), meta={"encoding": "utf-8"})
    w.convert(d, kb)
    assert "全量备份每周日凌晨执行" in (d / "page.md").read_text(encoding="utf-8")

def test_same_url_with_changed_content_points_at_the_earlier_batch(tmp_path):
    kb = tmp_path / "kb"
    old = _batch(kb, name="clip-20261020-090000-aaaa", meta={"sha256": "0" * 64})
    new = _batch(kb, name="clip-20261027-103000-bbbb")
    assert w.convert(new, kb).changed_from == old.name


MHTML = b"""From: <Saved by Blink>
Snapshot-Content-Location: https://wiki.corp/display/OPS/backup
Subject: =?utf-8?Q?=E5=A4=87=E4=BB=BD?=
MIME-Version: 1.0
Content-Type: multipart/related; type="text/html"; boundary="----MultipartBoundary--abc"

------MultipartBoundary--abc
Content-Type: text/html
Content-ID: <frame-1@mhtml.blink>
Content-Transfer-Encoding: quoted-printable
Content-Location: https://wiki.corp/display/OPS/backup

<html><head><meta charset=3D"utf-8"><title>=E5=A4=87=E4=BB=BD=E8=A7=84=E8=8C=83</title></head>=
<body><div id=3D"main-content" class=3D"wiki-content"><h1>=E5=A4=87=E4=BB=BD=E8=A7=84=E8=8C=83</h1>=
<p>""" + ("=E5=85=A8=E9=87=8F=E5=A4=87=E4=BB=BD=E6=AF=8F=E5=91=A8=E6=97=A5=E6=89=A7=E8=A1=8C=E3=80=82" * 12).encode() + b"""</p>=
<p><img src=3D"https://wiki.corp/img/b.png" alt=3D"cron"></p></div></body></html>
------MultipartBoundary--abc
Content-Type: image/png
Content-Transfer-Encoding: base64
Content-Location: https://wiki.corp/img/b.png

iVBORw0KGgo=
------MultipartBoundary--abc--
"""


def test_mhtml_upload_is_parsed_with_its_images(tmp_path):
    html, base, res = w.parse_mhtml(MHTML)
    assert base == "https://wiki.corp/display/OPS/backup" and "备份规范" in html
    assert list(res) == ["https://wiki.corp/img/b.png"] and res["https://wiki.corp/img/b.png"][1].startswith(b"\x89PNG")
    kb = tmp_path / "kb"
    d = _batch(kb, html=MHTML, suffix=".mhtml", meta={"url": "", "images": [], "skipped": []})
    out = w.convert(d, kb)
    page = (d / "page.md").read_text(encoding="utf-8")
    assert out.url == "https://wiki.corp/display/OPS/backup" and "全量备份每周日执行" in page
    assert "![cron](images/img-001.png)" in page and (d / "images" / "img-001.png").read_bytes().startswith(b"\x89PNG")


def test_not_an_mhtml_is_reported():
    with pytest.raises(w.ClipError, match="mhtml"):
        w.parse_mhtml(b"MIME-Version: 1.0\nContent-Type: text/plain\n\nhello")


def test_batch_names_are_validated_and_must_exist(tmp_path):
    kb = tmp_path / "kb"
    _batch(kb)
    assert w.batch_path(kb, "inbox/clips/clip-20261027-103000-ab12/").name == "clip-20261027-103000-ab12"
    for bad in ("../../etc", "clip-x", "clip-20261027-103000-zzzz"):
        with pytest.raises(w.ClipError):
            w.batch_path(kb, bad)


def test_latest_prefers_the_users_own_batch(tmp_path):
    kb = tmp_path / "kb"
    _batch(kb, name="clip-20261027-100000-aaaa", meta={"user_id": "u0001"})
    _batch(kb, name="clip-20261027-110000-bbbb", meta={"user_id": "u0002"})
    assert w.latest(kb, "u0001").name == "clip-20261027-100000-aaaa"
    assert w.latest(kb).name == "clip-20261027-110000-bbbb"
    with pytest.raises(w.ClipError):
        w.latest(tmp_path / "empty")


def test_allowed_origins_are_normalized():
    assert w.allowed_origins(" https://Wiki.Corp/ ,https://confluence.corp:8443/display, http://x.corp  https://wiki.corp") == \
        ["https://wiki.corp", "https://confluence.corp:8443", "http://x.corp"]
    assert w.allowed_origins("") == [] and w.allowed_origins("not a url") == []
    assert w.origin_of("https://wiki.corp/a/b?c=1#d") == "https://wiki.corp"
