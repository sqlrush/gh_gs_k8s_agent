"""白名单增量的顺序数据(common/grmp/deltas.py)与 docs/delivery/ 里的增量文件逐份一致(2026-11-19)。

部署自检据这份数据告诉现场「从哪一份起、按顺序导完哪几份」;数据和文件对不上,指路就是错的。
新出一份增量,这里会因为「文件比数据多一份」而失败,提醒补上。
"""
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import deltas  # noqa: E402

_DELIVERY = _ROOT.parent / "docs" / "delivery"


def _parse(text):
    added = re.findall(r"INSERT INTO grmp\.script_config \([^)]*\) VALUES\s*\(grmp\.script_config_seq\.nextval, "
                       r"'[A-Z]+', '([a-z_]+\.[a-z_0-9]+)'", text)
    changed, routing = [], []
    for body, name in re.findall(r"UPDATE grmp\.script_config\s+SET(.*?)WHERE script_name = '([a-z_]+\.[a-z_0-9]+)'",
                                 text, re.S):
        changed.append(name)
        if re.search(r"\bdatabase_type\s*=\s*'", body):
            routing.append(name)
    return tuple(sorted(set(added))), tuple(sorted(set(changed))), tuple(sorted(set(routing)))


def test_the_data_matches_every_delta_file_in_order():
    files = sorted(p.name for p in _DELIVERY.glob("scripts-delta-*.sql"))
    assert [d.file for d in deltas.DELTAS] == files, "新出了增量文件:在 common/grmp/deltas.py 里按顺序补一行"
    for d in deltas.DELTAS:
        assert (d.added, d.changed, d.routing) == _parse((_DELIVERY / d.file).read_text(encoding="utf-8")), d.file


def test_plan_starts_at_the_earliest_delta_that_explains_the_gap():
    newest = deltas.DELTAS[-1].file
    assert deltas.plan() == []
    files = deltas.plan(missing={"memanalyze.sql_mem_history"})            # 20261112 新增的
    assert files[0] == "scripts-delta-20261112.sql" and files[-1] == newest
    files = deltas.plan(missing={"memanalyze.sql_mem_history", "login.current_db"})   # 还缺 20261111 新增的:从更早的起
    assert files[0] == "scripts-delta-20261111.sql"
    assert deltas.plan(misrouted={"sqltune.indexes"})[0] == "scripts-delta-20261111.sql", "执行库是 20261111 改的"
    assert deltas.plan(missing={"health.overview"}) is None, "全量里就有、不是任何一份增量新增的:推断不出"


def test_advice_lists_files_in_order_or_asks_for_the_export():
    text = deltas.advice(missing={"memanalyze.sql_mem_history"})
    assert "scripts-delta-20261112.sql、scripts-delta-20261116.sql" in text and "一份都不能跳" in text
    assert "把白名单导出交给我方核对" in text
    assert "推断不出" in deltas.advice(missing={"health.overview"}) and deltas.advice() == ""
