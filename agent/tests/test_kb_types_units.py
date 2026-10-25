"""八类知识的纯函数层(2026-10-24):类型化条目校验、专题归类、版本与适用性。

钉住的纪律:
  · 类型 = 目录,frontmatter 的 type 必须一致;硬性缺项(摘要、出处、专题、各类必填)是 error,要点缺项是 warn;
  · 画像里不许有 IP / 主机:端口 / 口令(它进 L0,每次诊断都读);
  · ID 永不复用:改过类型的旧 ID(aliases)也算占用;
  · 适用性只有 适用 / 不适用 / 未知,拿不准一律未知;没声明边界的条目不打标签。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import topics as kbtopics  # noqa: E402
from common.kb import types as kbtypes  # noqa: E402
from common.kb import versions as kbv  # noqa: E402


def _write(kb: pathlib.Path, code: str, item_id: str, front: str, body: str = "## 摘要\n一句话结论。\n") -> pathlib.Path:
    d = kb / kbtypes.TYPED[code].dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{item_id}.md"
    p.write_text(f"---\nid: {item_id}\ntype: {code}\n{front}---\n{body}", encoding="utf-8")
    return p


# ---------------------------------------------------------------- 类型化条目

def test_a_complete_runbook_parses_clean(tmp_path):
    p = _write(tmp_path, "runbook", "RB-LCK-001",
               "title: 终止阻塞根会话\ntopics: [lock]\nsource: 运维手册#4.2\nverification: 已验证\n",
               "## 摘要\n只终止根会话。\n## 适用场景\na\n## 执行前提\nb\n## 影响\nc\n## 步骤\nd\n## 验证方法\ne\n## 回退步骤\nf\n")
    item, errors, warns = kbtypes.parse_item(p.read_text(encoding="utf-8"), p, "runbook")
    assert errors == [] and warns == []
    assert item.topics == ("lock",) and item.verification == "已验证"


@pytest.mark.parametrize("code,item_id,front,needle", [
    ("wiki", "WK-0001", "title: x\ntopics: [conn]\n", "缺少 source"),
    ("wiki", "WK-0001", "title: x\nsource: s\n", "缺少 topics"),
    ("product", "PD-0001", "title: x\ntopics: [lock]\nsource: s\nproduct: openGauss\n", "缺少 versions"),
    ("product", "PD-0001", "title: x\ntopics: [lock]\nsource: s\nproduct: Oracle\nversions: '*'\n", "product `Oracle` 非法"),
    ("defect", "KD-0001", "title: x\ntopics: [wait]\nsource: s\n", "缺少 affected_versions"),
    ("baseline", "BL-CBST-QPS", "title: x\ntopics: [conn]\nsystem: CBST\n", "缺少 metric"),
    ("profile", "EP-CBST", "title: x\n", "缺少 system"),
    ("wiki", "WK-1", "title: x\ntopics: [conn]\nsource: s\n", "不符合"),
])
def test_hard_requirements_are_errors(tmp_path, code, item_id, front, needle):
    p = _write(tmp_path, code, item_id, front)
    _item, errors, _w = kbtypes.parse_item(p.read_text(encoding="utf-8"), p, code)
    assert any(needle in e for e in errors), errors


def test_type_must_match_the_directory(tmp_path):
    p = _write(tmp_path, "wiki", "WK-0001", "title: x\ntopics: [conn]\nsource: s\n")
    text = p.read_text(encoding="utf-8").replace("type: wiki", "type: runbook")
    _i, errors, _w = kbtypes.parse_item(text, p, "wiki")
    assert any("与所在目录 wiki/ 不一致" in e for e in errors)


def test_missing_summary_is_an_error_and_long_summary_a_warning(tmp_path):
    p = _write(tmp_path, "wiki", "WK-0001", "title: x\ntopics: [conn]\nsource: s\n", "## 正文\n没有摘要\n")
    assert any("摘要" in e for e in kbtypes.parse_item(p.read_text(encoding="utf-8"), p, "wiki")[1])
    p = _write(tmp_path, "wiki", "WK-0002", "title: x\ntopics: [conn]\nsource: s\n", "## 摘要\n" + "长" * 400 + "\n")
    _i, errors, warns = kbtypes.parse_item(p.read_text(encoding="utf-8"), p, "wiki")
    assert errors == [] and any("400 字" in w for w in warns)


@pytest.mark.parametrize("leak", ["主库 10.0.0.9", "db01.bank.local:5432", "password: x"])
def test_profile_refuses_addresses_and_passwords(tmp_path, leak):
    p = _write(tmp_path, "profile", "EP-CBST", "title: x\nsystem: CBST\n", f"## 摘要\nCBST。\n## 部署拓扑\n{leak}\n")
    _i, errors, _w = kbtypes.parse_item(p.read_text(encoding="utf-8"), p, "profile")
    assert any("只能写连接别名" in e for e in errors)


def test_load_items_skips_broken_and_reports_duplicates(tmp_path):
    _write(tmp_path, "wiki", "WK-0001", "title: a\ntopics: [conn]\nsource: s\n")
    _write(tmp_path, "wiki", "WK-0002", "title: b\n")                       # 缺 source / topics
    items, findings = kbtypes.load_items(tmp_path)
    assert [i.id for i in items] == ["WK-0001"]
    assert any(lvl == "error" and "WK-0002" in m for lvl, m in findings)


def test_next_id_never_reuses_an_alias(tmp_path):
    _write(tmp_path, "wiki", "WK-0003", "title: a\ntopics: [conn]\nsource: s\naliases: [WK-0007]\n")
    assert kbtypes.next_id(tmp_path, "wiki") == "WK-0008", "旧 ID WK-0007 被引用过,不得复用"
    assert kbtypes.next_id(tmp_path, "runbook", "lck") == "RB-LCK-001"
    assert kbtypes.next_id(tmp_path, "profile", "cbst") == "EP-CBST"
    assert kbtypes.next_id(tmp_path, "baseline", "CBST/active sessions") == "BL-CBST-ACTIVE_SESSIONS"


# ---------------------------------------------------------------- 专题

@pytest.mark.parametrize("code,dim,want", [
    ("LOCK_WAIT_LONG", "", "lock"), ("CONN_HIGH", "Overview", "conn"), ("WIN_TEMP_SPILL", "", "mem"),
    ("CKPT_PRESSURE", "", "wal"), ("REPL_LAG", "", "repl"), ("VACUUM_DEAD_RATIO", "", "vacuum"),
    ("X1", "Wait Events / Classes", "wait"), ("seq_scan", "sqltune", "sql"),
])
def test_findings_map_to_topics_by_code_then_dimension(code, dim, want):
    assert want in kbtopics.finding_topics(code, dim, "", kbtopics.DEFAULT_TOPICS)


def test_findings_fall_back_to_evidence_words():
    assert kbtopics.finding_topics("Z9", "", "日终批量跑不完", kbtopics.DEFAULT_TOPICS)[0] == "batch"


def test_taxonomy_file_overrides_extends_and_disables(tmp_path):
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "_taxonomy.yaml").write_text(
        "- {slug: lock, name: 锁, synonyms: [卡死]}\n- {slug: batch, disabled: true}\n- {slug: xa, name: 分布式事务}\n"
        "- {slug: Bad Slug}\n", encoding="utf-8")
    topics, warns = kbtopics.load_taxonomy(tmp_path)
    by = kbtopics.by_slug(topics)
    assert by["lock"].name == "锁" and by["lock"].synonyms == ("卡死",) and by["lock"].codes  # 没写的键沿用默认
    assert "batch" not in by and "xa" in by and len(warns) == 1


def test_broken_taxonomy_falls_back_to_defaults(tmp_path):
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "_taxonomy.yaml").write_text("{not a list", encoding="utf-8")
    topics, warns = kbtopics.load_taxonomy(tmp_path)
    assert len(topics) == len(kbtopics.DEFAULT_TOPICS) and warns


# ---------------------------------------------------------------- 版本与适用性

@pytest.mark.parametrize("spec,ver,want", [
    ("505.2", "505.2.0", True), ("505", "505.2.1", True), ("505.*", "503.1", False),
    (">= 503", "505.2", True), ("≥503", "502.9", False), ("< 505.2", "505.1.9", True),
    ("505.1–505.2", "505.2.0", True), ("505.1-505.2", "505.3", False), ("503, 505.2", "505.2.1", True),
    ("*", None, True), ("最新版", "505.2", None), ("505.2", None, None),
])
def test_version_matches(spec, ver, want):
    assert kbv.version_matches(spec, ver) is want


@pytest.mark.parametrize("text,want", [
    ("GaussDB 集中式", "GaussDB 集中式"), ("openGauss 5.0", "openGauss"), ("GaussDB(DWS)", "GaussDB 分布式"),
    ("PostgreSQL 14", "PostgreSQL"), ("Oracle", ""),
])
def test_family(text, want):
    assert kbv.family(text) == want


PROFILE = {"system": "CBST", "product": "GaussDB 集中式", "version": "505.2.0", "aliases": ["og5"]}


def test_judge_three_states():
    assert kbv.judge({}, PROFILE).state == kbv.APPLICABLE, "没声明边界的条目不打标签"
    assert kbv.judge({"product": "openGauss", "versions": "5.*"}, PROFILE).state == kbv.NOT_APPLICABLE
    assert kbv.judge({"applies": {"systems": ["CBMS"]}}, PROFILE).state == kbv.NOT_APPLICABLE
    assert kbv.judge({"applies": {"versions": ">= 503"}}, PROFILE).state == kbv.APPLICABLE
    assert kbv.judge({"applies": {"versions": ">= 503"}}, None).state == kbv.UNKNOWN
    assert kbv.judge({"applies": {"versions": "最新"}}, PROFILE).state == kbv.UNKNOWN


def test_defect_verdict_reads_fix_and_range():
    d = {"affected_versions": "505.1–505.2", "fixed_in": "505.2.1"}
    assert "受影响" in kbv.defect_verdict(d, PROFILE) and "不受影响" not in kbv.defect_verdict(d, PROFILE)
    assert "已含修复" in kbv.defect_verdict(d, {**PROFILE, "version": "505.2.1"})
    assert "未知" in kbv.defect_verdict(d, None)


def test_profile_matching_by_alias_is_case_insensitive():
    assert kbv.first_match([PROFILE], "OG5") is PROFILE and kbv.first_match([PROFILE], "og6") is None
