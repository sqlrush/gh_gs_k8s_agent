"""WDR 报告的回答模板版式(2026-10-26,R7):负载画像 → 问题与元凶 → 解决方案 → 总结和结论 → 附录。

版式由脚本固定(skills/gaussdb-wdr/scripts/mdreport.py),模型只写 interp.json 的字段。钉住:
① 段落顺序,等待表在负载画像里、Top SQL 多维表与各维度元凶行在问题与元凶里、其余全景表进附录;
② 新字段(短结论、两种视角、优先级、总结、展开提议)放在该放的位置;旧格式的 interp 照样能渲染;
③ 知识引用由脚本上色、加出处括号,编号核对不过的不进正文;没有知识库目录时一条都不展示;
④ 上色与库名的规则(common/kb/citestyle.py)与回答模板的「知识引用写法」一致。
"""
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# 各技能的脚本同名(model / render / util …),先清掉别的技能留下的同名模块(与 test_wdr_units 同一做法)
for _m in ("model", "thresholds", "util", "collectors", "report", "native", "snaps", "interp", "recheck",
           "finalreport", "render", "mdreport", "appendix", "kbrefs", "ansi"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-wdr" / "scripts"))

import finalreport  # noqa: E402
import model  # noqa: E402
from common.kb import citestyle  # noqa: E402
from interp import Interp, InterpFinding, InterpOverall, KbRef, Suggestion, Validation  # noqa: E402
from model import DimResult, Evidence, Finding, Severity, Window  # noqa: E402

CHAMP = "各维度元凶 — DB time:111(60%,CPU占5%) ｜ 溢出:222(86%)"


def _ev():
    wait_f = Finding(model.DIM_WAITS, "WDR_WAIT_CLASS_SKEW", Severity.WARN, "等待类倾斜", "LOCK 90%", ">60%", "e")
    dl_f = Finding(model.DIM_DBSTAT, "WDR_DEADLOCK", Severity.CRITICAL, "死锁数", "26", "≥5", "e")
    dims = [
        DimResult(dimension=model.DIM_WAITS, available=True, headline="LOCK_EVENT 占 90%",
                  headers=["等待类", "占比%"], rows=[["LOCK_EVENT", "90"], ["IO_EVENT", "8"]], findings=[wait_f]),
        DimResult(dimension=model.DIM_DBSTAT, available=True, headline="死锁 26",
                  headers=["死锁"], rows=[["26"]], findings=[dl_f]),
        DimResult(dimension=model.DIM_TOPSQL, available=True, headline=CHAMP,
                  headers=["sql_id", "占DB time%"], rows=[["111", "60"], ["222", "20"]], findings=[]),
        DimResult(dimension=model.DIM_CHECKPOINT, available=True, headline="checkpoint 正常",
                  headers=["次数"], rows=[["3"]], findings=[]),
    ]
    return Evidence(conn="og5", window=Window(begin_id=3073, end_id=3079, scope="node", node="og5"),
                    dims=dims, findings=[wait_f, dl_f], overall=Severity.CRITICAL)


def _kb(tmp_path):
    for sub in ("cases", "rules", "archive"):
        (tmp_path / sub).mkdir()
    (tmp_path / "rules" / "chg.yaml").write_text(
        "- id: GS-CHG-003\n  severity: warn\n  check: advisory\n  rule: DDL 变更时会话级锁等待上限不超过 30 秒\n", encoding="utf-8")
    return tmp_path


def _interp(**extra):
    base = dict(overall=InterpOverall("CRITICAL", "锁等待约 90%"), findings=[
        InterpFinding(code="WDR_WAIT_CLASS_SKEW", root_cause="行锁冲突", priority="P0",
                      app_view="转账类交易在排队等锁", mechanism="transactionid 锁等待占 90%",
                      kb_refs=[KbRef("GS-CHG-003", "DDL 变更时会话级锁等待上限不超过 30 秒"),
                               KbRef("GS-CHG-999", "编出来的一条")],
                      suggestions=[Suggestion("按主键顺序加锁", "低", True)]),
        InterpFinding(code="WDR_DEADLOCK", root_cause="加锁顺序不一致", sql_id="111",
                      suggestions=[Suggestion("捕获死锁后重试", "中", True,
                                              Validation("none", "n/a", ""))]),
    ])
    base.update(extra)
    return Interp(**base)


def test_sections_come_in_template_order(tmp_path):
    out = finalreport.render_report(_ev(), _interp(summary="库不缺 CPU,时间都花在等锁上。"), kb_dir=_kb(tmp_path))
    heads = ["评估窗口：snap 3073", "总体状态 ", "判断校验 ", "**短结论**：库不缺 CPU", "## ① 负载画像",
             "### Wait Events / Classes", "## ② 问题与元凶", "### Top SQL", "## ③ 解决方案",
             "## ④ 总结和结论", "## 附录", "#### Checkpoint"]
    pos = [out.index(h) for h in heads]
    assert pos == sorted(pos), out
    culprit = out[out.index("## ② 问题与元凶"):out.index("## ③ 解决方案")]
    assert CHAMP in culprit, "Top SQL 多维表的「各维度元凶」行要原样留在问题与元凶里"


def test_new_fields_land_where_the_template_says(tmp_path):
    in_ = _interp(summary="s", load_app_view="业务感受到交易慢", load_mechanism="DB time 九成是等锁",
                  conclusion="先解决锁,再调报表。", followups=["需要就 111 做一次调优吗?"])
    out = finalreport.render_report(_ev(), in_, kb_dir=_kb(tmp_path))
    load = out[out.index("## ① 负载画像"):out.index("## ② 问题与元凶")]
    assert "应用视角：业务感受到交易慢" in load and "数据库机制：DB time 九成是等锁" in load
    culprit = out[out.index("## ② 问题与元凶"):out.index("## ③ 解决方案")]
    assert "- 应用视角：转账类交易在排队等锁" in culprit and "- 数据库机制：transactionid" in culprit
    assert "- 引发它的语句：sql_id `111`" in culprit
    sol = out[out.index("## ③ 解决方案"):out.index("## ④ 总结和结论")]
    # 告警级的发现被 interp 排成 P0;严重级的没给优先级 → 按严重度落 P0
    assert sol.index("### P0(最优先)") < sol.index("WDR_WAIT_CLASS_SKEW：按主键顺序加锁")
    assert "WDR_DEADLOCK：捕获死锁后重试" in sol and "### P1" not in sol
    end = out[out.index("## ④ 总结和结论"):out.index("## 附录")]
    assert "先解决锁,再调报表。" in end and "- 需要就 111 做一次调优吗?" in end
    assert end.index("本报告经脚本只读采集") < end.index("需要就 111")


def test_old_interp_still_renders(tmp_path):
    old = Interp(overall=InterpOverall("CRITICAL", "x"),
                 findings=[InterpFinding(code="WDR_DEADLOCK", root_cause="加锁顺序不一致")])
    out = finalreport.render_report(_ev(), old, kb_dir=_kb(tmp_path))
    assert "**短结论**" not in out and "本次未给出总结。" in out
    assert "本次没有经验证的优化建议" in out
    assert "应用视角" not in out.split("## 附录")[0]


def test_kb_refs_are_coloured_by_script_and_fabricated_ones_dropped(tmp_path):
    out = finalreport.render_report(_ev(), _interp(), kb_dir=_kb(tmp_path))
    good = "<font color=#2f6fdb>DDL 变更时会话级锁等待上限不超过 30 秒</font>(引用自贵行规范库 GS-CHG-003)"
    assert good in out.split("## ③ 解决方案")[0]
    body, appendix = out.split("## 附录")
    assert "GS-CHG-999" not in body and "编出来的一条" not in out
    assert "知识引用未展示" in appendix and "GS-CHG-999:未找到" in appendix


def test_without_a_kb_directory_no_reference_is_shown(tmp_path):
    out = finalreport.render_report(_ev(), _interp(), kb_dir=tmp_path / "no-such-kb")
    assert "<font" not in out
    assert "知识库目录不存在" in out.split("## 附录")[1]


def test_ansi_follows_the_same_order_and_drops_tags(tmp_path):
    out = finalreport.render_report(_ev(), _interp(summary="s"), fmt="ansi", no_color=True, kb_dir=_kb(tmp_path))
    pos = [out.index(h) for h in ("① 负载画像", "② 问题与元凶", "③ 解决方案", "④ 总结和结论", "附录")]
    assert pos == sorted(pos)
    assert "<font" not in out and "(引用自贵行规范库 GS-CHG-003)" in out


def test_interp_json_new_keys_parse_and_bad_values_are_ignored():
    raw = {"overall": {"severity": "WARN"}, "summary": " 短 ", "loadView": {"appView": "a", "mechanism": "m"},
           "conclusion": "c", "followups": ["1", "", "2", "3", "4"],
           "findings": [{"code": "X", "priority": "p9", "kbRefs": [{"id": "RB-LCK-001", "text": "t"}, {"id": ""}, "junk"],
                         "appView": 3}]}
    in_ = Interp.from_dict(json.loads(json.dumps(raw)))
    assert (in_.summary, in_.load_app_view, in_.load_mechanism, in_.conclusion) == ("短", "a", "m", "c")
    assert in_.followups == ["1", "2", "3"]
    f = in_.findings[0]
    assert f.priority == "" and f.app_view == "" and [r.id for r in f.kb_refs] == ["RB-LCK-001"]


def test_citestyle_matches_the_template_rules():
    assert citestyle.library_for("PD-G25132C-01481") == ("GaussDB 官方文档库", citestyle.OFFICIAL_COLOR)
    assert citestyle.library_for("PD-0001") == ("产品文档库", citestyle.OFFICIAL_COLOR)     # 客户导入的产品文档也是官方说法
    assert citestyle.library_for("S3-20260416-FPAYS-PAYS支付流水重复提交")[0] == "本行案例库"
    assert citestyle.library_for("lockwait_timeout") is None
    assert (citestyle.format_ref("WK-0002", "调到 1GB", "未验证")
            == "<font color=#2f6fdb>调到 1GB</font>(引用自 Wiki 经验库 WK-0002,未验证)")
    assert citestyle.format_ref("RB-LCK-001", "第一行\n第二行") == \
        "<font color=#2f6fdb>第一行 第二行</font>(引用自处置手册库 RB-LCK-001)"      # 一个标签不跨段落
    cite = (_ROOT / "common" / "answer_templates" / "cite.md").read_text(encoding="utf-8")
    for lib in citestyle.LIBRARY_NAMES:
        assert lib in cite, "cite.md 的库名表里没有 %s" % lib
    assert citestyle.CUSTOMER_COLOR in cite and citestyle.OFFICIAL_COLOR in cite


def test_kb_refs_resolve_in_a_fresh_process(tmp_path):
    """在干净进程里核对引用编号,与 wdr.py render 时一样(2026-10-26 测试环境:渲染崩在这里)。

    同一个测试进程里别的用例可能已经把 sys.modules['kb'] 换成了另一个 kb.py,进程内的用例因此测不出来:
    容器线查询侧的 kb.py 没有 load_rule_file,cite-check 要从 common/kb/rulesfile 取。
    """
    import subprocess
    kb = tmp_path / "kb"
    kb.mkdir()
    _kb(kb)
    scripts = _ROOT / "skills" / "gaussdb-wdr" / "scripts"
    code = ("import sys, pathlib; sys.path[:0] = [%r, %r]\n"
            "import kbrefs, interp\n"
            "print(kbrefs.resolve([interp.KbRef('GS-CHG-003', 'x'), interp.KbRef('GS-CHG-999', 'y')], pathlib.Path(%r)))"
            % (str(scripts), str(_ROOT), str(kb)))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(scripts))
    assert out.returncode == 0, out.stderr
    assert "(引用自贵行规范库 GS-CHG-003)" in out.stdout and "GS-CHG-999:未找到" in out.stdout
