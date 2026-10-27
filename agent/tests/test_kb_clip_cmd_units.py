"""kb.py clip 子命令(2026-10-27):status / list / ingest / clean。

status 是模型引导用户装书签前要跑的那一条:没开启、网站不在名单、剪藏过没有,输出都要说清;
ingest 之后要告诉模型按材料类型走哪条现有流程;clean 不能把还没导入的批次悄悄删掉。
"""
import importlib.util
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_SCRIPT = _ROOT / "skills" / "gaussdb-kb-import" / "scripts" / "kb.py"
sys.path.insert(0, str(_SCRIPT.parent))
spec = importlib.util.spec_from_file_location("kb", _SCRIPT)
kb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kb)

PAGE = ("<html><head><title>备份恢复手册</title></head><body><article><h1>备份恢复手册</h1><p>"
        + "全量备份每周日凌晨执行,恢复前先在演练库验证。" * 10 + "</p></article></body></html>")


def _clip(kbdir, name="clip-20261027-103000-ab12", user="u0001", url="https://wiki.corp/p/1", html=PAGE):
    d = kbdir / "inbox" / "clips" / name
    d.mkdir(parents=True)
    (d / (name + ".html")).write_text(html, encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"id": name, "url": url, "title": "", "user_id": user,
                                             "fetched_at": "2026-10-27T10:30:00+08:00", "images": [], "skipped": []},
                                            ensure_ascii=False), encoding="utf-8")
    return d


def _kbdir(tmp_path):
    d = tmp_path / "kb"
    d.mkdir()
    return d


