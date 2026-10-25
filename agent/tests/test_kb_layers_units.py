"""渐进式披露(L0 / L1 / show / evidence)与类型化条目的命令,在样例知识库 + 六类条目上跑真的文件。

钉住的纪律:
  · L0 / L1 由条目生成、有上限,截断要写明「其余 N 条」;生成的旧专题文件会清掉,手写的 _taxonomy.yaml 不动;
  · 同一现象有多种根因(反例)要在 L1 并排、并提醒先按区分证据取证;
  · 知识库一节:状态行下是 L0 摘要(本实例画像、八类计数),每条发现先给专题与候选原因;
    不适用本环境的条目降到同类末尾、写明理由;别的系统的基线不出;
  · add 校验后才落盘、分配 ID;migrate 先出清单,--apply 才动文件,条款文件首行注释不丢;
  · retype 后旧 ID 仍能 show、cite-check 仍认。
"""
import importlib.util
import pathlib
import shutil
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# 部署仓:导入在 gaussdb-kb-import(含分层读取),查询 / 引用核对在只读的 gaussdb-kb
_SCRIPTS = _ROOT / "skills" / "gaussdb-kb-import" / "scripts"
_QUERY_SCRIPTS = _ROOT / "skills" / "gaussdb-kb" / "scripts"
_SAMPLE = _ROOT / "skills" / "gaussdb-kb-import" / "testdata" / "sample-kb"
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_SCRIPTS))

from common.kb import catalog as kbcat  # noqa: E402
from common.kb import layers as kblayers  # noqa: E402
from common.kb import query as kbquery  # noqa: E402
from common.kb import render as kbrender  # noqa: E402

TYPED = {
    "profiles/EP-CBST.md": """---
id: EP-CBST
type: profile
title: CBST 核心账务
system: CBST
aliases: [og5]
product: GaussDB 集中式
version: 505.2.0
deployment: 一主两备
---
## 摘要
CBST:GaussDB 集中式 505.2,一主两备。
## 批量窗口
22:00–02:00 日终批量。
""",
    "runbooks/RB-LCK-001.md": """---
id: RB-LCK-001
type: runbook
title: 终止阻塞根会话
topics: [lock]
source: sources/运维手册v3.md#4.2
applies: {product: GaussDB 集中式, versions: ">= 503"}
verification: 已验证
evidence: ["evidence/锁等待截图.png"]
---
## 摘要
锁等待超过 30 秒时只终止阻塞链的根会话,先与业务确认。
## 回退步骤
无需回退。
""",
    "product/PD-0001.md": """---
id: PD-0001
type: product
title: openGauss 锁等待超过 30 秒的默认行为
topics: [lock]
source: openGauss 文档
product: openGauss
versions: 5.*
---
## 摘要
openGauss 5.x 锁等待超过 30 秒默认不报错,等到 lockwait_timeout。
""",
    "product/PD-0002.md": """---
id: PD-0002
type: product
title: GaussDB 锁等待超过 30 秒的诊断视图
topics: [lock]
source: GaussDB 产品文档
product: GaussDB 集中式
versions: 505.*
---
## 摘要
GaussDB 505 锁等待超过 30 秒用 pg_locks 与 pg_thread_wait_status 定位根会话。
""",
    "baselines/BL-CBMS-ACTIVE_SESSIONS.md": """---
id: BL-CBMS-ACTIVE_SESSIONS
type: baseline
title: CBMS 活跃会话数
topics: [conn]
system: CBMS
metric: active_sessions
---
## 摘要
CBMS 锁等待超过 30 秒时活跃会话平时 30–60。
## 正常表现
平时 30–60。
""",
}


@pytest.fixture()
def kb(tmp_path):
    d = tmp_path / "kb"
    shutil.copytree(_SAMPLE, d)
    for sub in ("profiles", "runbooks", "defects", "baselines", "product", "wiki"):   # 样例自带的类型化条目换成本文件的
        shutil.rmtree(d / sub, ignore_errors=True)
    for rel, text in TYPED.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    (d / "sources").mkdir(exist_ok=True)
    (d / "sources" / "运维手册v3.md").write_text("# 第3章\n略\n## 4.2 终止根会话\n先确认,再终止根会话。\n", encoding="utf-8")
    (d / "evidence").mkdir(exist_ok=True)
    (d / "evidence" / "锁等待截图.png").write_bytes(b"\x89PNG")
    (d / "kb.yaml").write_text("embeddings: {source: none}\n", encoding="utf-8")
    return d


