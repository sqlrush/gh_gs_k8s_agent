"""前端 ↔ 报告的跨语言契约守卫。

docs/api/reports-contract.json 列出前端读到的报告字段路径;这里用 scripts/dash-samples.py **同一批构造函数**
(真实的 HealthEvidence / wdr Evidence / StmtRow / kb.health_dict,经 reports.archive 落盘)生成样例,
逐条核对路径存在。页面引用了报告里不存在的字段 → 这里红,不会等到上线才是一片空白。
没有 JS 工具链也能跑:这是两种语言之间唯一的钉子。
"""
import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REPO = _ROOT.parent
sys.path.insert(0, str(_ROOT))

CONTRACT = json.loads((_REPO / "docs" / "api" / "reports-contract.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("reports")
    spec = importlib.util.spec_from_file_location("dash_samples", _REPO / "scripts" / "dash-samples.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dash_samples"] = mod
    spec.loader.exec_module(mod)
    assert mod.main([str(out)]) == 0
    return out


def _walk(obj, path):
    """按 a.b[].c 取值;[] 表示列表的每个元素都要往下走。返回 (found: bool, where: str)。"""
    parts = path.split(".")
    cur = [obj]
    for i, p in enumerate(parts):
        nxt = []
        if p.endswith("[]"):
            key = p[:-2]
            for o in cur:
                if not isinstance(o, dict) or key not in o:
                    return False, ".".join(parts[:i]) + " 下没有 " + key
                if not isinstance(o[key], list):
                    return False, key + " 不是列表"
                if not o[key]:
                    return False, key + " 是空列表(样例得给一个元素,否则测不到里面的字段)"
                nxt.extend(o[key])
        else:
            for o in cur:
                if not isinstance(o, dict) or p not in o:
                    return False, ".".join(parts[:i]) + " 下没有 " + p
                nxt.append(o[p])
        cur = nxt
    return True, ""


def _check(name, obj):
    missing = []
    for path in CONTRACT[name]:
        optional = path.endswith("?")
        ok, why = _walk(obj, path.rstrip("?"))
        if not ok and not optional:
            missing.append("%s → %s(%s)" % (name, path, why))
    assert not missing, "前端引用了报告里不存在的字段:\n  " + "\n  ".join(missing)


def _load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_health_contract(samples):
    _check("health", _load(samples / "health" / "og5" / "latest.json"))


def test_topsql_contract(samples):
    _check("topsql", _load(samples / "topsql" / "og5" / "latest.json"))


def test_wdr_contract(samples):
    _check("wdr", _load(samples / "wdr" / "og5" / "latest.json"))


def test_sqltune_contract(samples):
    _check("sqltune", _load(samples / "sqltune" / "og5" / "1a9f.json"))


def test_kb_health_contract(samples):
    _check("kb_health", _load(samples / "kb" / "health.json"))


def test_kb_queries_contract(samples):
    rows = [json.loads(l) for l in (samples / "kb" / "queries.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows
    for r in rows:
        _check("kb_queries", r)


def test_index_targets_whoami_contract(samples):
    for e in _load(samples / "health" / "og5" / "index.json"):
        _check("index", e)
    for t in _load(samples / "health" / "targets.json"):
        _check("targets", t)
    _check("whoami", _load(samples / "whoami.json"))


def test_a_bogus_field_would_be_caught(samples):
    """钉子本身要能红:故意加一个不存在的字段。"""
    obj = _load(samples / "health" / "og5" / "latest.json")
    ok, why = _walk(obj, "dims[].no_such_field")
    assert not ok and "no_such_field" in why


def test_sqltune_skip_contract(samples):
    _check("sqltune_skip", _load(samples / "sqltune" / "og5" / "0b1c…e7.json"))


def test_kb_outline_contract(samples):
    """「目录」页签(渐进式披露)四条路:由 docker/kboutline 从样例知识库(含一套小的官方文档集)真实生成。"""
    _check("kb_outline", _load(samples / "_kb" / "outline.json"))
    _check("kb_list", _load(samples / "_kb" / "list.json"))
    for p in sorted((samples / "_kb" / "item").glob("*.json")):
        _check("kb_item", _load(p))
    for p in sorted((samples / "_kb" / "evidence").glob("*.json")):
        _check("kb_evidence", _load(p))
    assert len(list((samples / "_kb" / "item").glob("*.json"))) == 2, "官方文档一条、客户条目一条"


def test_kb_catalog_contract(samples):
    """知识库三个页签读的目录:由 docker/kbcatalog.build 从样例知识库真实生成。"""
    _check("kb_catalog", _load(samples / "_kb" / "catalog.json"))
