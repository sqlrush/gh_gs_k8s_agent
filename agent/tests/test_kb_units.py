"""DB-free unit tests for the kb skill.

Written after a review found four defects that a mocked-DB suite would never
catch but plain unit tests would have: a contract injection that could delete
the body of a SKILL.md, a template backslash blowing up re.sub, a GBK file
crashing validate, and a .doc conversion timeout escaping as a traceback.
Each of those has a test here.
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "skills" / "gaussdb-kb-import" / "scripts" / "kb.py"


def _real_skill_md(name: str) -> pathlib.Path:
    """部署仓目录带 gaussdb- 前缀,主干不带——两种都认。"""
    for cand in (_ROOT / "skills" / name, _ROOT / "skills" / ("gaussdb-" + name)):
        if (cand / "SKILL.md").is_file():
            return cand / "SKILL.md"
    return _ROOT / "skills" / name / "SKILL.md"

spec = importlib.util.spec_from_file_location("kb", _SCRIPT)
kb = importlib.util.module_from_spec(spec)
sys.modules["kb"] = kb
spec.loader.exec_module(kb)

# search 在查询 skill(gaussdb-kb)里;读文件的辅助两边共用同一份 common/kb/rulesfile。
# 模块名故意不叫 "kb":那个名字是导入侧的,别顶掉。
_qspec = importlib.util.spec_from_file_location(
    "gaussdb_kb_kb", _ROOT / "skills" / "gaussdb-kb" / "scripts" / "kb.py")
kbq = importlib.util.module_from_spec(_qspec)
_qspec.loader.exec_module(kbq)


def _template() -> str:
    return kb.load_contract_template()


def _skill_md(tmp_path, body: str) -> pathlib.Path:
    p = tmp_path / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# contract — the injection must never touch anything outside its markers
# --------------------------------------------------------------------------
_BODY = (
    "---\nname: demo\n---\n\n# Demo\n\n## 工作流\n\n1. 步骤 A\n2. 步骤 B\n\n"
    "## 安全红线\n\n- 绝不删库\n- 绝不改数据\n"
)


def test_apply_appends_block_and_keeps_the_body(tmp_path):
    md = _skill_md(tmp_path, _BODY)
    kb.apply_contract(md, _template())
    out = md.read_text(encoding="utf-8")
    assert "## 安全红线" in out and "绝不删库" in out
    assert kb.CONTRACT_BEGIN in out and kb.CONTRACT_END in out


def test_apply_is_idempotent(tmp_path):
    md = _skill_md(tmp_path, _BODY)
    kb.apply_contract(md, _template())
    once = md.read_text(encoding="utf-8")
    kb.apply_contract(md, _template())
    assert md.read_text(encoding="utf-8") == once
    assert once.count(kb.CONTRACT_BEGIN) == 1


def test_apply_refuses_a_broken_marker_instead_of_eating_the_body(tmp_path):
    """Regression: a BEGIN with no END made apply append a second block; the next
    apply then matched BEGIN..END *across the body* and replaced the lot — the
    '## 安全红线' section silently vanished. A half-written marker is a human
    problem: fail loudly, never guess."""
    md = _skill_md(tmp_path, _BODY.replace(
        "## 安全红线", kb.CONTRACT_BEGIN + " -->\n## 半截契约\n\n## 安全红线"))
    before = md.read_text(encoding="utf-8")

    with pytest.raises(kb.KbError) as exc:
        kb.apply_contract(md, _template())

    assert md.read_text(encoding="utf-8") == before, "拒绝写入时必须一字不改"
    assert "BEGIN" in str(exc.value) or "标记" in str(exc.value)


def test_apply_survives_a_backslash_in_the_template(tmp_path):
    """Regression: pattern.sub() parses backslash escapes in the replacement, so
    a regex in the template (`grep -E "\\d{3}"`) raised re.error. references/ is
    meant to be edited by users, and the template literally teaches grep."""
    md = _skill_md(tmp_path, _BODY)
    kb.apply_contract(md, _template())          # existing block -> update path
    tricky = (kb.CONTRACT_BEGIN + " -->\n## 契约\n\n"
              r'定位：grep -E "\d{3}" <kb>/rules' + "\n" + kb.CONTRACT_END + "\n")
    kb.apply_contract(md, tricky)
    assert r'\d{3}' in md.read_text(encoding="utf-8")


def test_contract_status_transitions(tmp_path):
    md = _skill_md(tmp_path, _BODY)
    assert kb.contract_status(md, _template()) == "missing"
    kb.apply_contract(md, _template())
    assert kb.contract_status(md, _template()) == "current"
    stale = kb.CONTRACT_BEGIN + " -->\n## 旧契约\n" + kb.CONTRACT_END + "\n"
    assert kb.contract_status(md, stale) == "stale"


def test_contract_status_flags_a_broken_marker(tmp_path):
    md = _skill_md(tmp_path, _BODY + "\n" + kb.CONTRACT_BEGIN + " -->\n## 半截\n")
    assert kb.contract_status(md, _template()) == "broken"


# --------------------------------------------------------------------------
# contract — scope: only skills that actually make normative judgements
# --------------------------------------------------------------------------
def test_only_governance_and_diagnostic_skills_get_the_contract():
    """slowsql/sqlfetch/explain just fetch rows — telling them to consult a
    standards KB before answering is noise. The judging skills get it."""
    for want in ("sqlreview", "health", "wdr", "memanalyze", "sqltune", "proctune"):
        assert want in kb.CONTRACT_SKILLS, f"{want} 应当注入契约"
    for skip in ("slowsql", "topsql", "sqlfetch", "explain", "topproc", "procinfo"):
        assert skip not in kb.CONTRACT_SKILLS, f"{skip} 是纯取数 skill,不应注入"
    assert "kb" not in kb.CONTRACT_SKILLS       # 管理者不是消费者


def test_contract_targets_are_filtered_by_the_whitelist(tmp_path):
    for name in ("sqlreview", "slowsql", "kb"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(_BODY, encoding="utf-8")
    picked = [p.parent.name for p in kb.contract_targets(tmp_path)]
    assert picked == ["sqlreview"]


# --------------------------------------------------------------------------
# contract — the precedence the block states must match the agreed governance
# --------------------------------------------------------------------------
def test_contract_puts_the_skill_above_the_kb():
    """Agreed boundary: a skill's own SKILL.md and its script's deterministic
    verdict outrank the KB; the KB outranks the model's own knowledge. The KB
    governs *what the standards say*, not *how a skill works*."""
    text = _template()
    assert "SKILL.md" in text, "契约必须点明 skill 自身策略的位置"
    # 不能再出现「一律以知识库为准」这种压过 skill 判定的措辞
    assert "一律以知识库为准" not in text
    assert "自带知识" in text                     # KB 仍高于模型自带知识


# --------------------------------------------------------------------------
# encoding — a Chinese spec is very likely GBK
# --------------------------------------------------------------------------
def _kb_dir(tmp_path) -> pathlib.Path:
    d = tmp_path / "kb"
    kb.ensure_kb_skeleton(d)
    return d


def test_validate_tolerates_a_gbk_guide(tmp_path):
    """Regression: read_text(encoding='utf-8') raised UnicodeDecodeError and the
    whole validate died. ingest already handles gb18030 — the two halves of the
    same tool disagreed about encoding."""
    d = _kb_dir(tmp_path)
    (d / "guides" / "gbk.md").write_bytes(
        "---\nid: G-1\ndescription: 中文指南\n---\n正文\n".encode("gb18030"))
    findings = []
    kb.validate_guides(d, findings)              # 不得抛异常
    assert not [m for lvl, m in findings if lvl == "error"]


def test_search_tolerates_a_gbk_file(tmp_path, capsys):
    # search 在查询 skill(gaussdb-kb)里;读文件的辅助两边共用同一份 common/kb/rulesfile
    qspec = importlib.util.spec_from_file_location(
        "gaussdb_kb_kb", _ROOT / "skills" / "gaussdb-kb" / "scripts" / "kb.py")
    kbq = importlib.util.module_from_spec(qspec)
    qspec.loader.exec_module(kbq)
    d = _kb_dir(tmp_path)
    (d / "errata" / "e.md").write_bytes("索引必须以 idx_ 开头\n".encode("gb18030"))
    args = type("A", (), {"kb": str(d), "keyword": "idx_"})()
    assert kbq.cmd_search(args) == 0
    assert "idx_" in capsys.readouterr().out


# --------------------------------------------------------------------------
# .doc conversion
# --------------------------------------------------------------------------
def test_doc_conversion_timeout_becomes_a_kberror(monkeypatch, tmp_path):
    """A 120s hang must reach the user as '请先另存为 .txt/.docx', not a traceback."""
    monkeypatch.setattr(kb.shutil, "which", lambda _c: "/usr/bin/fake")

    def _hang(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="textutil", timeout=120)

    monkeypatch.setattr(kb.subprocess, "run", _hang)
    with pytest.raises(kb.KbError):
        kb.extract_doc(tmp_path / "x.doc")


# --------------------------------------------------------------------------
# validate — rule schema
# --------------------------------------------------------------------------
def _write_rules(d: pathlib.Path, entries) -> None:
    import yaml
    (d / "rules" / "t.yaml").write_text(
        yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8")


_GOOD = {"id": "GS-IDX-001", "severity": "error", "check": "deterministic",
         "rule": "索引名必须以 idx_ 开头", "source": "第 3 章"}


def test_validate_accepts_a_well_formed_rule(tmp_path):
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD])
    findings = []
    kb.validate_rules(d, findings)
    assert [m for lvl, m in findings if lvl == "error"] == []


@pytest.mark.parametrize("mutation, needle", [
    ({"id": "IDX-1"}, "格式"),
    ({"severity": "fatal"}, "severity"),
    ({"check": "magic"}, "check"),
    ({"rule": ""}, "必填"),
])
def test_validate_rejects_bad_rules(tmp_path, mutation, needle):
    d = _kb_dir(tmp_path)
    _write_rules(d, [dict(_GOOD, **mutation)])
    findings = []
    kb.validate_rules(d, findings)
    errors = [m for lvl, m in findings if lvl == "error"]
    assert any(needle in m for m in errors), errors


def test_validate_rejects_duplicate_rule_ids(tmp_path):
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD, dict(_GOOD, rule="另一条")])
    findings = []
    kb.validate_rules(d, findings)
    assert any("重复" in m for lvl, m in findings if lvl == "error")


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------
def test_split_frontmatter():
    meta, err = kb.split_frontmatter("---\nid: G1\n---\n正文\n")
    assert err is None and meta["id"] == "G1"
    _, err = kb.split_frontmatter("---\nid: G1\n正文\n")
    assert err is not None                       # 起始 --- 没有结束 ---
    assert kb.split_frontmatter("# 无 frontmatter\n") == ({}, None)


def test_slugify_never_escapes_the_kb_directory():
    assert kb.slugify("../../etc/passwd") == "etc-passwd"
    assert kb.slugify("客户规范 v2.1") == "客户规范-v2-1"
    assert kb.slugify("!!!") == "imported"


def test_detect_outline_picks_chinese_headings():
    text = "第一章 总则\n正文一段\n1.2 索引规范\n" + "x" * 80 + "\n"
    titles = [t for _, t in kb.detect_outline(text)]
    assert "第一章 总则" in titles
    assert "1.2 索引规范" in titles
    assert not any(len(t) > kb.MAX_HEADING_LEN for t in titles)


# --------------------------------------------------------------------------
# end-to-end (still DB-free)
# --------------------------------------------------------------------------
def test_ingest_index_validate_round_trip(tmp_path):
    src = tmp_path / "spec.md"
    src.write_text("# 规范\n\n## 1. 索引\n\n索引名必须以 idx_ 开头。\n", encoding="utf-8")
    d = tmp_path / "kb"

    def run(*argv):
        return subprocess.run([sys.executable, str(_SCRIPT), *argv, "--kb", str(d)],
                              capture_output=True, text=True)

    assert run("ingest", str(src)).returncode == 0
    assert (d / "inbox" / "spec" / "source.md").is_file()
    assert (d / "sources" / "spec.md").is_file()      # 原文快照可追溯
    assert run("index").returncode == 0
    assert (d / "INDEX.md").is_file()
    assert run("validate").returncode == 0            # 只有 inbox 未清空的 warn


# --------------------------------------------------------------------------
# governance — the boundary must hold in the SKILL.md files themselves
# --------------------------------------------------------------------------
def test_sqlreview_scopes_its_single_source_claim():
    """The injected contract used to contradict sqlreview head-on: line 15 said
    rules.yaml was 「规范的唯一来源」 while the contract said the KB was
    「唯一权威来源」, and a third rule forbade reporting anything the script did
    not. Whichever way the wording drifts, the two must stay reconcilable."""
    md = _real_skill_md("sqlreview").read_text(encoding="utf-8")
    assert "规范的唯一来源" not in md, "无限定的「规范的唯一来源」会与知识库契约打架"
    assert "确定性判定规则**的唯一来源" in md or "确定性判定规则" in md
    assert "不得凭知识库补报" in md          # 判定边界写死在 skill 里


def test_every_contracted_skill_carries_exactly_one_block():
    for name in sorted(kb.CONTRACT_SKILLS):
        md = _real_skill_md(name)
        if not md.is_file():
            continue
        text = md.read_text(encoding="utf-8")
        assert text.count(kb.CONTRACT_BEGIN) == 1, f"{name}: 契约块数量异常"
        assert text.count(kb.CONTRACT_END) == 1, f"{name}: 契约块数量异常"


def test_every_contracted_skill_carries_the_current_block():
    """改了 kb-contract.md 要再跑 contract --apply——不然技能里还是旧契约,新规矩(比如标蓝)模型根本看不到。"""
    for name in sorted(kb.CONTRACT_SKILLS):
        md = _real_skill_md(name)
        if md.is_file():
            assert kb.contract_status(md, _template()) == "current", f"{name}: 契约块不是最新,跑 kb.py contract --apply"


# 2026-10-24:对话界面里出自知识库的内容显示成蓝色。opencode 网页渲染行内 HTML(DOMPurify 保留 font 的 color)。
# 用最简单的 <font color=#2f6fdb>:测试环境里模型把 style="…" 的引号写成中文引号、把 color: 写成 color=,都不变色。
# 固定中蓝,浅色 / 深色主题对比度都在 4 以上。三处写法必须一字不差,模型照抄。
KB_MARK = '<font color=#2f6fdb>'


# 2026-10-26 回答模板化:官方文档集换青色,与客户知识分开;句后括号注明出处(引用自 xxx 库 编号)。
OFFICIAL_MARK = '<font color=#0f7c86>'


def test_contract_tells_the_model_to_mark_kb_content_blue():
    text = _template()
    assert KB_MARK in text and OFFICIAL_MARK in text
    assert "不上色" in text, "要写明哪些不上色:诊断结果、你自己的分析、通用经验"
    assert "(引用自" in text and "贵行规范库" in text and "GaussDB 官方文档库" in text, "句后括号写出处,库名按类型"
    assert "知识库中未找到相关内容,以下为通用知识,仅供参考" in text, "兜底说法统一成这一种"
    assert "代码块里不放标签" in text, "代码块里的标签不变色,还会原样露出来"
    assert "不加引号" in text and "加粗" in text, "引号被写成中文引号、标签里加粗,都会让蓝色失效(测试环境实测)"


def test_blue_tag_has_no_quotes_to_mangle():
    """2026-10-24 测试环境:模型把 style="…" 写成了 style=“…”,整段不变色。属性值不含空格与等号,可以不加引号。"""
    for mark in (KB_MARK, OFFICIAL_MARK):
        assert '"' not in mark and "'" not in mark


@pytest.mark.parametrize("rel", ["skills/gaussdb-kb/SKILL.md", "AGENTS.md"])
def test_kb_skill_and_agents_md_use_the_same_blue_tag(rel):
    assert KB_MARK in (_ROOT / rel).read_text(encoding="utf-8"), f"{rel} 的标蓝写法与契约不一致"


def test_fetch_only_skills_stay_clean():
    for name in ("slowsql", "topsql", "sqlfetch", "explain", "topproc", "procinfo"):
        md = _real_skill_md(name)
        assert kb.CONTRACT_BEGIN not in md.read_text(encoding="utf-8"), \
            f"{name} 是纯取数 skill,不该被注入契约"


# --------------------------------------------------------------------------
# KB 落点：与 skill 装在一起（skills/ 的同级目录，不是 skill 目录内部）
#
# 不能放 skills/<name>/kb —— install-opencode.sh 每次重装 `rm -rf` 整个 skill
# 目录，客户导入的知识库会被删光。同级目录 install 从不触碰(已实测)。
# --------------------------------------------------------------------------
import os


@pytest.mark.parametrize("script, want_root", [
    ("/Users/x/.config/opencode/skills/gaussdb-kb/scripts/kb.py",
     "/Users/x/.config/opencode"),                      # 全局安装
    ("/Users/x/proj/.opencode/skills/gaussdb-kb/scripts/kb.py",
     "/Users/x/proj/.opencode"),                        # 项目安装
    ("/Users/x/opencode_skill/skills/gaussdb-kb/scripts/kb.py",
     "/Users/x/opencode_skill"),                        # 源码仓直跑
])
def test_install_root_is_derived_from_the_script_location(script, want_root):
    assert kb.install_root(pathlib.Path(script)) == pathlib.Path(want_root)


def test_kb_defaults_to_the_install_root(monkeypatch):
    monkeypatch.delenv("GSDB_KB_DIR", raising=False)
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.delenv("GDAA_HOME", raising=False)
    assert kb.resolve_kb_dir(None) == kb.install_root() / "kb"


def test_kb_dir_env_var_overrides_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_KB_DIR", str(tmp_path / "custom"))
    assert kb.resolve_kb_dir(None) == tmp_path / "custom"


def test_explicit_kb_flag_beats_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_KB_DIR", str(tmp_path / "env"))
    assert kb.resolve_kb_dir(str(tmp_path / "cli")) == tmp_path / "cli"


def test_kb_never_lands_inside_a_skill_directory(monkeypatch):
    """The whole point: a reinstall must not be able to delete the KB."""
    monkeypatch.delenv("GSDB_KB_DIR", raising=False)
    parts = kb.resolve_kb_dir(None).parts
    assert "skills" not in parts, "知识库落在 skills/ 内 —— 重装会被 rm -rf 删掉"


def test_contract_points_skills_at_the_new_kb_location():
    text = _template()
    assert "~/.gdaa/kb" not in text, "契约块仍指向旧路径"
    assert "kb" in text


def test_contract_uses_a_placeholder_the_installer_substitutes():
    """{kbDir} 必须被 install-opencode.sh 替换,否则装完 SKILL.md 里会留下字面量。"""
    assert "{kbDir}" in _template()
    installer = (_ROOT / "install-opencode.sh").read_text(encoding="utf-8")
    assert "{kbDir}" in installer, "安装脚本没有替换 {kbDir}"


# --------------------------------------------------------------------------
# 编码：非 UTF-8 文件会被各 skill 的 grep 静默漏掉
#
# 消费者 skill 走的是契约块里的 `grep -rn "<关键词>" <kb>/...`。grep 拿 LLM 敲的
# UTF-8 字节去比对一个 GB18030 文件,字节层面对不上——**不报错,就是"没找到"**。
# 于是客户明明把规范写进了库里,模型却回「知识库未覆盖,以下为通用经验」,
# 正是本 skill 要防的那种谎话。validate 必须在导入时就把它拦下来。
# --------------------------------------------------------------------------
_GBK_RULE = "- id: GS-IDX-001\n  severity: warn\n  check: deterministic\n  rule: 索引命名\n"
_GBK_MD = "---\nid: G-1\ndescription: 中文指南\n---\n索引命名必须遵循前缀规范\n"


@pytest.mark.parametrize("sub, name, body", [
    ("guides", "gbk.md", _GBK_MD),
    ("errata", "gbk.md", "索引命名的例外情况\n"),
    ("rules", "gbk.yaml", _GBK_RULE),
])
def test_validate_rejects_non_utf8_files_in_searchable_dirs(tmp_path, sub, name, body):
    d = _kb_dir(tmp_path)
    (d / sub / name).write_bytes(body.encode("gb18030"))
    findings = []
    kb.validate_encoding(d, findings)
    errors = [m for lvl, m in findings if lvl == "error"]
    assert any(name in m for m in errors), f"{sub}/{name} 未被报为编码错误：{findings}"
    assert any("UTF-8" in m for m in errors)
    assert any("grep" in m for m in errors), "报错必须说清后果：grep 会静默漏掉它"


def test_validate_accepts_utf8_files(tmp_path):
    d = _kb_dir(tmp_path)
    (d / "guides" / "ok.md").write_text(_GBK_MD, encoding="utf-8")
    (d / "rules" / "ok.yaml").write_text(_GBK_RULE, encoding="utf-8")
    findings = []
    kb.validate_encoding(d, findings)
    assert findings == []


def test_original_snapshots_keep_their_encoding(tmp_path):
    """sources/ 是原文快照,本来就该保留客户给的编码;检索也不扫它,不该报错。"""
    d = _kb_dir(tmp_path)
    (d / "sources" / "客户规范.txt").write_bytes("索引命名".encode("gb18030"))
    findings = []
    kb.validate_encoding(d, findings)
    assert findings == []


def test_cmd_validate_wires_in_the_encoding_check(tmp_path):
    """端到端:validate 子命令必须真的跑这一项,退出码为 2。"""
    d = _kb_dir(tmp_path)
    (d / "guides" / "gbk.md").write_bytes(_GBK_MD.encode("gb18030"))
    (d / "INDEX.md").write_text("# idx\n- `guides/gbk.md` — x\n", encoding="utf-8")
    args = type("A", (), {"kb": str(d)})()
    assert kb.cmd_validate(args) == 2


def test_encoding_error_suggests_a_command_that_actually_runs(tmp_path):
    """回归:原先建议 `iconv ... -o file` —— macOS/BSD 的 iconv 没有 -o,
    而且原地覆写会先把文件截断成空。跑不通的修复建议比不给更糟。"""
    d = _kb_dir(tmp_path)
    (d / "guides" / "gbk.md").write_bytes(_GBK_MD.encode("gb18030"))
    findings = []
    kb.validate_encoding(d, findings)
    msg = findings[0][1]
    assert " -o " not in msg, "iconv -o 在 macOS/BSD 上不存在"
    assert ">" in msg and "mv" in msg, "应给出可移植的重定向 + mv 写法"


# --------------------------------------------------------------------------
# 换版治理:废止条款必须离开 rules/,否则各 skill 的 grep 仍会命中它
#
# 契约块(kb-contract.md)让各 skill 用
#     grep -rn "<关键词>" <kb>/errata <kb>/rules <kb>/guides
# 定位条款。grep -rn 只输出**命中行**,所以一条标了 status: deprecated 的条款,
# 模型搜「外键」时看到的是 `rules/t.yaml:12: rule: 禁止使用外键约束` —— 它
# 看不到 status 那一行,照样会按已废止的规范判客户违规。
#
# 所以废止必须是**物理隔离**:条款移进 archive/,而 archive/ 不在 grep 范围内。
# status 字段的作用是让 validate 能双向校验「移了没标」和「标了没移」。
# --------------------------------------------------------------------------
def _write_archive(d: pathlib.Path, entries, name: str = "t.yaml") -> None:
    import yaml
    (d / "archive" / name).write_text(
        yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8")


_DEPRECATED = dict(_GOOD, id="GS-TBL-002", rule="禁止使用外键约束",
                   status="deprecated", superseded_by="V2.2 已废止,无替代条款")


def test_archive_is_outside_the_grep_range_the_contract_gives_the_skills():
    """整个设计的支点:契约块的 grep 范围必须**不含** archive/。

    这条一旦破了(有人好心把 archive 加进 grep 列表),废止条款就会重新被各 skill
    搜到,物理隔离失效,而且不会有任何别的测试报警。"""
    contract = kb.load_contract_template()
    grep_line = next(ln for ln in contract.splitlines() if "grep -rn" in ln)
    assert "errata" in grep_line and "rules" in grep_line and "guides" in grep_line
    assert "archive" not in grep_line, "archive/ 绝不能进 grep 范围,否则废止条款会复活"


def test_archive_is_part_of_the_kb_skeleton(tmp_path):
    d = _kb_dir(tmp_path)
    assert (d / "archive").is_dir()


def test_a_rule_without_status_is_active(tmp_path):
    """向后兼容:存量条款没有 status 字段,必须一律视为现行有效,不得报错。"""
    assert kb.rule_status(_GOOD) == "active"
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD])
    findings = []
    kb.validate_rules(d, findings)
    assert [m for lvl, m in findings if lvl == "error"] == []


def test_validate_rejects_a_deprecated_rule_left_in_rules_dir(tmp_path):
    """核心:标了废止却没移走 —— 各 skill 的 grep 照样命中它,是静默失效。"""
    d = _kb_dir(tmp_path)
    _write_rules(d, [_DEPRECATED])
    findings = []
    kb.validate_rules(d, findings)
    errors = [m for lvl, m in findings if lvl == "error"]
    assert any("archive" in m and "GS-TBL-002" in m for m in errors), errors


def test_validate_rejects_an_active_rule_parked_in_archive(tmp_path):
    """反向:移走了却没标废止 —— 一条现行条款被静默地移出了检索范围。"""
    d = _kb_dir(tmp_path)
    _write_archive(d, [_GOOD])
    findings = []
    kb.validate_rules(d, findings)
    errors = [m for lvl, m in findings if lvl == "error"]
    assert any("deprecated" in m and "GS-IDX-001" in m for m in errors), errors


def test_validate_rejects_an_unknown_status(tmp_path):
    d = _kb_dir(tmp_path)
    _write_rules(d, [dict(_GOOD, status="retired")])
    findings = []
    kb.validate_rules(d, findings)
    assert any("status" in m for lvl, m in findings if lvl == "error")


def test_validate_warns_when_a_deprecated_rule_says_nothing_about_why(tmp_path):
    d = _kb_dir(tmp_path)
    entry = dict(_DEPRECATED)
    del entry["superseded_by"]
    _write_archive(d, [entry])
    findings = []
    kb.validate_rules(d, findings)
    assert any("superseded_by" in m for lvl, m in findings if lvl == "warn")


def test_rule_ids_stay_unique_across_rules_and_archive(tmp_path):
    """ID 永不复用:废止一条之后,新条款不得占用它的 ID。跨目录查重才拦得住。

    (只断言「拦住了」;报错该点谁的名、怎么措辞,由
    test_reusing_an_archived_id_blames_the_new_rule_not_the_archived_one 管。)"""
    d = _kb_dir(tmp_path)
    _write_archive(d, [_DEPRECATED])
    _write_rules(d, [dict(_GOOD, id="GS-TBL-002", rule="一条占用了废止 ID 的新条款")])
    findings = []
    kb.validate_rules(d, findings)
    assert any("GS-TBL-002" in m for lvl, m in findings if lvl == "error")


def test_search_does_not_reach_archived_rules(tmp_path, capsys):
    """search 与 grep 必须口径一致:废止条款搜不到。"""
    d = _kb_dir(tmp_path)
    _write_archive(d, [_DEPRECATED])
    args = type("A", (), {"kb": str(d), "keyword": "外键", "include_archived": False})()
    assert kbq.cmd_search(args) == 0
    out = capsys.readouterr().out
    assert "禁止使用外键约束" not in out
    assert "未命中" in out


def test_search_can_reach_the_archive_when_explicitly_asked(tmp_path, capsys):
    """人工排查换版历史时要能搜到,但必须显式要求,且结果标注已废止。"""
    d = _kb_dir(tmp_path)
    _write_archive(d, [_DEPRECATED])
    args = type("A", (), {"kb": str(d), "keyword": "外键", "include_archived": True})()
    assert kbq.cmd_search(args) == 0
    out = capsys.readouterr().out
    assert "禁止使用外键约束" in out
    assert "已废止" in out


def test_index_lists_archived_rules_but_marks_them_deprecated(tmp_path):
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD])
    _write_archive(d, [_DEPRECATED])
    kb.cmd_index(type("A", (), {"kb": str(d)})())
    index = (d / "INDEX.md").read_text(encoding="utf-8")
    assert "GS-TBL-002" in index, "模型必须知道这条 ID 存在过(报告可追溯)"
    assert "已废止" in index
    assert "不得据此判定" in index, "光列出来不够,要说清它不能用来判违规"


def test_validate_checks_the_encoding_of_archived_files(tmp_path):
    """archive/ 虽不进 grep,但 validate 要解析它 —— 非 UTF-8 会让校验静默漏掉。"""
    d = _kb_dir(tmp_path)
    (d / "archive" / "old.yaml").write_bytes(
        "- id: GS-TBL-002\n  rule: 禁止外键\n".encode("gb18030"))
    findings = []
    kb.validate_encoding(d, findings)
    assert any("archive" in m for lvl, m in findings if lvl == "error")


def test_ingest_flags_a_re_import_so_the_model_cannot_forget_to_reconcile(tmp_path):
    """换版导入:库里已有条款时,ingest 必须喊出来。

    脚本判定不了「哪条该废止」(语义活),但它能确定性地判定「这是第二次导入」,
    并挡住『导完新版就忘了旧条款还在』这条静默失效路径。"""
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD])
    src = tmp_path / "spec-v2.md"
    src.write_text("# 规范 V2\n\n索引名必须以 ix_ 开头。\n", encoding="utf-8")

    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "ingest", str(src), "--kb", str(d)],
        capture_output=True, text=True)
    assert out.returncode == 0
    assert "换版" in out.stdout
    assert "1 条" in out.stdout, "要报出库里现有多少条款"
    assert "archive" in out.stdout, "要指明废止条款该去哪"


def test_search_does_not_claim_a_miss_when_it_just_showed_archived_hits(tmp_path, capsys):
    """回归:--include-archived 打印了废止条款,却又跟一句「未命中」—— 自相矛盾。

    「未命中」这句话是给模型的纪律(不得用自带知识冒充规范),它说的是**现行条款**
    里没有。既然已经列出了废止条款,就该说清「现行条款未命中,上列已废止」,
    而不是让读者在两句互相打架的话里自己猜。"""
    d = _kb_dir(tmp_path)
    _write_archive(d, [_DEPRECATED])
    args = type("A", (), {"kb": str(d), "keyword": "外键", "include_archived": True})()
    kbq.cmd_search(args)
    out = capsys.readouterr().out
    assert "禁止使用外键约束" in out
    # 光秃秃的「未命中:'外键'(KB=...)」那一行不能出现——它跟上面刚列出的命中行打架。
    # 「现行条款未命中」是另一回事:它说清了「现行的没有,上面那些是废止的」。
    assert "未命中:'外键'(KB=" not in out, "已经列出命中行了,不能再甩一句光秃秃的未命中"
    assert "现行条款未命中" in out and "已废止" in out


def test_reusing_an_archived_id_blames_the_new_rule_not_the_archived_one(tmp_path):
    """回归:报错指错了地方。

    seen 先被 rules/ 填充,于是 archive/ 里那条**原版**条款被报成「重复」,而真正
    违规的、复用了 ID 的新条款反倒成了被参照物。运维照着这条报错去改,会去动
    archive/ —— 正好是反方向。历史是既成事实,该被点名的永远是新条款。"""
    d = _kb_dir(tmp_path)
    _write_archive(d, [_DEPRECATED])                       # GS-TBL-002,已废止
    _write_rules(d, [dict(_GOOD, id="GS-TBL-002", rule="一条占用了废止 ID 的新条款")])
    findings = []
    kb.validate_rules(d, findings)
    errors = [m for lvl, m in findings if lvl == "error"]
    blame = [m for m in errors if "GS-TBL-002" in m]
    assert blame, errors
    assert all(m.startswith("rules/") for m in blame), \
        f"该被点名的是 rules/ 里的新条款,不是 archive/ 里的历史条款:{blame}"
    assert any("废止" in m and "复用" in m for m in blame), \
        f"报错要说清这是「复用了已废止的 ID」,而不是笼统的「重复」:{blame}"


def test_validate_keeps_index_in_step_with_the_archive(tmp_path):
    """index 会把 archive/ 写进 INDEX.md,validate 的一致性检查就必须跟着覆盖它 ——
    否则删掉一个 archive 文件会在 INDEX 里留下悬空引用,而没人报错。"""
    d = _kb_dir(tmp_path)
    _write_archive(d, [_DEPRECATED])

    findings = []                                  # ① archive 文件没进 INDEX
    (d / "INDEX.md").write_text("# idx\n(空)\n", encoding="utf-8")
    kb.validate_index(d, findings)
    assert any("archive/t.yaml" in m for lvl, m in findings if lvl == "error"), findings

    findings = []                                  # ② INDEX 引用了不存在的 archive 文件
    (d / "INDEX.md").write_text(
        "# idx\n- `archive/t.yaml` — 1 条\n- `archive/gone.yaml` — 1 条\n",
        encoding="utf-8")
    kb.validate_index(d, findings)
    assert any("gone.yaml" in m for lvl, m in findings if lvl == "error"), findings


# --------------------------------------------------------------------------
# RULES.md:现行条款的逐条全量清单(L1)
#
# INDEX.md 是**文件级**地图(一行代表一个 yaml 文件,只到「索引规范在 index.yaml」
# 这一层);而语义转化的坎藏在「文件 → 条款」这最后一跳里——模型要在文件内定位到
# 具体某条,还是得猜关键词去 grep。RULES.md 把索引颗粒度推进到**条款级**:每条
# 现行条款一行,全量摊开,模型逐条判相关性,不必猜该搜什么。
#
# 代价是引入了一个**新的**静默失效点:清单一旦与 rules/ 脱节(漏一条 / 多一条),
# 模型看到的候选集就不再完整。所以 validate 必须守住「RULES.md 的 ID 集合 == rules/
# 现行条款 ID 集合」这个双向不变式——这几个测试就是钉死这条不变式的。
# --------------------------------------------------------------------------
def _index(d: pathlib.Path) -> None:
    kb.cmd_index(type("A", (), {"kb": str(d)})())


def test_index_writes_a_rule_level_listing(tmp_path):
    """逐条,不是逐文件:两条 ID 连同条款全文都要在 RULES.md 里。"""
    d = _kb_dir(tmp_path)
    _write_rules(d, [
        dict(_GOOD, id="GS-IDX-001", rule="索引名必须以 idx_ 开头"),
        dict(_GOOD, id="GS-IDX-003", rule="单个索引列数不超过 4 列"),
    ])
    _index(d)
    listing = (d / "RULES.md").read_text(encoding="utf-8")
    assert "GS-IDX-001" in listing and "索引名必须以 idx_ 开头" in listing
    assert "GS-IDX-003" in listing and "单个索引列数不超过 4 列" in listing


def test_rule_listing_excludes_archived_clauses(tmp_path):
    """RULES.md 是「据此判定」的清单,废止条款绝不能进——否则重演静默失效。

    追溯性由 INDEX.md 的 archive 段负责,与判定清单是两回事,不能混。"""
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD])
    _write_archive(d, [_DEPRECATED])
    _index(d)
    listing = (d / "RULES.md").read_text(encoding="utf-8")
    assert "GS-TBL-002" not in listing
    assert "禁止使用外键约束" not in listing


def test_index_points_at_the_rule_listing(tmp_path):
    """INDEX.md 要把模型引到 RULES.md,否则新加的这层没人读。"""
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD])
    _index(d)
    assert "RULES.md" in (d / "INDEX.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# CASES.md —— 案例的逐条清单,与 RULES.md 同级。没配向量库/图库(文件模式)时模型靠它
# 把案例当 md 知识库加载:先扫清单挑相关案例,再读 cases/ 里的全文。
# --------------------------------------------------------------------------
_CASE_ID = "S1-20250224-CBST-偶现单条update慢"
_CASE_MD = f"""---
id: {_CASE_ID}
title: 偶现单条 update 走索引耗时 3s
system: CBST
occurred_at: 2025-02-24
conclusion: 已确认
source: sources/report.v1.docx#前言
objects: [cbst.cosp_asyn_task_dtl]
signals: [autovacuum 频繁触发]
---
## 现场
慢。
## 判断
锁。
## 处置
调大阈值。
"""


def _write_case(d: pathlib.Path, text: str = _CASE_MD, name: str = _CASE_ID) -> None:
    (d / "cases").mkdir(parents=True, exist_ok=True)
    (d / "cases" / f"{name}.md").write_text(text, encoding="utf-8")


def test_index_writes_a_case_listing(tmp_path):
    """逐条:ID、标题、系统、时间、结论强度、复发标志、文件路径都要在 CASES.md 里。"""
    d = _kb_dir(tmp_path)
    _write_case(d)
    _index(d)
    listing = (d / "CASES.md").read_text(encoding="utf-8")
    assert _CASE_ID in listing and "偶现单条 update 走索引耗时 3s" in listing
    assert "CBST" in listing and "2025-02-24" in listing and "已确认" in listing
    assert "autovacuum 频繁触发" in listing and f"cases/{_CASE_ID}.md" in listing
    assert "CASES.md" in (d / "INDEX.md").read_text(encoding="utf-8")


def test_case_listing_says_when_empty(tmp_path):
    d = _kb_dir(tmp_path)
    _index(d)
    assert "暂无案例" in (d / "CASES.md").read_text(encoding="utf-8")


def test_case_listing_flags_a_broken_case_file_instead_of_dropping_it(tmp_path):
    """坏文件要在清单里露头(⚠ + 文件名),不能静默少一条——否则模型以为库里没这个案例。"""
    d = _kb_dir(tmp_path)
    _write_case(d)
    _write_case(d, "---\nid: [\n---\n", "broken")
    _index(d)
    listing = (d / "CASES.md").read_text(encoding="utf-8")
    assert _CASE_ID in listing and "⚠" in listing and "broken" in listing


def test_validate_errors_when_the_rule_listing_is_missing(tmp_path):
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD])
    _index(d)
    (d / "RULES.md").unlink()
    findings = []
    kb.validate_rules_listing(d, findings)
    assert any("RULES.md" in m for lvl, m in findings if lvl == "error")


def test_validate_errors_when_a_rule_is_absent_from_the_listing(tmp_path):
    """核心不变式(正向):漏掉一条 = 模型永远看不见它 = 新的静默失效点。"""
    d = _kb_dir(tmp_path)
    _write_rules(d, [
        dict(_GOOD, id="GS-IDX-001", rule="第一条"),
        dict(_GOOD, id="GS-IDX-003", rule="第二条"),
    ])
    _index(d)
    p = d / "RULES.md"                              # 手工抹掉一条,模拟清单与 rules/ 脱节
    kept = [l for l in p.read_text(encoding="utf-8").splitlines() if "GS-IDX-003" not in l]
    p.write_text("\n".join(kept), encoding="utf-8")
    findings = []
    kb.validate_rules_listing(d, findings)
    assert any("GS-IDX-003" in m for lvl, m in findings if lvl == "error")


def test_validate_errors_when_the_listing_has_a_dangling_id(tmp_path):
    """核心不变式(反向):清单里多出一条 rules/ 里已不存在的 ID —— 幽灵条款。"""
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD])
    _index(d)
    p = d / "RULES.md"
    p.write_text(p.read_text(encoding="utf-8")
                 + "\n- `GS-IDX-999` · error · 一条 rules/ 里并不存在的幽灵条款\n",
                 encoding="utf-8")
    findings = []
    kb.validate_rules_listing(d, findings)
    assert any("GS-IDX-999" in m for lvl, m in findings if lvl == "error")


def test_cmd_validate_wires_in_the_rule_listing_check(tmp_path):
    """回归护栏:validate_rules_listing 必须真的被 cmd_validate 调到,不能只是定义了。

    (参照 test_cmd_validate_wires_in_the_encoding_check 的同款教训。)"""
    d = _kb_dir(tmp_path)
    _write_rules(d, [_GOOD])
    _index(d)
    (d / "RULES.md").unlink()
    rc = kb.cmd_validate(type("A", (), {"kb": str(d)})())
    assert rc == 2, "缺 RULES.md 是静默失效隐患,cmd_validate 必须判 error"


def test_contract_leads_with_the_listing_not_keyword_grep(tmp_path):
    """契约模板必须把模型引向「读 RULES.md 逐条判」,而不再把「猜关键词 grep」当主路径。"""
    tpl = kb.load_contract_template()
    assert "RULES.md" in tpl, "契约没提 RULES.md,新加的 L1 层不会被任何 skill 读到"


# --------------------------------------------------------------------------
# PDF 导入:要么干净地读出来,要么明确拒绝 —— 绝不入库半吊子文本
#
# 陷阱:扫描件 PDF 里的字是图片,没有任何文本操作符。pdftotext 对它
# **成功退出(returncode 0)、输出空字符串**。照抄 .doc 那套「退出码 0 = 成功」
# 会写出一个空的 source.md,模型对着空文档说「这份规范没有条款」—— 而客户的
# 规范明明白白印在那 30 页图片里。不报错、不崩溃,只是悄悄什么都没做。
#
# 中文规范文档里扫描件比例很高(盖了红章的基本都是扫的),所以质量闸门比
# 提取功能本身更重要。
# --------------------------------------------------------------------------
def _pdf(body_objs: list, content: bytes = b"") -> bytes:
    """手写一个最小可用 PDF(纯 stdlib,不引依赖)。"""
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(body_objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(body_objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(body_objs) + 1, xref))
    return bytes(out)


_SPEC_LINES = [
    b"Chapter 2  Index design standards for GaussDB and openGauss",
    b"2.1 Index names must start with idx_ and unique index names with uk_",
    b"2.2 A single index must not span more than five columns",
    b"2.3 Do not create a standalone B-tree index on a low-cardinality column",
    b"2.4 Redundant indexes whose columns prefix another index must be dropped",
]


def _text_pdf() -> bytes:
    """一页真实体量的规范正文。夹具必须像真文档 —— 用 30 个字符的假 PDF 去测
    「能提取」,只会证明质量闸门被绕过了。"""
    c = b"BT /F1 12 Tf 72 720 Td 14 TL\n"
    c += b"".join(b"(" + ln + b") Tj T*\n" for ln in _SPEC_LINES)
    c += b"ET\n"
    return _pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(c) + c + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ])


def _scanned_pdf(pages: int = 12) -> bytes:
    """只有矢量图形、没有任何文本操作符 —— 扫描件的等价物。"""
    c = b"0.2 g 100 500 400 200 re f\n"
    kids = b" ".join(b"%d 0 R" % (3 + i) for i in range(pages))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % pages,
    ]
    objs += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             b"/Contents %d 0 R >>" % (3 + pages + i) for i in range(pages)]
    objs += [b"<< /Length %d >>\nstream\n" % len(c) + c + b"endstream"
             for _ in range(pages)]
    return _pdf(objs)


# ---- 页数统计(纯函数) ----
def test_pdf_page_count_reads_the_page_objects():
    assert kb.pdf_page_count(_text_pdf()) == 1
    assert kb.pdf_page_count(_scanned_pdf(12)) == 12


def test_pdf_page_count_returns_zero_when_it_cannot_tell():
    """PDF 1.5+ 的对象流会把 /Type /Page 压进二进制流里,数不出来。
    数不出来就说数不出来(0),别瞎猜 —— 闸门另有绝对字符数兜底。"""
    assert kb.pdf_page_count(b"%PDF-1.5\n(compressed object streams)\n") == 0


# ---- 质量闸门(纯函数,这是整个功能的核心) ----
def test_quality_gate_passes_a_normal_extraction():
    text = "第二章 索引设计\n2.1 索引名必须以 idx_ 开头。\n2.2 索引列数不超过 5 列。\n" * 3
    assert kb.pdf_extraction_problem(text, pages=1) is None


def test_quality_gate_rejects_an_empty_extraction():
    """扫描件的典型表现:退出码 0,输出空。"""
    reason = kb.pdf_extraction_problem("", pages=12)
    assert reason is not None
    assert "扫描件" in reason and "OCR" in reason


def test_quality_gate_rejects_a_suspiciously_thin_extraction():
    """12 页只抠出几十个字 —— 页眉页脚是文本、正文是图片的混合扫描件。"""
    reason = kb.pdf_extraction_problem("第 1 页\n" * 12, pages=12)
    assert reason is not None
    assert "扫描件" in reason


def test_quality_gate_rejects_garbled_text():
    """CID 字体缺 ToUnicode CMap 时,抠出来的是私用区/替换字符 —— 看着有内容,
    实际全是垃圾。入库比空文档更糟:模型会把乱码当成规范条款。"""
    reason = kb.pdf_extraction_problem("�" * 200, pages=3)
    assert reason is not None
    assert "乱码" in reason


def test_quality_gate_tolerates_a_few_odd_characters():
    """真实文档里偶有生僻字/特殊符号,不能一见到就判乱码。"""
    text = "第二章 索引设计\n2.1 索引名必须以 idx_ 开头。\n" * 20 + "�"
    assert kb.pdf_extraction_problem(text, pages=1) is None


def test_quality_gate_still_guards_when_the_page_count_is_unknown():
    """数不出页数时(pages=0),绝对字符数下限仍然要拦住空提取。"""
    assert kb.pdf_extraction_problem("", pages=0) is not None
    assert kb.pdf_extraction_problem("三个字", pages=0) is not None


# ---- extract_pdf 的 I/O 行为 ----
def test_pdf_without_a_converter_says_how_to_install_one(monkeypatch, tmp_path):
    monkeypatch.setattr(kb.shutil, "which", lambda _c: None)
    p = tmp_path / "spec.pdf"
    p.write_bytes(_text_pdf())
    with pytest.raises(kb.KbError) as exc:
        kb.extract_pdf(p)
    msg = str(exc.value)
    assert "pdftotext" in msg and ("brew" in msg or "install" in msg)


def test_pdf_conversion_timeout_becomes_a_kberror(monkeypatch, tmp_path):
    monkeypatch.setattr(kb.shutil, "which", lambda _c: "/usr/bin/fake")

    def _hang(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="pdftotext", timeout=120)

    monkeypatch.setattr(kb.subprocess, "run", _hang)
    p = tmp_path / "spec.pdf"
    p.write_bytes(_text_pdf())
    with pytest.raises(kb.KbError):
        kb.extract_pdf(p)


def test_a_scanned_pdf_is_rendered_for_transcription_never_imported_blank(tmp_path):
    """端到端红线(2026-10-27 改):扫描件不再一律拒绝,而是每页渲染成图片交给模型看图转录 ——
    但**绝不能**出现一份看起来像正文、其实是空的 source.md。退出码 2(还有活没干完),
    source.md 里只有转录要求,validate 也点名「还没转录」。"""
    if not kb.shutil.which("pdftotext") and not kb.shutil.which("mutool"):
        pytest.skip("本机没有 PDF 转换器")
    if not kb.shutil.which("pdftoppm"):
        pytest.skip("本机没有 pdftoppm")
    d = tmp_path / "kb"
    src = tmp_path / "扫描规范.pdf"
    src.write_bytes(_scanned_pdf(12))

    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "ingest", str(src), "--kb", str(d)],
        capture_output=True, text=True)

    assert out.returncode == 2, out.stdout + out.stderr
    assert "扫描件" in out.stdout and "看图转录" in out.stdout
    inbox = d / "inbox" / "扫描规范"
    pages = sorted((inbox / "pages").glob("page-*.jpg"))
    assert len(pages) == 12 and pages[0].name == "page-01.jpg"
    stub = (inbox / "source.md").read_text(encoding="utf-8")
    assert stub.startswith("<!-- 扫描件转录稿") and "不要猜" in stub and stub.rstrip().endswith("-->"), stub
    assert not list(d.glob("rules/*.yaml")), "转录之前不能有任何条款"
    val = subprocess.run([sys.executable, str(_SCRIPT), "validate", "--kb", str(d)], capture_output=True, text=True)
    assert "还没转录" in val.stdout, val.stdout


def test_a_scanned_pdf_that_cannot_be_rendered_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    """渲染不了(没装 pdftoppm 等)时退回旧红线:非零退出,不留半成品 source.md。"""
    from common.kb import extract as kbx
    from common.kb import pdfpages

    def no_render(*_a, **_k):
        raise kbx.ExtractError("这里没有 pdftoppm")

    def scanned(_p):
        raise kbx.UnreadablePdf("PDF 提取到的文本过少……扫描件", 12, "scanned")

    monkeypatch.setattr(pdfpages, "render_pages", no_render)
    monkeypatch.setattr(kbx, "extract_source", scanned)
    d = tmp_path / "kb"
    src = tmp_path / "扫描规范.pdf"
    src.write_bytes(_scanned_pdf(12))
    assert kb.main(["ingest", str(src), "--kb", str(d)]) == 1
    assert not list((d / "inbox").rglob("source.md")) if (d / "inbox").exists() else True


def test_a_text_pdf_imports(tmp_path):
    if not kb.shutil.which("pdftotext") and not kb.shutil.which("mutool"):
        pytest.skip("本机没有 PDF 转换器")
    p = tmp_path / "spec.pdf"
    p.write_bytes(_text_pdf())
    assert "idx_" in kb.extract_pdf(p)


def test_extract_source_now_routes_pdf(tmp_path):
    """.pdf 不再撞「不支持的格式」。"""
    p = tmp_path / "spec.pdf"
    p.write_bytes(_scanned_pdf(3))
    with pytest.raises(kb.KbError) as exc:
        kb.extract_source(p)
    assert "不支持的格式" not in str(exc.value)


def test_ingest_says_so_when_it_overwrites_an_unprocessed_inbox(tmp_path):
    """同名重导会覆盖 inbox/<slug>/source.md —— 覆盖本身是对的默认(同名 = 同一份
    文档的新版本,你要的就是最新的),但**静默**覆盖不行:上次没条款化完的待办
    就这么没了,输出里一个字都不提。

    原文快照还在 sources/,所以不是数据丢失,是「待办丢失」—— 说一句就够。"""
    d = tmp_path / "kb"
    src = tmp_path / "规范.txt"

    def run():
        return subprocess.run(
            [sys.executable, str(_SCRIPT), "ingest", str(src), "--kb", str(d)],
            capture_output=True, text=True)

    src.write_text("第一章 总则\n1.1 索引名必须以 idx_ 开头。\n", encoding="utf-8")
    first = run()
    assert first.returncode == 0
    assert "覆盖" not in first.stdout, "首次导入没什么可覆盖的,不该吓唬人"

    src.write_text("第一章 总则\n1.1 索引名必须以 ix_ 开头。\n1.2 每张表必须有主键。\n",
                   encoding="utf-8")
    second = run()
    assert second.returncode == 0
    assert "覆盖" in second.stdout, "静默冲掉上次未处理的待办是不行的"
    assert "2 行" in second.stdout, "要报出被覆盖的是多少内容"
    assert "sources/" in second.stdout, "要指明原文快照还在,没真丢"


def test_ingest_local_machine_path_tells_user_to_upload(tmp_path, capsys, monkeypatch):
    """现场:用户给 D:\\AI\\…md 让导入。skill 在沙箱里读不到,要直说「传到沙箱的哪个目录」,不是一句「文件不存在」。"""
    monkeypatch.delenv("GSDB_KB_INBOX", raising=False)
    rc = kb.main(["ingest", r"D:\AI\报告.md", "--kb", str(tmp_path / "kb")])
    cap = capsys.readouterr()
    text = cap.out + cap.err
    assert rc != 0
    assert "沙箱" in text and "上传" in text and str(tmp_path / "kb" / "inbox" / "uploads") in text


def test_agents_md_does_not_send_the_model_to_paths_that_do_not_exist():
    """容器里没有 /workspace,也没有 search_docs.py。2026-10-24 测试环境:模型照 AGENTS.md 的旧检索流程猜出
    python3 /workspace/kb.py,被权限规则拦下,界面显示「shell 失败」。检索走知识库 skill,路径以 skill 给的为准。"""
    text = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "search_docs.py" not in text and "/workspace/docs" not in text
    assert "不要猜路径" in text and "kb.py query" in text and "kb.py show" in text
    assert "看文件内容用 read 工具" in text, "模型用 sed -n 看文件片段被拦(同一次测试),要指给它能用的办法"
