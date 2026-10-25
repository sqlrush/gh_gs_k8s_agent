"""common.kb.ingest —— csv/xlsx/文本 → 一单一文件;列名猜测;脱敏(无库)。"""
import pathlib
import sys
import zipfile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import ingest  # noqa: E402


def _xlsx(path: pathlib.Path, headers, rows, inline=False):
    """最小 xlsx:共享字符串表 + 一张 sheet;inline=True 时用内联字符串。"""
    strings, cells_xml = [], []
    def sref(s):
        if s not in strings:
            strings.append(s)
        return strings.index(s)
    def col(i):
        return chr(65 + i)
    all_rows = [headers] + rows
    for r, row in enumerate(all_rows, 1):
        cs = []
        for c, v in enumerate(row):
            if v == "":
                continue
            if isinstance(v, (int, float)):
                cs.append(f'<c r="{col(c)}{r}"><v>{v}</v></c>')
            elif inline:
                cs.append(f'<c r="{col(c)}{r}" t="inlineStr"><is><t>{v}</t></is></c>')
            else:
                cs.append(f'<c r="{col(c)}{r}" t="s"><v>{sref(v)}</v></c>')
        cells_xml.append(f'<row r="{r}">{"".join(cs)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", f'<workbook {ns}><sheets><sheet name="s" sheetId="1"/></sheets></workbook>')
        zf.writestr("xl/sharedStrings.xml",
                    f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        zf.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(cells_xml)}</sheetData></worksheet>')


HEADERS = ["工单号", "标题", "业务系统", "发生时间", "问题描述", "处理过程", "解决方案", "处理人"]
ROW = ["ITSM-2026-018823", "偶现单条update慢", "CBST", "2025-02-24",
       "业务偶现单条update耗时3s", "查 autovacuum 日志", "调大 autovacuum_vacuum_threshold", "张三 13812345678"]


def test_read_xlsx_shared_and_inline_strings(tmp_path):
    for inline in (False, True):
        p = tmp_path / f"t{int(inline)}.xlsx"
        _xlsx(p, HEADERS, [ROW, ["", "空号单", "", "", "x", "", "", ""]], inline=inline)
        headers, rows = ingest.read_xlsx(p)
        assert headers == HEADERS
        assert rows[0][0] == "ITSM-2026-018823" and rows[0][6].startswith("调大")
        assert rows[1][0] == "" and rows[1][1] == "空号单"


def test_read_xlsx_rejects_garbage(tmp_path):
    p = tmp_path / "bad.xlsx"
    p.write_bytes(b"not a zip")
    with pytest.raises(ingest.IngestError, match="xlsx"):
        ingest.read_xlsx(p)


def test_read_csv_gb18030(tmp_path):
    p = tmp_path / "t.csv"
    p.write_bytes("工单号,标题\nA-1,分区表未analyze\n".encode("gb18030"))
    headers, rows = ingest.read_csv(p)
    assert headers == ["工单号", "标题"] and rows == [["A-1", "分区表未analyze"]]


def test_guess_columns_synonyms():
    m = ingest.guess_columns(HEADERS)
    assert m["id"] == "工单号" and m["title"] == "标题" and m["system"] == "业务系统"
    assert m["occurred_at"] == "发生时间" and m["description"] == "问题描述"
    assert m["process"] == "处理过程" and m["solution"] == "解决方案"
    assert "处理人" not in m.values()
    en = ingest.guess_columns(["Ticket ID", "Summary", "Root Cause", "Resolution"])
    assert en["id"] == "Ticket ID" and en["title"] == "Summary" and en["root_cause"] == "Root Cause" and en["solution"] == "Resolution"


def test_rows_to_items_builds_markdown_with_sections_and_locator():
    items = ingest.rows_to_items(HEADERS, [ROW], ingest.guess_columns(HEADERS), "tickets.xlsx")
    it = items[0]
    assert it.id == "ITSM-2026-018823" and it.locator == "tickets.xlsx#row=2"
    assert it.text.startswith("# 偶现单条update慢\n")
    assert "## 问题描述\n\n业务偶现单条update耗时3s" in it.text
    assert "## 解决方案\n\n调大 autovacuum_vacuum_threshold" in it.text
    assert "## 原始字段\n\n- 处理人: 张三 13812345678" in it.text     # 未映射的列不丢
    assert it.fields["业务系统"] == "CBST"


def test_rows_without_id_get_row_number():
    items = ingest.rows_to_items(["标题"], [["a"], ["b"]], {"title": "标题"}, "t.csv")
    assert [i.id for i in items] == ["row2", "row3"]


def test_split_text_items():
    one = ingest.split_text_items("# 单一工单\n正文\n", "a.md", "a")
    assert len(one) == 1 and one[0].id == "a" and one[0].title == "单一工单" and one[0].locator == "a.md"
    many = ingest.split_text_items("# 甲\n1\n\n---\n\n# 乙\n2\n", "b.md", "b")
    assert [i.id for i in many] == ["b-1", "b-2"] and many[1].title == "乙" and many[1].locator == "b.md#item=2"


def test_redact_patterns_but_keep_object_names():
    text = "DBA 张三 13812345678 邮箱 a.b@bank.com 在 10.20.30.40 上查 cbst.cosp_asyn_task_dtl,证件 11010119900101123X"
    out, n = ingest.redact(text)
    assert n == 4
    assert "<手机号>" in out and "<邮箱>" in out and "<IP>" in out and "<证件号>" in out
    assert "cbst.cosp_asyn_task_dtl" in out


def test_write_items_frontmatter_and_redaction(tmp_path):
    items = ingest.rows_to_items(HEADERS, [ROW], ingest.guess_columns(HEADERS), "tickets.xlsx")
    paths = ingest.write_items(tmp_path / "items", items, "tickets.xlsx", do_redact=True)
    text = paths[0].read_text(encoding="utf-8")
    assert paths[0].name == "ITSM-2026-018823.md"
    assert text.startswith('---\nitem_id: "ITSM-2026-018823"\n')
    assert 'source: "tickets.xlsx#row=2"' in text and "redacted: 1" in text
    assert "<手机号>" in text and "13812345678" not in text
