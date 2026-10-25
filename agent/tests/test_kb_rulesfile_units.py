"""common/kb/rulesfile —— 规则文件读取辅助从 kb.py 搬到 common,查询 skill 与导入 skill 共用。"""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import rulesfile as rf  # noqa: E402


def test_rule_without_status_is_active():
    assert rf.rule_status({"id": "GS-SQ-001"}) == rf.STATUS_ACTIVE


def test_iter_active_rules_drops_deprecated(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "a.yaml").write_text(
        "- id: GS-SQ-001\n  severity: warn\n  check: advisory\n  rule: x\n"
        "- id: GS-SQ-002\n  severity: warn\n  check: advisory\n  rule: y\n  status: deprecated\n",
        encoding="utf-8")
    [(path, err, active)] = list(rf.iter_active_rules(tmp_path))
    assert err is None and [e["id"] for e in active] == ["GS-SQ-001"]


def test_split_frontmatter_reports_unterminated_block():
    meta, err = rf.split_frontmatter("---\nid: x\nno end")
    assert meta is None and "---" in err


def test_read_text_file_accepts_gbk(tmp_path):
    p = tmp_path / "g.md"
    p.write_bytes("规范".encode("gb18030"))
    assert rf.read_text_file(p) == "规范"


def test_kb_py_still_exposes_the_helpers_by_the_old_names():
    """现有测试与 kb_cases 通过 kb.<name> 取这些函数;搬家不能改名。"""
    scripts = _ROOT / "skills" / "gaussdb-kb-import" / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("kb_rf_probe", scripts / "kb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("split_frontmatter", "load_rule_file", "rule_status", "iter_files",
                 "first_heading", "iter_active_rules", "read_text_file", "STATUS_ACTIVE",
                 "KB_SUBDIRS", "RULE_DIRS", "SEARCH_HIT_CAP"):
        assert getattr(mod, name) is getattr(rf, name), name
    assert mod._SEARCHABLE is rf.SEARCHABLE