def test_status_when_clipping_is_not_enabled_says_who_must_configure_it(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("KB_CLIP_ORIGINS", raising=False)
    monkeypatch.delenv("KB_CLIP_RECEIVER", raising=False)
    assert kb.main(["clip", "status", "--kb", str(_kbdir(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "未开启" in out and "KB_CLIP_ORIGINS" in out and "/kb-import/clip/install" in out
    assert "kb.py clip add" in out, "没有剪藏页时要告诉模型另一条路"


def test_status_reports_origins_url_check_and_the_users_last_clip(tmp_path, capsys, monkeypatch):
    d = _kbdir(tmp_path)
    _clip(d)
    monkeypatch.setenv("KB_CLIP_ORIGINS", "https://wiki.corp, https://confluence.corp")
    monkeypatch.setenv("KB_CLIP_RECEIVER", "1")
    monkeypatch.setenv("GSDB_USER_ID", "u0001")
    assert kb.main(["clip", "status", "--kb", str(d), "--url", "https://wiki.corp/display/OPS/x"]) == 0
    out = capsys.readouterr().out
    assert "已开启" in out and "在运行" in out and "在允许名单里" in out
    assert "clip-20261027-103000-ab12" in out and "未导入" in out
    assert kb.main(["clip", "status", "--kb", str(d), "--url", "https://other.corp/x", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["url"]["allowed"] is False and info["my_clips"] == 1 and info["install_url"] == "/kb-import/clip/install"


def test_ingest_converts_the_latest_own_batch_and_names_the_next_steps(tmp_path, capsys, monkeypatch):
    d = _kbdir(tmp_path)
    b = _clip(d)
    monkeypatch.setenv("GSDB_USER_ID", "u0001")
    assert kb.main(["clip", "ingest", "--kb", str(d)]) == 0
    out = capsys.readouterr().out
    assert (b / "page.md").is_file() and "全量备份每周日凌晨执行" in (b / "page.md").read_text(encoding="utf-8")
    assert "kb_init.py scan" in out and "kb.py ingest" in out and "kb.py template" in out
    assert "kb.py attach %s" % (b / (b.name + ".html")) in out and "kb.py clip clean %s" % b.name in out


def test_a_cross_site_image_is_counted_once_as_not_retrieved(tmp_path, capsys):
    """书签跳过的跨站图片既在 meta 的跳过清单里,也是正文里指不到本地的图片 —— 只算一张(端到端里报成了 2 张)。"""
    d = _kbdir(tmp_path)
    b = _clip(d, html=PAGE.replace("</article>", '<p><img src="https://cdn.other/x.png" alt="示意"></p></article>'))
    meta = json.loads((b / "meta.json").read_text(encoding="utf-8"))
    meta["skipped"] = [{"src": "https://cdn.other/x.png", "reason": "跨站图片,读不到"}]
    (b / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    assert kb.main(["clip", "ingest", b.name, "--kb", str(d)]) == 0
    assert "另有 1 张没取回" in capsys.readouterr().out


MHTML = ("From: <Saved by Blink>\r\nSnapshot-Content-Location: https://wiki.corp/display/OPS/backup\r\n"
         "Subject: =?utf-8?B?5aSH5Lu95oGi5aSN5omL5YaM?=\r\nMIME-Version: 1.0\r\n"
         "Content-Type: multipart/related; type=\"text/html\"; boundary=\"B\"\r\n\r\n--B\r\nContent-Type: text/html\r\n"
         "Content-Location: https://wiki.corp/display/OPS/backup\r\n\r\n" + PAGE + "\r\n--B--\r\n")


def test_clip_add_turns_a_saved_mhtml_into_a_batch_that_ingests(tmp_path, capsys, monkeypatch):
    """没有剪藏页的环境:浏览器另存的 .mhtml 上传到收件目录,clip add 成批次,之后照常 ingest。"""
    d = _kbdir(tmp_path)
    f = tmp_path / "备份恢复手册.mhtml"
    f.write_text(MHTML, encoding="utf-8")
    monkeypatch.setenv("GSDB_USER_ID", "u0001")
    assert kb.main(["clip", "add", str(f), "--kb", str(d)]) == 0
    out = capsys.readouterr().out
    assert "https://wiki.corp/display/OPS/backup" in out and "clip ingest clip-" in out
    batch = next((d / "inbox" / "clips").glob("clip-*"))
    meta = json.loads((batch / "meta.json").read_text(encoding="utf-8"))
    assert meta["title"] == "备份恢复手册" and meta["user_id"] == "u0001" and (batch / (batch.name + ".mhtml")).is_file()
    assert kb.main(["clip", "ingest", batch.name, "--kb", str(d)]) == 0
    assert "全量备份每周日凌晨执行" in (batch / "page.md").read_text(encoding="utf-8")


def test_clip_add_rejects_what_is_not_a_saved_web_page(tmp_path, capsys):
    d = _kbdir(tmp_path)
    bad = tmp_path / "x.mhtml"
    bad.write_text("<html>其实不是 mhtml</html>", encoding="utf-8")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert kb.main(["clip", "add", str(bad), "--kb", str(d)]) != 0
    assert kb.main(["clip", "add", str(pdf), "--kb", str(d)]) != 0
    assert kb.main(["clip", "add", str(tmp_path / "nope.mhtml"), "--kb", str(d)]) != 0
    out = capsys.readouterr()
    text = out.out + out.err
    assert "网页,单个文件" in text and "只收 .mhtml" in text and "文件不存在" in text
    assert not list((d / "inbox" / "clips").glob("clip-*")) if (d / "inbox" / "clips").exists() else True


def test_clip_add_takes_the_url_from_a_saved_html_or_from_the_option(tmp_path, capsys):
    d = _kbdir(tmp_path)
    f = tmp_path / "p.html"
    f.write_text("<!-- saved from url=(0024)https://wiki.corp/p/77 -->\n" + PAGE, encoding="utf-8")
    assert kb.main(["clip", "add", str(f), "--kb", str(d)]) == 0
    g = tmp_path / "q.htm"
    g.write_text(PAGE, encoding="utf-8")
    assert kb.main(["clip", "add", str(g), "--kb", str(d), "--url", "https://wiki.corp/p/88"]) == 0
    urls = sorted(json.loads((b / "meta.json").read_text(encoding="utf-8"))["url"] for b in (d / "inbox" / "clips").glob("clip-*"))
    assert urls == ["https://wiki.corp/p/77", "https://wiki.corp/p/88"]


def test_ingest_of_a_login_page_fails_with_the_reason(tmp_path, capsys):
    d = _kbdir(tmp_path)
    _clip(d, html="<html><head><title>登录</title></head><body><form><input type=password></form></body></html>")
    assert kb.main(["clip", "ingest", "clip-20261027-103000-ab12", "--kb", str(d)]) == 1
    assert "登录页" in capsys.readouterr().err


def test_list_and_clean_never_drop_an_unimported_batch_silently(tmp_path, capsys):
    d = _kbdir(tmp_path)
    b1 = _clip(d, name="clip-20261027-090000-aaaa")
    b2 = _clip(d, name="clip-20261027-100000-bbbb")
    assert kb.main(["clip", "list", "--kb", str(d)]) == 0
    out = capsys.readouterr().out
    assert out.index("bbbb") < out.index("aaaa") and "未导入" in out
    assert kb.main(["clip", "clean", b1.name, "--kb", str(d)]) == 2
    assert b1.is_dir() and "还没导入" in capsys.readouterr().out
    assert kb.main(["clip", "ingest", b2.name, "--kb", str(d)]) == 0
    assert kb.main(["clip", "clean", b2.name, "--kb", str(d)]) == 0 and not b2.exists()
    assert kb.main(["clip", "clean", b1.name, "--discard", "--kb", str(d)]) == 0 and not b1.exists()


def test_validate_reminds_unimported_clips_without_flagging_the_clips_dir(tmp_path, capsys):
    d = _kbdir(tmp_path)
    _clip(d)
    kb.main(["validate", "--kb", str(d)])
    out = capsys.readouterr().out
    assert "inbox/clips/clip-20261027-103000-ab12 剪藏了还没导入" in out
    assert "inbox/clips 尚未条款化" not in out