def _kbmain():
    if "kb" in sys.modules and getattr(sys.modules["kb"], "__file__", "").startswith(str(_SCRIPTS)):
        return sys.modules["kb"]
    spec = importlib.util.spec_from_file_location("kb", _SCRIPTS / "kb.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kb"] = mod
    spec.loader.exec_module(mod)
    return mod


def run(capsys, *argv):
    rc = _kbmain().main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out + cap.err


# ---------------------------------------------------------------- L0 / L1

def test_l0_counts_types_lists_environment_and_stays_small(kb):
    text = kblayers.render_l0(kb, kbcat.load(kb), "2026.09")
    assert "| 公司内部规范 | 12 |" in text and "| 已知缺陷与版本例外 | 0 —— 未登记 |" in text
    assert "| CBST | GaussDB 集中式 505.2.0 | 一主两备 | og5 | 22:00–02:00 日终批量。 | EP-CBST |" in text
    assert "`kb.py topic lock`" in text and len(text) <= kblayers.L0_LIMIT


def test_l1_puts_lookalikes_side_by_side_and_points_down(kb):
    cat = kbcat.load(kb)
    from common.kb import graphfiles as gf
    triples, _ = gf.load_triples(kb)
    view = kblayers.topic_view(cat, triples, "lock")
    text = kblayers.render_l1(view, "2026.09")
    assert "锁等待超过 30 秒" in view.lookalikes, "样例库里的干扰案例:同一现象两种根因"
    assert "先按区分证据取证" in text and "| 锁等待超过 30 秒 |" in text
    assert "〔手册 RB-LCK-001〕" in text and "kb.py show <ID>" in text
    assert len(text) <= kblayers.L1_LIMIT


def test_l1_truncation_is_visible(kb):
    for i in range(40):
        (kb / "wiki").mkdir(exist_ok=True)
        (kb / "wiki" / f"WK-{i + 1:04d}.md").write_text(
            f"---\nid: WK-{i + 1:04d}\ntype: wiki\ntitle: 锁经验{i}\ntopics: [lock]\nsource: w\n---\n## 摘要\n{'锁等待经验' * 30}\n",
            encoding="utf-8")
    cat = kbcat.load(kb)
    text = kblayers.render_l1(kblayers.topic_view(cat, [], "lock"), "v")
    assert len(text) <= kblayers.L1_LIMIT and "kb.py topic lock --all" in text
    full = kblayers.render_l1(kblayers.topic_view(cat, [], "lock"), "v", show_all=True)
    assert full.count("〔Wiki ") == 40


def test_write_layers_removes_only_generated_stale_files(kb):
    (kb / "topics").mkdir(exist_ok=True)
    (kb / "topics" / "repl.md").write_text(f"# x\n\n{kblayers.GENERATED_MARK}。\n", encoding="utf-8")
    (kb / "topics" / "notes.md").write_text("# 手写笔记\n", encoding="utf-8")
    written, _ = kblayers.write_layers(kb, "v")
    assert (kb / kblayers.L0_FILE).is_file() and "topics/lock.md" in written
    assert not (kb / "topics" / "repl.md").exists(), "已经没有条目的专题:生成的旧文件要清掉"
    assert (kb / "topics" / "notes.md").exists(), "不是生成的文件不动"


# ---------------------------------------------------------------- 读取命令

def test_overview_topic_show_evidence(kb, capsys):
    rc, out = run(capsys, "overview", "--kb", str(kb), "--alias", "og5")
    assert rc == 0 and "知识库总索引(L0)" in out and "本实例(og5):EP-CBST" in out
    rc, out = run(capsys, "topic", "锁等待", "--kb", str(kb))          # 同义词也能进专题
    assert rc == 0 and "锁与阻塞(L1 专题导航" in out
    rc, out = run(capsys, "show", "PD-0001", "--kb", str(kb), "--alias", "og5")
    assert rc == 0 and "适用性:不适用(条目是 openGauss,本环境是 GaussDB 集中式)" in out
    rc, out = run(capsys, "show", "GS-XACT-001", "--kb", str(kb), "--brief")
    assert rc == 0 and "〔规范 GS-XACT-001〕" in out and "rule:" in out
    rc, out = run(capsys, "evidence", "RB-LCK-001", "--kb", str(kb))
    assert rc == 0 and "图片:" in out and "先确认,再终止根会话" in out


