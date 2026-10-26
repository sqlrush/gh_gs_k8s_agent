"""回答格式检查(tools/answer_check.py):合格的样例全过、典型的走样每一种都能抓到。"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from tools import answer_check as ac  # noqa: E402

GOOD_FAULT = """评估时段:要的是 10-24 18:20–18:40,实际评估快照 3197 → 3205。

总体状态 🟠告警  驱动:行锁等待
**短结论**:交易超时是在排队等锁。

## ① 现象
应用视角:转账交易在排队。数据库机制:transactionid 锁等待。

## ② 根因
应用视角:同一批账户被并发更新。数据库机制:根会话持有行锁未提交。

## ③ 解决方案
- P0 <font color=#2f6fdb>应急时先取消 DDL 会话</font>(引用自处置手册库 RB-LCK-001)
- P1 <font color=#0f7c86>lockwait_timeout 默认 20 分钟</font>(引用自 GaussDB 官方文档库 PD-G25132C-01481)

## ④ 总结
需要就阻塞链的根会话展开说明吗?
"""

GOOD_QA = """解答来源:GaussDB 官方文档库 PD-G25132C-01481;贵行规范库 GS-CHG-003

**① 解答正文**
<font color=#0f7c86>默认 20 分钟</font>[1];<font color=#2f6fdb>贵行要求不超过 30 秒</font>[2]。

**② 举例说明**
通用场景:白天执行 DDL。

**③ 引申和拓展**
需要了解 deadlock_timeout 和它的区别吗?
"""


def _bad(checks):
    return {c.name for c in checks if not c.ok}


def test_good_answers_pass():
    assert _bad(ac.check(GOOD_FAULT, "fault")) == set()
    assert _bad(ac.check(GOOD_QA, "qa")) == set()


def test_missing_or_misordered_sections_are_caught():
    no_solution = GOOD_FAULT.replace("## ③ 解决方案", "## ③ 处理")
    assert "段落" in _bad(ac.check(no_solution, "fault"))
    reordered = GOOD_FAULT.replace("## ① 现象", "## ① 总结在前").replace("## ④ 总结", "## ④ 现象")
    assert "段落" in _bad(ac.check(reordered, "fault"))


def test_opening_followup_and_two_views():
    assert "开头" in _bad(ac.check(GOOD_FAULT.replace("总体状态", "状态"), "fault"))
    assert "展开提议" in _bad(ac.check(GOOD_FAULT.replace("需要就阻塞链的根会话展开说明吗?", "完。"), "fault"))
    assert "两种读者" in _bad(ac.check(GOOD_FAULT.replace("数据库机制", "机制"), "fault"))
    assert "开头" in _bad(ac.check(GOOD_QA.replace("解答来源", "来源", 1), "qa"))


def test_citation_mistakes_are_caught():
    no_paren = GOOD_FAULT.replace("(引用自处置手册库 RB-LCK-001)", "")
    assert "出处括号" in _bad(ac.check(no_paren, "fault"))
    wrong_lib = GOOD_FAULT.replace("处置手册库 RB-LCK-001", "贵行规范库 RB-LCK-001")
    assert "库名与颜色" in _bad(ac.check(wrong_lib, "fault"))
    wrong_color = GOOD_FAULT.replace("<font color=#0f7c86>lockwait", "<font color=#2f6fdb>lockwait")
    assert "库名与颜色" in _bad(ac.check(wrong_color, "fault"))
    declared = GOOD_FAULT.replace("## ③ 解决方案", "## ③ 解决方案\n根据知识库检索,")
    assert "不特意声明来源" in _bad(ac.check(declared, "fault"))
    old = GOOD_FAULT + "\n〔手册 RB-LCK-001〕 本行无先例,以下为通用做法"
    assert {"引用写法", "兜底说法"} <= _bad(ac.check(old, "fault"))
    # 知识问答:出处在第一行,句后不带括号也合格
    assert "出处括号" not in _bad(ac.check(GOOD_QA, "qa"))


def test_cli_exit_code(tmp_path, capsys):
    f = tmp_path / "a.md"
    f.write_text(GOOD_FAULT, encoding="utf-8")
    assert ac.main(["--kind", "fault", "--file", str(f)]) == 0
    f.write_text(GOOD_FAULT.replace("## ③ 解决方案", ""), encoding="utf-8")
    assert ac.main(["--kind", "fault", "--file", str(f)]) == 1
    assert "✗ 段落" in capsys.readouterr().out


def test_source_inside_the_tag_and_wdr_followups_in_section_four():
    """括号写进了标签里(括号也上了色)也算注明了出处;WDR 的展开提议在第 ④ 段,后面还有很长的附录。"""
    inside = GOOD_FAULT.replace("应急时先取消 DDL 会话</font>(引用自处置手册库 RB-LCK-001)",
                                "应急时先取消 DDL 会话(引用自处置手册库 RB-LCK-001)</font>")
    assert "出处括号" not in _bad(ac.check(inside, "fault"))
    wdr = ("评估窗口:snap 1 → 2\n总体状态 🟠告警\n## ① 负载画像\n应用视角:a 数据库机制:b\n## ② 问题与元凶\n## ③ 解决方案\n"
           "## ④ 总结和结论\n总结。\n- 需要就 111 做一次调优吗?\n## 附录\n" + "附录正文\n" * 400)
    assert "展开提议" not in _bad(ac.check(wdr, "wdr"))
