"""增量文件 → 每份导完后各脚本的正文 md5 与参数(2026-11-19),给发版台推断白名单导到了哪一份。"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import whitelist as wl  # noqa: E402

_DELIVERY = _ROOT / "docs" / "delivery"


def test_each_delta_file_is_read_in_order_and_the_newest_bodies_match_the_full_set():
    deltas = wl.deltas_from_dir(_DELIVERY)
    assert [d["file"] for d in deltas] == sorted(p.name for p in _DELIVERY.glob("scripts-delta-*.sql"))
    full = wl.parse_dml((_DELIVERY / "scripts.sql").read_text(encoding="utf-8"))
    last = deltas[-1]
    assert sorted(last["changed"]) == ["explain.kernel_funcs", "health.partitions", "health.stale_stats", "health.table_writes",
                                       "health.unused_index", "lockwait.pairs", "login.current_db", "procinfo.locate_search",
                                       "procinfo.proc_def", "proctune.locate_search", "proctune.proc_def", "sqlreview.indexes",
                                       "sqlreview.tables"]           # v1.5.1 增量 20261123:M 兼容业务库
    for name, md5 in last["changed"].items():
        assert md5 == full[name].md5, "最新一份增量导完后的正文就是全量里的正文:%s" % name
    d1111 = next(d for d in deltas if d["file"] == "scripts-delta-20261111.sql")
    assert list(d1111["added"]) == ["login.current_db"] and len(d1111["changed"]) == 71
    assert d1111["params"]["sqltune.indexes"][0] == ["dbName", "String"], "改执行库的那份带参数定义"


def _d(file, added=None, changed=None, params=None):
    return {"file": file, "added": added or {}, "changed": changed or {}, "params": params or {}}


DELTAS = [_d("a.sql", added={"x.new": "m1"}), _d("b.sql", changed={"y.old": "m2"}),
          _d("c.sql", changed={"y.old": "m3"}, params={"y.old": [["dbName", "String"]]})]


def _reg(name, md5, params=()):
    return wl.Registered(name, md5, tuple(tuple(p) for p in params), True)


def test_suggest_rules():
    F = wl.Finding
    assert wl.suggest(DELTAS, [F("x.new", wl.MISSING)], [])[0] == ["a.sql", "b.sql", "c.sql"]
    assert wl.suggest(DELTAS, [F("y.old", wl.CONTENT)], [_reg("y.old", "m2")])[0] == ["c.sql"]
    files, why = wl.suggest(DELTAS, [F("y.old", wl.CONTENT)], [_reg("y.old", "zz")])
    assert files is None and "y.old" in why
    assert wl.suggest(DELTAS, [F("z.base", wl.MISSING)], [])[0] is None, "不是任何一份增量新增的"
    assert wl.suggest(DELTAS, [F("y.old", wl.DUPLICATE)], [])[0] == [], "重名 / 未生效不是导增量能解决的"
    assert wl.suggest([], [F("x.new", wl.MISSING)], [])[0] is None, "旧包里没有增量数据:推断不出"