def test_show_unknown_id_says_so_and_suggests(kb, capsys):
    rc, out = run(capsys, "show", "RB-LCK", "--kb", str(kb))
    assert rc == 1 and "没有 `RB-LCK`" in out and "RB-LCK-001" in out and "不得编造" in out


def test_template_lists_required_fields_and_allowed_values(capsys):
    """模型为了知道字段能填什么去翻源码(测试环境里用了不放行的 sed),template 要把要求说全。"""
    rc, out = run(capsys, "template", "baseline")
    assert rc == 0 and "必填:" in out and "metric" in out and "取值:value_kind 只能是 累计值 / 差值 / 瞬时值" in out
    rc, out = run(capsys, "template", "defect")
    assert "取值:vendor_status 只能是 已确认 / 待确认" in out and "affected_versions" in out.split("建议")[0]


def test_template_is_addable_after_filling(kb, capsys, tmp_path):
    rc, out = run(capsys, "template", "wiki")
    tpl = out[out.index("---"):]
    draft = tmp_path / "d.md"
    draft.write_text(tpl.replace("<专题 slug,如 lock>", "conn").replace("<出处:sources/原文.docx#章节>", "wiki/x")
                     .replace("<≤200 字,结论在前;L1 专题导航里只显示这一段>", "连接池泄漏看 backend_start。")
                     .replace("<YYYY-MM>", "2024-06"), encoding="utf-8")
    rc, out = run(capsys, "add", str(draft), "--kb", str(kb))
    assert rc == 0 and "wiki/WK-0001.md" in out


# ---------------------------------------------------------------- 写命令

def test_add_rejects_invalid_and_assigns_ids(kb, capsys, tmp_path):
    good = tmp_path / "g.md"
    good.write_text("---\nid: auto\ntype: runbook\ntitle: 杀会话前确认\ntopics: [lock]\nsource: s\n---\n## 摘要\n先确认。\n",
                    encoding="utf-8")
    bad = tmp_path / "b.md"
    bad.write_text("---\nid: auto\ntype: profile\ntitle: x\nsystem: CBMS\n---\n## 摘要\n主库 10.1.2.3\n", encoding="utf-8")
    rc, out = run(capsys, "add", str(good), str(bad), "--kb", str(kb), "--move")
    assert rc == 2 and "runbooks/RB-LCK-002.md" in out and "[拒收]" in out and "只能写连接别名" in out
    assert not good.exists() and bad.exists(), "--move 只删入库成功的草稿"
    assert not (kb / "profiles" / "EP-CBMS.md").exists()


def test_retype_keeps_the_old_id_reachable(kb, capsys):
    rc, out = run(capsys, "retype", "PD-0002", "--to", "wiki", "--kb", str(kb))
    assert rc == 0 and "wiki/WK-0001.md" in out
    rc, out = run(capsys, "show", "PD-0002", "--kb", str(kb))
    assert rc == 0 and "已改为 `WK-0001`" in out
    spec = importlib.util.spec_from_file_location("kb_cite_query", _QUERY_SCRIPTS / "kb_cite.py")
    kb_cite = importlib.util.module_from_spec(spec)
    sys.modules["kb_cite_query"] = kb_cite              # dataclass 要在 sys.modules 里找到自己的模块
    spec.loader.exec_module(kb_cite)
    assert kb_cite.typed_ids(kb)["PD-0002"] == "WK-0001", "cite-check(查询 skill)按 aliases 认旧 ID"


