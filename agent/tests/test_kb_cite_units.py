"""kb_cite —— cite-check:回答里引用的案例 / 条款 ID 逐个对文件核。

钉住的纪律:截断的案例 ID 按前缀找;已废止条款标出来;库里没有的一律「未找到」退出 2;
等待事件名 / GUC 名不是 ID,不许误报。
"""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_SCRIPTS = _ROOT / "skills" / "gaussdb-kb" / "scripts"      # cite-check 在查询 skill 里
sys.path.insert(0, str(_SCRIPTS))

spec = importlib.util.spec_from_file_location("kb", _SCRIPTS / "kb.py")
kbmain = importlib.util.module_from_spec(spec)      # 查询 skill 的 kb.py;不注册成 sys.modules["kb"],那个名字是导入侧的
spec.loader.exec_module(kbmain)

import kb_cite  # noqa: E402

CID = "S2-20250120-CBST-联机交易超时"


def _kb(tmp_path):
    for sub in ("cases", "rules", "archive"):
        (tmp_path / sub).mkdir()
    (tmp_path / "cases" / f"{CID}.md").write_text(
        f"---\nid: {CID}\ntitle: 联机交易超时\nsystem: CBST\noccurred_at: 2025-01-20\nconclusion: 已确认\nsource: s#x\n---\n"
        "## 现场\na\n## 判断\nb\n## 处置\nc\n", encoding="utf-8")
    (tmp_path / "rules" / "guc.yaml").write_text(
        "- id: GS-GUC-001\n  severity: warn\n  check: advisory\n  rule: 全局参数不因单次指标调整\n", encoding="utf-8")
    (tmp_path / "archive" / "idx.yaml").write_text(
        "- id: GS-IDX-009\n  severity: warn\n  check: advisory\n  rule: 旧条款\n  status: deprecated\n", encoding="utf-8")
    return tmp_path


def test_extract_ids_keeps_order_dedupes_and_ignores_non_ids():
    text = ("依据 GS-GUC-001 与案例 S2-20250120-CBST-联机交易超时(已确认);等待事件 LWLock:WALWriteLock、"
            "GUC lockwait_timeout 不是 ID。另见 GS-GUC-001。")
    assert kb_cite.extract_ids(text) == [("clause", "GS-GUC-001"), ("case", CID)]


def test_extract_ids_stops_at_fullwidth_punctuation_and_markdown():
    # 模型回答里常见:全角括号「（已确认）」、书名号、加粗星号——都不是 ID 的一部分
    text = f"案例 **{CID}**（已确认）,另见《{CID}》与 {CID}！"
    assert kb_cite.extract_ids(text) == [("case", CID)]


def test_extract_ids_ignores_the_blue_marking_tag():
    """出自知识库的内容按契约包进蓝色标签:标签不能粘进 ID,否则核对时整条变「疑似编造」。"""
    text = (f'<font color=#2f6fdb>案例 {CID}(已确认):处置 = 杀会话</font>;'
            '<font color=#2f6fdb>依据 GS-GUC-001</font>')
    assert kb_cite.extract_ids(text) == [("case", CID), ("clause", "GS-GUC-001")]


def test_check_exact_prefix_archived_and_fabricated(tmp_path):
    kb = _kb(tmp_path)
    text = f"{CID};S2-20250120-CBST-…;GS-GUC-001;GS-IDX-009;GS-VAC-002;S3-20250101-XYZ-不存在"
    by = {c.token: c for c in kb_cite.check(text, kb)}
    assert by[CID].status == "存在" and by[CID].resolved == CID
    assert by["S2-20250120-CBST-…"].status == "前缀匹配" and by["S2-20250120-CBST-…"].resolved == CID
    assert by["GS-GUC-001"].status == "存在"
    assert by["GS-IDX-009"].status == "已废止"
    assert by["GS-VAC-002"].status == "未找到" and by["GS-VAC-002"].note == "疑似编造"
    assert by["S3-20250101-XYZ-不存在"].status == "未找到"


def test_prefix_that_matches_several_cases_is_flagged_not_resolved(tmp_path):
    kb = _kb(tmp_path)
    other = "S2-20250120-CBST-批量对账失败"
    (kb / "cases" / f"{other}.md").write_text(
        (kb / "cases" / f"{CID}.md").read_text(encoding="utf-8").replace(CID, other).replace("联机交易超时", "批量对账失败"),
        encoding="utf-8")
    cite = kb_cite.check("见 S2-20250120-CBST-…", kb)[0]
    assert cite.status == "不唯一" and CID in cite.note and other in cite.note


def test_cmd_exit_codes_and_render(tmp_path, capsys):
    kb = _kb(tmp_path)
    assert kbmain.main(["cite-check", "--kb", str(kb), "--text", "依据 GS-GUC-001 与 S2-20250120-CBST-…"]) == 0
    out = capsys.readouterr().out
    assert "✓ GS-GUC-001" in out and f"前缀匹配 → {CID}" in out and "2 个在库" in out
    assert kbmain.main(["cite-check", "--kb", str(kb), "--text", "依据 GS-VAC-002 和 GS-IDX-009"]) == 2
    out = capsys.readouterr().out
    assert "✗ GS-VAC-002" in out and "疑似编造" in out and "⚠ GS-IDX-009" in out and "不得引用" in out
    f = tmp_path / "answer.md"
    f.write_text("这段回答没有引用任何 ID。", encoding="utf-8")
    assert kbmain.main(["cite-check", "--kb", str(kb), "--file", str(f)]) == 0
    assert "没有案例 ID" in capsys.readouterr().out
    assert kbmain.main(["cite-check", "--kb", str(kb), "--text", "  "]) == 1        # 没文本是运行错误
