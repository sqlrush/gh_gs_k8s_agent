"""WDR 的「各维度元凶」与临时溢出归属 —— 2026-10-18 那轮测试抓到的归错语句。

当时的窗口(og5 快照 2981 → 2986):库级临时溢出 7.8G,报告把它归到按耗时排的榜首上;实际语句级
落盘最多的 2858996927 落了 4483.7MB(占语句级落盘 87%),只跑了 259 秒,不在按耗时排的前 10 名里 ——
元凶只在显示的前 10 名里找,所以永远找不到它。钉住:
  ① 元凶从宽的一批(CHAMPION_WIDTH)里找,显示仍是前 top 条,榜外的元凶另列、标明是哪一项冠军;
  ② 溢出那条发现写明语句级占库级多少、冠军是谁、在不在榜上,并带上它的 sql_id(下一步调优用);
  ③ 两边口径对不上(语句级反而更大)时直说这个比例不可信。
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-wdr" / "scripts"
for _m in ("model", "thresholds", "util", "collectors", "report",
           "native", "snaps", "interp", "recheck", "finalreport", "ansi", "followup"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors as C                                   # noqa: E402
import followup                                          # noqa: E402
import thresholds                                        # noqa: E402
from model import DIM_DBSTAT, DimResult, Evidence, Options, Severity  # noqa: E402
from common.finding import Finding                       # noqa: E402


class Runner:
    def __init__(self, rows):
        self.rows, self.params = rows, []

    def run(self, name, params=None):
        assert name == "wdr.top_sql"
        self.params.append(dict(params))
        return self.rows[: int(params["top"])]


def stmt(sid, elapsed_s, spill_mb=0.0, cpu_s=0.0, calls=1, phys=0, query="select 1"):
    return {"sid": sid, "query": query, "calls": str(calls), "elapsed_us": str(int(elapsed_s * 1e6)),
            "cpu_us": str(int(cpu_s * 1e6)), "spill_kb": str(int(spill_mb * 1024)), "phys_blocks": str(phys)}


# og5 故障窗口的形状:前 10 名是锁受害者,落盘冠军排在第 12
ROWS = ([stmt("3167133288", 5963.14, query="UPDATE fl.acct SET bal = bal + ? WHERE id = ?")]
        + [stmt("%d" % (1000 + i), 3000 - 200 * i) for i in range(9)]
        + [stmt("511666082", 300.0, spill_mb=682.8), stmt("2858996927", 259.17, spill_mb=4483.68,
                                                              query="SELECT count(*) AS n, max(x.rn) FROM …")])


def _opt(**kw):
    o = Options(begin=2981, end=2986, top=10, thresholds=thresholds.default_thresholds())
    o.db_time_us = 23_510_330_000
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_champions_are_searched_in_a_wide_set_and_listed_when_off_the_list():
    r, opt = Runner(ROWS), _opt()
    d = C.collect_topsql(r, opt)
    assert r.params[0]["top"] == C.CHAMPION_WIDTH, "元凶要从宽的一批里找"
    assert [row[0] for row in d.rows[:10]] == [x["sid"] for x in ROWS[:10]], "显示仍是前 top 条"
    off = [row for row in d.rows[10:]]
    assert [row[0] for row in off] == ["2858996927"]
    assert off[0][-1].startswith("〔榜外·溢出冠军〕")
    assert "溢出:2858996927(4484MB)" in d.headline
    assert opt.spill_champ == ("2858996927", int(4483.68 * 1024), False)


def test_a_champion_already_on_the_list_is_not_listed_twice():
    rows = [stmt("1", 100, spill_mb=10), stmt("2", 50)]
    d = C.collect_topsql(Runner(rows), _opt())
    assert [row[0] for row in d.rows] == ["1", "2"]


def _spill_ev(opt, temp_bytes):
    opt.temp_bytes = temp_bytes
    f = Finding(DIM_DBSTAT, "WDR_TEMP_SPILL", Severity.WARN, "临时文件溢出", "x", ">y",
                "snap_summary_stat_database.temp_bytes")
    ev = Evidence()
    ev.dims = [DimResult(dimension=DIM_DBSTAT, available=True, findings=[f])]
    ev.findings = [f]
    C._attribute_spill(ev, opt)
    return ev


def test_the_spill_finding_says_how_much_is_attributable_and_to_whom():
    opt = _opt()
    C.collect_topsql(Runner(ROWS), opt)
    ev = _spill_ev(opt, temp_bytes=8030 << 20)              # 库级 8030MiB
    f = ev.findings[0]
    assert f.sql_id == "2858996927", "下一步调优要能直接拿到这条的 sql_id"
    assert "占库级 64%" in f.evidence and "占语句级的 87%" in f.evidence
    assert "占库级的 56%" in f.evidence, "冠军自己占库级多少要直接写出来(测试里被读成了 64%)"
    assert "不在按耗时排的前 10 名里" in f.evidence
    assert "归不到具体语句" in f.evidence
    assert ev.dims[0].findings[0] == f, "维度里那份与汇总里那份要一致"


def test_statement_spill_larger_than_the_db_total_is_called_not_credible():
    opt = _opt()
    C.collect_topsql(Runner(ROWS), opt)
    f = _spill_ev(opt, temp_bytes=100 << 20).findings[0]
    assert "两个口径对不上" in f.evidence and "不可信" in f.evidence
    assert "占库级的" not in f.evidence, "口径对不上时不写冠军占库级多少"


def test_no_statement_spill_means_it_cannot_be_attributed():
    opt = _opt()
    C.collect_topsql(Runner([stmt("1", 100), stmt("2", 50)]), opt)
    f = _spill_ev(opt, temp_bytes=1 << 30).findings[0]
    assert f.sql_id == "" and "归不到某一条语句" in f.evidence


def test_followup_tunes_the_spill_champion_not_the_elapsed_leader():
    opt = _opt()
    d = C.collect_topsql(Runner(ROWS), opt)
    ev = _spill_ev(opt, temp_bytes=8030 << 20)
    steps = followup.plan(ev.findings, None, d.headers, d.rows)
    assert any("sqltune.py 2858996927" in s.cmd for s in steps)
    # 发现里没带 sql_id 时(老存档),从表里取 —— 表里有榜外的冠军行
    bare = [Finding(DIM_DBSTAT, "WDR_TEMP_SPILL", Severity.WARN, "m", "v", "t", "e")]
    assert any("sqltune.py 2858996927" in s.cmd for s in followup.plan(bare, None, d.headers, d.rows))