def test_migrate_plan_then_apply(kb, capsys):
    (kb / "guides").mkdir(exist_ok=True)
    (kb / "guides" / "lock.md").write_text("---\nid: G-LOCK\ndescription: 锁等待处置步骤\n---\n执行前确认;步骤一;回退:无。\n",
                                           encoding="utf-8")
    rule_file = next((kb / "rules").glob("*.yaml"))
    head = rule_file.read_text(encoding="utf-8").splitlines()[0]
    rc, out = run(capsys, "migrate", "--kb", str(kb))
    assert rc == 2 and "guides/lock.md → runbook" in out and not list((kb / "runbooks").glob("RB-LCK-002.md"))
    rc, out = run(capsys, "migrate", "--kb", str(kb), "--apply")
    assert rc == 0, out
    moved = list((kb / "runbooks").glob("RB-*.md"))
    assert len(moved) == 2 and not (kb / "guides" / "lock.md").exists()
    assert rule_file.read_text(encoding="utf-8").splitlines()[0] == head, "条款文件首行注释(INDEX 的说明)不能丢"
    cat = kbcat.load(kb)
    assert cat.find("G-LOCK") is not None, "旧指南 ID 进 aliases,报告里引用过的还能找到"
    assert sum(1 for e in cat.entries if e.type == "norm" and e.topics_inferred) < 12
    rc, out = run(capsys, "migrate", "--kb", str(kb), "--apply")
    assert rc == 0, "可重复执行:已迁过的跳过"


def test_profile_draft_parses_the_login_version_string(kb, capsys):
    rc, out = run(capsys, "profile", "--system", "CBMS", "--alias", "og7", "--kb", str(kb),
                  "--version-string", "PostgreSQL 9.2.4 (GaussDB Kernel 505.2.0 build 1a2b) compiled at 2025")
    text = (kb / "inbox" / "drafts" / "EP-CBMS.md").read_text(encoding="utf-8")
    assert rc == 0 and "version: 505.2.0" in text and "product: GaussDB 集中式" in text and "og7" in text


def test_index_and_validate_cover_typed_items(kb, capsys):
    rc, out = run(capsys, "index", "--kb", str(kb))
    assert rc in (0, 2) and "分层已重建" in out and (kb / "topics" / "lock.md").is_file()
    assert "`runbooks/RB-LCK-001.md`" in (kb / "INDEX.md").read_text(encoding="utf-8")
    rc, out = run(capsys, "validate", "--kb", str(kb))
    assert rc == 0, out
    assert "没写「区分证据」" not in out, "样例库三组反例都写了区分证据"
    assert "approval" in out, "条款没写审批 / 生效版本要汇总提示"
    case = kb / "cases" / "S2-20250915-CBMS-exchange分区锁等待.md"
    case.write_text(case.read_text(encoding="utf-8").split("## 区分证据")[0], encoding="utf-8")
    rc, out = run(capsys, "validate", "--kb", str(kb))
    assert "S2-20250915-CBMS-exchange分区锁等待 没写「区分证据」" in out


# ---------------------------------------------------------------- 检索输出

def test_query_section_has_l0_hints_and_applicability(kb):
    res = kbquery.from_text("锁等待超过 30 秒怎么处置", kb_dir=kb, alias="og5")
    out = kbrender.render_section(res)
    assert "> 本实例(og5):EP-CBST" in out and "> 已登记:规范 12 · 案例 27 · 画像 1" in out
    assert "- 专题:锁与阻塞(`kb.py topic lock`)" in out
    assert "**处置手册** RB-LCK-001《终止阻塞根会话》(已验证)" in out
    prods = [ln for ln in out.splitlines() if ln.startswith("- **产品文档**")]
    assert prods and "PD-0002" in prods[0], "适用本环境的产品文档在前"
    assert all("不适用本环境" in ln for ln in prods if "PD-0001" in ln)
    assert "BL-CBMS" not in out, "别的系统的基线与本实例无关"


def test_without_alias_applicability_is_unknown(kb):
    out = kbrender.render_section(kbquery.from_text("锁等待超过 30 秒 openGauss", kb_dir=kb))
    assert "本实例(" not in out
    assert any("未知本环境" in ln for ln in out.splitlines() if ln.startswith("- **产品文档**"))


# ---------------------------------------------------------------- 只读的查询 skill(诊断环境)

