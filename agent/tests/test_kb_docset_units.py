"""官方文档集(2026-10-25):CHM → 标准知识库的一层。

钉住的纪律:
  · 导航页不成条;GUC 参数一参一条、带字段;错误码按码切块、只建关键词;视图按名字归专题(不按华为放错的目录);
  · ID 永不复用:重导沿用,新页面接着编,删掉的页面号码保留;
  · 官方文档单独一个候选池:几千条官方文档不能把客户先例挤出候选;
  · 测试开关打开时官方文档一律「适用性未知」,并且打包时拒绝;
  · 包装上要核对校验和,同版本跳过;长条目 show 先给小节清单,错误码 --grep 只给那一码。
"""
import argparse
import importlib.util
import io
import json
import pathlib
import shutil
import sys
import tarfile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-kb-import" / "scripts"
_QUERY_SCRIPTS = _ROOT / "skills" / "gaussdb-kb" / "scripts"
_SAMPLE = _ROOT / "skills" / "gaussdb-kb-import" / "testdata" / "sample-kb"
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_SCRIPTS))

from common.kb import catalog as kbcat  # noqa: E402
from common.kb import chmdoc, docindex, docset, docset_pack, docsplit, htmldoc, layers, query  # noqa: E402
from common.kb import topics as kbtopics  # noqa: E402


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    base = _QUERY_SCRIPTS if name == "kb_cite" else _SCRIPTS      # 引用核对在查询 skill(gaussdb-kb)里
    spec = importlib.util.spec_from_file_location(name, base / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


PAGE = """<html><head><meta http-equiv="Content-Type" content="text/html; charset=gb2312"></head><body>
<h1 class="topictitle1">{title}</h1>{body}
<div class="familylinks"><div class="parentlink"><strong>父主题：</strong> <a href="x.html">上级</a></div></div>
<div class="footer">版权所有 © 华为技术有限公司</div></body></html>"""

GUC = """<p>本节介绍锁相关参数。</p>
<div class="section"><h4 class="sectiontitle">deadlock_timeout</h4><p><strong>参数说明：</strong>设置死锁超时检测时间。该参数仅针对常规锁生效。</p>
<p><strong>参数类型：</strong>整型</p><p><strong>取值范围：</strong>1 ~ 2147483647</p><p><strong>默认值：</strong>1000（即1s）。</p>
<p><strong>设置建议：</strong>一般保持默认。</p></div>
<div class="section"><h4 class="sectiontitle">lockwait_timeout</h4><p><strong>参数说明：</strong>单个锁的最长等待时间。超出报错。</p>
<p><strong>参数类型：</strong>整型</p><p><strong>默认值：</strong>1200000（20min）。</p>
<p><strong>设置不当的风险与影响：</strong>过大会延长阻塞。</p></div>"""

VIEW = """<p>PG_STAT_ACTIVITY视图显示和当前用户查询相关的信息。</p>
<table><thead><tr><th>名称</th><th>类型</th><th>描述</th></tr></thead>
<tbody><tr><td>pid</td><td>bigint</td><td>线程ID。</td></tr><tr><td>state</td><td>text</td><td>后端当前总体状态 | 含竖线</td></tr></tbody></table>"""

FAULT = """<div class="section"><h4 class="sectiontitle">问题现象</h4><p>业务大量锁等待超时，报 lock wait timeout。</p></div>
<div class="section"><h4 class="sectiontitle">处理步骤</h4><ol><li>查询 pg_locks 找阻塞源。</li><li>{pad}</li></ol>
<pre class="screen">SELECT * FROM pg_locks WHERE granted = false;</pre><h2>附注</h2><p>注意备份。</p></div>"""

ERR = """<p>GAUSS-00011: relation "&lt;string&gt;" does not exist</p><p>SQLSTATE: 42P01</p><p>CAUSE: 表不存在。</p><p>ACTION: 检查表名。</p>
<p>GAUSS-00012: could not obtain lock on row</p><p>SQLSTATE: 55P03</p><p>CAUSE: 行锁获取失败。</p><p>ACTION: 稍后重试。</p>"""

NAV = """<ul class="ullinks"><li><a href="p_lock.html">锁管理</a></li></ul>"""


def _obj(name, local):
    return f'<LI><OBJECT type="text/sitemap"><param name="Name" value="{name}"><param name="Local" value="{local}"></OBJECT>'


def make_doc(root: pathlib.Path, extra_fault: bool = False, drop_view: bool = False) -> pathlib.Path:
    root.mkdir(parents=True, exist_ok=True)
    pages = {"p_root.html": ("使用指南", NAV), "p_lock.html": ("锁管理", GUC), "p_view.html": ("PG_STAT_ACTIVITY", VIEW),
             "p_fault.html": ("锁等待超时处理", FAULT.format(pad="排查说明。" * 800)),
             "p_err.html": ("GAUSS-00011-- GAUSS-00020", ERR), "p_new.html": ("新增页面", "<p>" + "新内容说明。" * 30 + "</p>")}
    for name, (title, body) in pages.items():
        (root / name).write_bytes(PAGE.format(title=title, body=body).encode("gb18030"))
    view = "" if drop_view else (_obj("系统表和系统视图", "") + "<UL>" + _obj("系统视图", "") + "<UL>" + _obj("VACUUM", "")
                                 + "<UL>" + _obj("PG_STAT_ACTIVITY", "p_view.html") + "</UL></UL></UL>")
    hhc = ("<HTML><BODY><UL>" + _obj("使用指南", "p_root.html") + "<UL>" + _obj("开发指南", "") + "<UL>"
           + _obj("配置运行参数", "") + "<UL>" + _obj("GUC参数说明", "") + "<UL>" + _obj("锁管理", "p_lock.html")
           + "</UL></UL>" + view + "</UL>" + _obj("错误码参考（内核）", "") + "<UL>" + _obj("GAUSS-00011-- GAUSS-00020", "p_err.html")
           + "</UL></UL>" + _obj("维护指南", "") + "<UL>" + _obj("故障处理", "") + "<UL>" + _obj("锁等待超时处理", "p_fault.html")
           + ("" if not extra_fault else _obj("新增页面", "p_new.html")) + "</UL></UL></UL></BODY></HTML>")
    (root / "book.hhc").write_bytes(hhc.encode("gb18030"))
    return root


# ---------------------------------------------------------------- 转换与目录

def test_html_to_markdown_keeps_body_and_drops_navigation(tmp_path):
    lines = htmldoc.page_lines(make_doc(tmp_path / "d") / "p_fault.html")
    text = "\n".join(lines)
    assert lines[0] == "# 锁等待超时处理" and "### 问题现象" in lines and "#### 附注" in lines
    assert "```\nSELECT * FROM pg_locks WHERE granted = false;\n```" in text
    assert "父主题" not in text and "版权所有" not in text
    view = "\n".join(htmldoc.page_lines(tmp_path / "d" / "p_view.html"))
    assert "| 名称 | 类型 | 描述 |" in view and "含竖线" in view and "\\|" in view, "表格转 markdown,单元格里的竖线要转义"


def test_hhc_paths_follow_nesting(tmp_path):
    nodes = chmdoc.load_toc(make_doc(tmp_path / "d"))
    by = {n.title: n for n in nodes}
    assert by["锁管理"].path == ("使用指南", "开发指南", "配置运行参数", "GUC参数说明", "锁管理")
    assert by["锁等待超时处理"].path == ("维护指南", "故障处理", "锁等待超时处理")
    assert by["开发指南"].local == ""


def _items(tmp_path, **kw):
    root = make_doc(tmp_path / "d", **kw)
    items, _pages = docset.split_all(root, chmdoc.load_toc(root), [], kbtopics.DEFAULT_TOPICS)
    return {it.title: it for it in items}


def test_split_rules(tmp_path):
    items = _items(tmp_path)
    assert "使用指南" not in items, "只有子目录链接的导航页不成条"
    lw = items["lockwait_timeout(GUC 参数 · 锁管理)"]
    assert lw.kind == docsplit.KIND_GUC and lw.doc_key == "p_lock.html#lockwait_timeout"
    assert dict(lw.sections)["默认值"].startswith("1200000") and lw.topics == ("lock", "param")
    assert lw.summary.startswith("lockwait_timeout:单个锁的最长等待时间。") and "默认值 1200000（20min）" in lw.summary
    assert items["PG_STAT_ACTIVITY"].topics == ("conn",), "视图按名字归专题,不按它所在的 VACUUM 小节"
    fault = items["锁等待超时处理"]
    assert [n for n, _ in fault.sections] == ["问题现象", "处理步骤"] and fault.summary.startswith("业务大量锁等待超时")
    err = items["GAUSS-00011-- GAUSS-00020"]
    assert err.kind == docsplit.KIND_ERRCODE and "共 2 个" in err.summary
    assert [c for c, _ in docsplit.errcode_blocks(err.text())] == ["GAUSS-00011", "GAUSS-00012"]
    assert docsplit.find_errcode(err.text(), "lock on row")[0].startswith("GAUSS-00012")


# ---------------------------------------------------------------- 导入流程

@pytest.fixture()
def kb(tmp_path):
    d = tmp_path / "kb"
    shutil.copytree(_SAMPLE, d)
    (d / "kb.yaml").write_text("embeddings: {source: none}\n", encoding="utf-8")
    return d


def _ns(**kw):
    return argparse.Namespace(**{"kb": None, "slug": "ds", "code": None, "product": None, "versions": None,
                                 "deployment": None, "doc_version": None, "kernel_version": None, "engine_version": None,
                                 "as_of": None, "title": None, "exclude": None, "override": None, "depth": 3,
                                 "plan_dir": None, **kw})


def _import(kb, tmp_path, **doc_kw):
    kd = _load("kb_docset")
    src = make_doc(tmp_path / "src", **doc_kw)
    assert kd.cmd_scan(_ns(kb=str(kb), file=str(src))) == 2
    assert kd.cmd_plan(_ns(kb=str(kb), code="T25", product="GaussDB 集中式", versions="505.2",
                           deployment="华为云Stack 8.5.1")) == 2
    assert kd.cmd_apply(_ns(kb=str(kb))) == 0
    return docset.get(kb, "T25")


def test_ids_are_stable_and_never_reused(kb, tmp_path):
    ds = _import(kb, tmp_path)
    first = {c["doc_key"]: c["id"] for c in ds.catalog()}
    assert all(docset.ID_RE.match(i) for i in first.values())
    shutil.rmtree(tmp_path / "src")
    ds = _import(kb, tmp_path, drop_view=True)                     # 删掉视图页
    second = {c["doc_key"]: c["id"] for c in ds.catalog()}
    assert "p_view.html" not in second and all(second[k] == first[k] for k in second)
    shutil.rmtree(tmp_path / "src")
    ds = _import(kb, tmp_path, extra_fault=True)                   # 新页面接着编号,不复用视图页的号
    third = {c["doc_key"]: c["id"] for c in ds.catalog()}
    assert third["p_new.html"] not in first.values()
    assert third["p_view.html"] == first["p_view.html"], "页面回来了,号还是原来那个"
    assert (kb / "docsets" / ".rollback" / "T25").is_dir(), "旧的一份留着便于回滚"


def test_override_from_topic_map(kb, tmp_path):
    kd = _load("kb_docset")
    _import(kb, tmp_path)
    assert kd.cmd_plan(_ns(kb=str(kb), override=["维护指南 › 故障处理=wait"])) == 2
    assert kd.cmd_apply(_ns(kb=str(kb))) == 0
    cat = {c["title"]: c for c in docset.get(kb, "T25").catalog()}
    assert cat["锁等待超时处理"]["topics"] == ["wait"]


def _index(kb, budget=0.0):
    kd = _load("kb_docset")
    return kd.index_docsets(kb, budget)


def test_index_budget_resumes_and_errcodes_are_lexical_only(kb, tmp_path):
    from common.kb import store_sqlite as ssq
    ds = _import(kb, tmp_path)
    store = ssq.SqliteStore.open_writer(ds.index_path, dims=4)
    rep = docindex.run(ds, store, None, budget_s=1e-9)
    store.publish()
    assert rep.docs_left > 0 and not rep.done, "时限到了:停在完整条目之后,没写的留给下一次"
    assert _index(kb) == 0
    idx = docset.get(kb, "T25").manifest["index"]
    assert idx["complete"] and idx["docs_left"] == 0 and idx["chunks_lexical_only"] == 2


def test_docset_pool_keeps_customer_cases_and_labels_applicability(kb, tmp_path):
    kbm = _load("kb")
    _import(kb, tmp_path)
    assert kbm.cmd_index_all(argparse.Namespace(kb=str(kb), rebuild=False, fill_missing=False, budget=0)) in (0, 2)
    res = query.from_text("lockwait_timeout 默认值", kb_dir=kb)
    typed = [r.id for r in res.items[0].typed]
    assert any(i.startswith("product:PD-T25-") for i in typed), typed
    res = query.from_text("死锁", kb_dir=kb)
    assert "case:S2-20250812-CBMS-死锁频发" in [c.id for c in res.items[0].cases], "客户案例照样排在客户池里"
    (kb / "kb.yaml").write_text("embeddings: {source: none}\ntest_switches: {docset_applicability_unknown: true}\n",
                                encoding="utf-8")
    res = query.from_text("lockwait_timeout 默认值", kb_dir=kb)
    labels = [r.meta.get("_applies", "") for r in res.items[0].typed if r.id.startswith("product:PD-T25-")]
    assert labels and all("测试开关" in x for x in labels)
    assert any("测试开关" in line for line in res.status.l0)


def test_layers_show_official_docs(kb, tmp_path):
    _import(kb, tmp_path)
    cat = kbcat.load(kb)
    l0 = layers.render_l0(kb, cat, "1")
    assert "## 官方文档集" in l0 and "| T25 |" in l0 and "官方" in l0
    view = layers.topic_view(cat, [], "lock")
    l1 = layers.render_l1(view, "1")
    assert "## 官方文档(GaussDB 集中式 505.2 · 文档集 T25" in l1 and "kb.py doc T25 --topic lock" in l1
    assert l1.count("〔产品文档 PD-T25-") <= 4


def test_show_long_item_and_grep_errcode(kb, tmp_path, capsys):
    kr = _load("kb_read")
    ds = _import(kb, tmp_path)
    capsys.readouterr()                                 # 导入时打印的样例不算
    ids = {c["title"]: c["id"] for c in ds.catalog()}
    base = dict(kb=str(kb), brief=False, alias=None, section=None, grep=None, full=False)
    kr.cmd_show(argparse.Namespace(id=ids["锁等待超时处理"], **base))
    out = capsys.readouterr().out
    assert "## 小节(全文" in out and '--section "处理步骤"' in out and "排查说明。" * 50 not in out
    kr.cmd_show(argparse.Namespace(id=ids["GAUSS-00011-- GAUSS-00020"], **{**base, "grep": "GAUSS-00012"}))
    out = capsys.readouterr().out
    assert "could not obtain lock on row" in out and "does not exist" not in out
    kr.cmd_evidence(argparse.Namespace(id=ids["lockwait_timeout(GUC 参数 · 锁管理)"], kb=str(kb)))
    out = capsys.readouterr().out
    assert "### lockwait_timeout" in out and "### deadlock_timeout" not in out


def test_pack_refuses_test_switch_and_install_checks(kb, tmp_path):
    kd = _load("kb_docset")
    _import(kb, tmp_path)
    assert _index(kb) == 0
    (kb / "kb.yaml").write_text("embeddings: {source: none}\ntest_switches: {docset_applicability_unknown: true}\n",
                                encoding="utf-8")
    assert kd.cmd_pack(argparse.Namespace(kb=str(kb), code="T25", out=str(tmp_path / "out"))) == 2
    assert not list((tmp_path / "out").glob("*.tar.gz")) if (tmp_path / "out").exists() else True
    (kb / "kb.yaml").write_text("embeddings: {source: none}\n", encoding="utf-8")
    assert kd.cmd_pack(argparse.Namespace(kb=str(kb), code="T25", out=str(tmp_path / "out"))) == 0
    pack = next((tmp_path / "out").glob("kb-std-T25-*.tar.gz"))
    fresh = tmp_path / "fresh"
    shutil.copytree(_SAMPLE, fresh)
    assert docset_pack.install(fresh, pack)[0] == "installed"
    assert docset_pack.install(fresh, pack)[0] == "skipped"
    assert len(docset.get(fresh, "T25").catalog()) == len(docset.get(kb, "T25").catalog())
    # 篡改一个文件:校验和对不上,拒绝,原有的不动
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(pack, "r:gz") as src, tarfile.open(bad, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None
            if m.name.endswith("catalog.json"):
                data = data.replace(b"PD-T25", b"PD-T26")
                m.size = len(data)
            if m.name == docset_pack.PACK_INFO:
                info = json.loads(data)
                info["content_version"] = "other"
                data = json.dumps(info).encode()
                m.size = len(data)
            dst.addfile(m, io.BytesIO(data) if data is not None else None)
    with pytest.raises(docset_pack.PackError, match="校验和"):
        docset_pack.install(fresh, bad)
    assert docset.get(fresh, "T25").catalog()[0]["id"].startswith("PD-T25")



def test_install_command_writes_nothing_when_every_pack_is_already_there(kb, tmp_path, capsys):
    """部署时的安装 Job 每次 apply 都会跑:同版本跳过时连 L0 / L1 也不该重写。"""
    kd = _load("kb_docset")
    _import(kb, tmp_path)
    assert _index(kb) == 0
    (kb / "kb.yaml").write_text("embeddings: {source: none}\n", encoding="utf-8")
    out_dir = tmp_path / "packs"
    assert kd.cmd_pack(argparse.Namespace(kb=str(kb), code="T25", out=str(out_dir))) == 0
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert kd.cmd_install(argparse.Namespace(kb=str(fresh), pack=str(out_dir))) == 0
    out = capsys.readouterr().out
    assert "[installed]" in out and "L0 / L1 已按装好的文档集重建" in out
    l0 = fresh / "L0-总索引.md"
    stamp = l0.stat().st_mtime_ns
    assert kd.cmd_install(argparse.Namespace(kb=str(fresh), pack=str(out_dir))) == 0
    out = capsys.readouterr().out
    assert "[skipped]" in out and "L0 / L1" not in out and l0.stat().st_mtime_ns == stamp
    (tmp_path / "empty").mkdir()
    assert kd.cmd_install(argparse.Namespace(kb=str(fresh), pack=str(tmp_path / "empty"))) == 0
    assert "没有标准知识库包" in capsys.readouterr().out

def test_model_summary_must_come_from_the_page():
    kd = _load("kb_docset")
    page = "lockwait_timeout 默认 1200000,超时报错"
    assert kd.check_summary("lockwait_timeout 默认 1200000", page) == []
    probs = kd.check_summary("lockwait_timeout 默认 30 秒,可调 deadlock_timeout", page)
    assert probs and "30" in probs[0] and "deadlock_timeout" in probs[0]


def test_cite_check_knows_docset_ids():
    kc = _load("kb_cite")
    assert kc.TYPED_RE.search("见〔产品文档 PD-G25132C-02268〕").group(0) == "PD-G25132C-02268"


def test_old_index_without_novec_upgrades_in_place(tmp_path):
    """2026-10-25 测试环境:旧索引没有 novec 列,一次 index 里 setup 跑两遍,第二遍又去加列 → duplicate column。"""
    import sqlite3
    from common.kb import store_sqlite as ssq
    path = tmp_path / "store.sqlite"
    conn = sqlite3.connect(path)
    for sql in ssq._SCHEMA:
        conn.execute(sql.replace(", novec INTEGER NOT NULL DEFAULT 0", ""))
    conn.execute("INSERT INTO kb_meta (key, value) VALUES ('schema_version', ?)", (ssq.SCHEMA_VERSION,))
    conn.commit()
    conn.close()
    store = ssq.SqliteStore.open_writer(path, dims=4)
    store.setup()
    store.setup()                                   # 第二遍不许再加列
    store.rebuild()                                 # 删表重建后也不许
    store.publish()
    ro = ssq.SqliteStore.open_readonly(path, dims=4)
    assert ro.lexical_only_count() == 0 and ro.coverage() == (0, 0 if not ro.capabilities().vector else 0)
    ro.close()


def test_inbox_clean_accepts_docset_batches(kb, tmp_path):
    """2026-10-25 测试环境:docset 批次没有 source.md,inbox-clean 拒绝清理,221 MB 的解包目录留在收件目录里。"""
    ki = _load("kb_items")
    _import(kb, tmp_path)
    assert (kb / "inbox" / "ds" / "scan.json").is_file()
    ki.clean_inbox(kb, "ds")
    assert not (kb / "inbox" / "ds").exists() and (kb / "docsets" / "T25" / "plan" / "plan.yaml").is_file(), "配方已在文档集里"
