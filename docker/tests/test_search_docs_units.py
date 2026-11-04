"""镜像里的根规范检索(/workspace/search_docs.py):本行知识库不可用或查不到时,智能体按原路径检索 /workspace/docs/。

钉住:模板(ROOTSPEC-TEMPLATE)不参与检索、标明不得引用;命中给出所在章节(写出处用);按命中的关键词数排序;
docx 能搜能读;--show 只读 /workspace/docs 下的文件;没有规范时说清楚。镜像里预留的三个模板本身也在这里核对。
"""
import importlib.util
import pathlib
import zipfile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("search_docs", _ROOT / "docker" / "search_docs.py")


@pytest.fixture
def sd(tmp_path, monkeypatch):
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DOCS", tmp_path)
    return mod


def _docx(path: pathlib.Path, paras, style_id="Heading%d", styles=""):
    body = "".join(
        '<w:p>%s<w:r><w:t>%s</w:t></w:r></w:p>' % ('<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % (style_id % lv) if lv else "", t)
        for lv, t in paras)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="w"><w:body>%s</w:body></w:document>' % body)
        if styles:
            z.writestr("word/styles.xml", '<w:styles xmlns:w="w">%s</w:styles>' % styles)


SPEC_MD = """# 数据库变更管理规范

## 第三章 变更窗口

### 3.2 DDL 变更
生产库 DDL 变更须在变更窗口内执行,会话级锁等待上限不超过 30 秒。

## 第四章 回退
变更失败按回退方案执行。
"""


def test_templates_are_listed_but_never_searched(sd, tmp_path, capsys):
    for p in sorted((_ROOT / "docker" / "rootspec").glob("*.md")):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    assert sd.main(["根规范"]) == 0
    out = capsys.readouterr().out
    assert "没有找到" in out and "模板,还没有换成正式规范,不得引用" in out and "根规范-1.md" in out


def test_hit_carries_the_section_for_the_citation(sd, tmp_path, capsys):
    (tmp_path / "变更规范.md").write_text(SPEC_MD, encoding="utf-8")
    assert sd.main(["锁等待", "DDL"]) == 0
    out = capsys.readouterr().out
    assert "《数据库变更管理规范》" in out and "命中 2/2 个关键词" in out
    assert "第三章 变更窗口 > 3.2 DDL 变更" in out, "出处要能写成「第X章/X节」"
    assert "读全文:read 工具" in out


def test_files_matching_more_terms_come_first(sd, tmp_path, capsys):
    (tmp_path / "a.md").write_text("# 甲\n锁等待\n锁等待\n锁等待\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# 乙\n锁等待与 DDL\n", encoding="utf-8")
    sd.main(["锁等待", "DDL"])
    out = capsys.readouterr().out
    assert out.index("《乙》") < out.index("《甲》")


def test_docx_is_searchable_and_readable(sd, tmp_path, capsys):
    _docx(tmp_path / "开发规范.docx", [(1, "SQL 开发规范"), (2, "第六章 索引"), (0, "单表索引不超过 5 个。")])
    sd.main(["索引不超过"])
    out = capsys.readouterr().out
    assert "《SQL 开发规范》" in out and "第六章 索引" in out and "--show" in out
    assert sd.main(["--show", "开发规范.docx"]) == 0
    assert "单表索引不超过 5 个" in capsys.readouterr().out


def test_chinese_word_heading_ids_resolve_through_styles_xml(sd, tmp_path, capsys):
    """中文版 Word:标题样式 ID 是「1」「2」,名字 heading 1 / 标题 2 只在 styles.xml 里。"""
    styles = ('<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="2"><w:name w:val="标题 2"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="a3"><w:name w:val="List Paragraph"/></w:style>')
    _docx(tmp_path / "x.docx", [(1, "数据库变更管理规范"), (2, "3.2 DDL 变更"), (0, "生产库 DDL 变更须在变更窗口内执行。")],
          style_id="%d", styles=styles)
    sd.main(["变更窗口"])
    out = capsys.readouterr().out
    assert "《数据库变更管理规范》" in out and "[3.2 DDL 变更]" in out


def test_show_reads_only_inside_the_docs_dir(sd, tmp_path, capsys):
    (tmp_path / "x.md").write_text(SPEC_MD, encoding="utf-8")
    assert sd.main(["--show", "/etc/passwd"]) == 2
    assert sd.main(["--show", "../x.md"]) == 2
    assert sd.main(["--show", "x.md", "--from", "6", "--lines", "1"]) == 0
    assert "生产库 DDL 变更须在变更窗口内执行" in capsys.readouterr().out


def test_empty_or_missing_dir_says_so(sd, tmp_path, capsys, monkeypatch):
    assert sd.main(["锁"]) == 0 and "还没有根规范" in capsys.readouterr().out
    monkeypatch.setattr(sd, "DOCS", tmp_path / "nope")
    assert sd.main(["--list"]) == 0 and "还没有根规范" in capsys.readouterr().out


def test_image_ships_exactly_three_marked_templates():
    tpl = sorted((_ROOT / "docker" / "rootspec").glob("*"))
    assert [p.name for p in tpl] == ["根规范-1.md", "根规范-2.md", "根规范-3.md"]
    for p in tpl:
        first = p.read_text(encoding="utf-8").splitlines()[0]
        assert "ROOTSPEC-TEMPLATE" in first, "模板标记要在第一行:检索与替换脚本都靠它认模板"
        assert "客户" not in p.read_text(encoding="utf-8")


def test_dockerfile_puts_search_and_templates_in_both_images_and_the_import_prompt_in_kb_import():
    df = (_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    base = df[df.index("AS base"):df.index("AS imgdeps")]
    assert "COPY docker/search_docs.py /workspace/search_docs.py" in base and "COPY docker/rootspec/ /workspace/docs/" in base, \
        "放在公共阶段:runtime 与 kb-import 都要有"
    kbi = df[df.index("AS kb-import"):]
    assert "COPY agent/AGENTS-kb-import.md /opt/agent/config/opencode/AGENTS.md" in kbi
    assert "AGENTS-kb-import" not in df[df.index("AS runtime"):df.index("AS kb-import")]