def _query_main():
    import subprocess
    def call(*argv):
        r = subprocess.run([sys.executable, str(_QUERY_SCRIPTS / "kb.py"), *argv], capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr
    return call


def test_query_skill_has_the_layered_reads_but_not_the_writes(kb):
    call = _query_main()
    rc, out = call("overview", "--kb", str(kb), "--alias", "og5")
    assert rc == 0 and "本实例(og5):EP-CBST" in out
    rc, out = call("show", "RB-LCK-001", "--kb", str(kb), "--brief")
    assert rc == 0 and "〔手册 RB-LCK-001〕" in out
    rc, out = call("topic", "lock", "--kb", str(kb))
    assert rc == 0 and "候选原因" in out
    for cmd in ("add", "migrate", "retype", "profile", "attach", "taxonomy"):
        rc, out = call(cmd, "x", "--kb", str(kb))
        assert rc == 2 and "本环境不含知识库导入功能" in out, cmd
    rc, out = call("query", "--q", "锁等待超过 30 秒", "--alias", "og5", "--kb", str(kb))
    assert rc == 0 and "> 本实例(og5):EP-CBST" in out


def test_inbox_clean_removes_only_finished_batches(kb, capsys, tmp_path):
    """导入环境的命令行不放行 rm(2026-10-24 测试环境:模型删不掉处理完的中间目录),删批次目录走脚本。"""
    src = tmp_path / "手册.md"
    src.write_text("# 手册\n步骤一\n", encoding="utf-8")
    rc, _ = run(capsys, "ingest", str(src), "--kb", str(kb))
    assert rc == 0 and (kb / "inbox" / "手册" / "source.md").is_file()
    (kb / "inbox" / "uploads").mkdir(parents=True, exist_ok=True)
    (kb / "inbox" / "t1" / "items").mkdir(parents=True)
    (kb / "inbox" / "t1" / "manifest.json").write_text("{}", encoding="utf-8")
    (kb / "inbox" / "t1" / "items" / "a.md").write_text("x", encoding="utf-8")
    rc, out = run(capsys, "inbox-clean", "手册", "uploads", "t1", "../rules", "--kb", str(kb))
    assert rc == 2 and "[已清理] inbox/手册" in out and not (kb / "inbox" / "手册").exists()
    assert (kb / "inbox" / "uploads").is_dir() and (kb / "inbox" / "t1" / "items" / "a.md").is_file()
    assert "还有 1 单工单没处理" in out and (kb / "rules").is_dir()
    rc, out = run(capsys, "inbox-clean", "t1", "--discard", "--kb", str(kb))
    assert rc == 0 and not (kb / "inbox" / "t1").exists(), "导错的批次可以整批丢弃"


def test_current_alias_maps_the_api_connection_back_to_the_login_alias(monkeypatch):
    """api(中间件)模式的会话连接名是 10-0-0-9-postgres,画像认的是登录别名 og5(2026-10-24 测试环境没匹配上)。"""
    from types import SimpleNamespace
    from common import config as gconfig
    from common import session as gsession
    monkeypatch.setattr(gsession, "current", lambda: SimpleNamespace(name="10-0-0-9-postgres", data_ip="10.0.0.9", database="postgres"))
    monkeypatch.setattr(gconfig, "instance_aliases", lambda: [gconfig.InstanceAlias("og5", "10.0.0.9", "postgres")])
    assert kbquery.current_alias() == "og5"
    monkeypatch.setattr(gsession, "current", lambda: SimpleNamespace(name="og", data_ip="", database="x"))
    assert kbquery.current_alias() == "og", "gsql 模式:连接名就是配置里的名字"
    monkeypatch.setattr(gsession, "current", lambda: None)
    assert kbquery.current_alias() == ""


def test_kb_health_gives_the_dashboard_link_when_archived(kb, monkeypatch, tmp_path):
    """查询 skill 的 health 存档后给大盘知识库页的直达链接(子进程继承 GSDB_REPORTS_DIR)。"""
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path / "reports"))
    rc, out = _query_main()("health", "--kb", str(kb))
    assert (tmp_path / "reports" / "kb" / "health.json").is_file()
    assert "[打开知识库大盘](/dash/kb)" in out
