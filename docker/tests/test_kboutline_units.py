"""知识库大盘「目录」页签的数据(kboutline,2026-10-25):按渐进式披露一层层取。

钉住的:
  · 骨架里有 L0(八类计数、画像、官方文档集)与 L1(专题:本行条目、候选原因、官方计数与代表条目),官方文档的原书目录带条目数;
  · 官方文档逐条只在下钻时分页取,一页最多 200 条;
  · L2 给小节全文;L3 官方文档给原页(GUC 参数只给本参数一段),**客户条目只给出处位置、不给原件内容**;
  · ID 只在目录里查,不认识的 404;知识库文件本身仍不能经报告端口直接读。
"""
import json
import pathlib
import shutil
import sys
import threading

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))
sys.path.insert(0, str(_ROOT / "agent"))

import kboutline  # noqa: E402
import podctl  # noqa: E402
from common.kb import docset  # noqa: E402

_SAMPLE = _ROOT / "agent" / "skills" / "gaussdb-kb-import" / "testdata" / "sample-kb"
PAGE = ('<html><head><meta charset="utf-8"></head><body><h1>{t}</h1>{b}'
        '<div class="familylinks"><div class="parentlink">父主题：上级</div></div></body></html>')
GUC = ('<div class="section"><h4 class="sectiontitle">deadlock_timeout</h4><p>参数说明：死锁检测时间。</p><p>默认值：1000。</p></div>'
       '<div class="section"><h4 class="sectiontitle">lockwait_timeout</h4><p>参数说明：单个锁的最长等待时间。</p>'
       '<p>默认值：1200000（20min）。</p></div>')
FAULT = '<div class="section"><h4 class="sectiontitle">问题现象</h4><p>' + "锁等待超时，业务报错。" * 20 + '</p></div>'


def _obj(name, local=""):
    return f'<LI><OBJECT type="text/sitemap"><param name="Name" value="{name}"><param name="Local" value="{local}"></OBJECT>'


def _kb(tmp_path):
    kb = tmp_path / "kb"
    shutil.copytree(_SAMPLE, kb)
    src = tmp_path / "chm"
    src.mkdir()
    (src / "p_lock.html").write_text(PAGE.format(t="锁管理", b=GUC), encoding="utf-8")
    (src / "p_fault.html").write_text(PAGE.format(t="锁等待超时处理", b=FAULT), encoding="utf-8")
    (src / "book.hhc").write_text("<UL>" + _obj("使用指南") + "<UL>" + _obj("开发指南") + "<UL>" + _obj("配置运行参数")
                                  + "<UL>" + _obj("GUC参数说明") + "<UL>" + _obj("锁管理", "p_lock.html") + "</UL></UL></UL></UL>"
                                  + _obj("维护指南") + "<UL>" + _obj("故障处理") + "<UL>" + _obj("锁等待超时处理", "p_fault.html")
                                  + "</UL></UL></UL>", encoding="utf-8")
    staging = kb / "inbox" / "ds"
    docset.scan(src, staging)
    (staging / "plan").mkdir()
    (staging / "plan" / "plan.yaml").write_text(yaml.safe_dump(
        {"code": "T25", "product": "GaussDB 集中式", "versions": "505.2", "title": "测试文档"}, allow_unicode=True), encoding="utf-8")
    docset.apply(kb, docset.unpacked_root(staging), staging / "plan")
    return kb


def test_outline_skeleton_has_l0_l1_and_book_toc(tmp_path):
    o = kboutline.Outline(_kb(tmp_path)).outline()
    assert o["attached"] and o["official"] == 3 and o["types"]["case"] > 0
    lock = next(t for t in o["topics"] if t["slug"] == "lock")
    assert lock["entries"] and lock["official"] >= 2 and lock["official_top"][0]["id"].startswith("PD-T25-")
    assert any(c["rootcause"] for t in o["topics"] for c in t["causes"]), "候选原因来自本行确认过的因果链"
    ds = o["docsets"][0]
    assert ds["code"] == "T25" and ds["items"] == 3
    top = {c["name"]: c for c in ds["toc"]}
    assert top["使用指南"]["n"] == 2 and top["维护指南"]["n"] == 1
    assert "PD-T25-00001" not in json.dumps(ds["toc"]), "骨架里不带逐条官方文档"


def test_list_is_paged_and_filtered(tmp_path):
    ol = kboutline.Outline(_kb(tmp_path))
    page = ol.list(docset="T25", path="使用指南 › 开发指南", limit=1)
    assert page["total"] == 2 and len(page["items"]) == 1 and page["items"][0]["category"] == "GUC 参数"
    assert ol.list(topic="lock", limit=1000)["limit"] == kboutline.LIST_MAX


def test_item_and_evidence_layers(tmp_path):
    kb = _kb(tmp_path)
    ol = kboutline.Outline(kb)
    lw = next(c for c in docset.get(kb, "T25").catalog() if c["doc_key"].endswith("#lockwait_timeout"))
    it = ol.item(lw["id"])
    assert it["docset"] == "T25" and [s["name"] for s in it["sections"]] == ["参数说明", "默认值"]
    ev = ol.evidence(lw["id"])
    assert ev["kind"] == "page" and "### lockwait_timeout" in ev["text"] and "deadlock_timeout" not in ev["text"]
    case = next(t for t in ol.outline()["topics"] if t["entries"])["entries"][0]["id"]
    assert ol.item(case)["sections"], "客户条目给小节"
    cev = ol.evidence(case)
    assert cev["kind"] == "refs" and "text" not in cev and "kb.py evidence" in cev["note"], "客户原件不上大盘"
    assert ol.item("PD-T25-99999") is None and ol.item("../../etc/passwd") is None


def test_reports_server_routes(tmp_path):
    import http.client
    kb = _kb(tmp_path)
    root = tmp_path / "reports"
    root.mkdir()
    srv = podctl.make_reports_server(root, 0, kb_dir=kb)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
    try:
        st, body = get("/_kb/outline.json")
        assert st == 200 and json.loads(body)["docsets"][0]["code"] == "T25"
        st, body = get("/_kb/list.json?docset=T25&topic=lock&offset=0&limit=5")
        assert st == 200 and json.loads(body)["total"] >= 2
        first = json.loads(body)["items"][0]["id"]
        assert get(f"/_kb/item/{first}.json")[0] == 200 and get(f"/_kb/evidence/{first}.json")[0] == 200
        assert get("/_kb/item/NOPE.json")[0] == 404
        assert get("/_kb/docsets/T25/manifest.json")[0] == 404, "知识库文件本身仍不能直接读"
    finally:
        srv.shutdown()


def test_topic_list_puts_parameters_first(tmp_path):
    ol = kboutline.Outline(_kb(tmp_path))
    items = ol.list(topic="lock", limit=10)["items"]
    cats = [x["category"] for x in items]
    assert cats == sorted(cats, key=lambda c: ["GUC 参数", "系统表 / 视图", "说明类页面", "SQL 语法与函数", "错误码"].index(c))


def test_images_normalize_file_permissions():
    """构建目录里的文件可能是 0600;镜像里必须统一成人人可读,否则槽位(uid 2000+)读不到 /opt/agent 下的脚本。"""
    assert "chmod -R a+rX /opt/agent" in (_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
