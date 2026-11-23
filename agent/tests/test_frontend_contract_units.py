"""前端 ↔ 报告的跨语言契约守卫。

frontend/lib/contract.json 列出每个页面读到的字段路径;这里用 scripts/dash-samples.py **同一批构造函数**
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

CONTRACT = json.loads((_REPO / "frontend" / "lib" / "contract.json").read_text(encoding="utf-8"))


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


def test_contract_lists_every_page_the_frontend_has():
    """frontend/pages/ 里每个页面在契约里都要有对应的段 —— 加了页面忘了写契约,守卫就守不到它。"""
    pages = {p.stem for p in (_REPO / "frontend" / "pages").glob("*.js")}
    covered = {"health", "topsql", "wdr", "kb"}
    # 资源大盘(v1.5.0)不读技能写的报告文件,读的是网关自己答的 /dash/api/resources:
    # 它的字段由网关的测试钉住(gateway/tests/test_dashdata_units.py、test_dash_api_units.py),不在这份契约里
    from_gateway = {"resources"}
    assert pages == covered | from_gateway, "pages/ 与契约不一致:%s" % (pages ^ (covered | from_gateway))


def test_a_bogus_field_would_be_caught(samples):
    """钉子本身要能红:故意加一个不存在的字段。"""
    obj = _load(samples / "health" / "og5" / "latest.json")
    ok, why = _walk(obj, "dims[].no_such_field")
    assert not ok and "no_such_field" in why


def test_sqltune_skip_contract(samples):
    _check("sqltune_skip", _load(samples / "sqltune" / "og5" / "0b1c…e7.json"))


# ---------------------------------------------------------------- 维度名 / 列名:不只是字段路径
# 第一版契约只核对字段路径(dims[].rows 存在就算过),而样例的维度名和行列形状是按设计稿编的:
# 样例上全绿,真集群上 WDR 的 8 个 KPI 全是「未采集」、健康检查的维度中文名一个都没对上。
# 这三条直接拿技能源码里的常量与表头去对页面源码 —— 技能改名或页面写错,这里先红。

import re  # noqa: E402

_SKILLS = _ROOT / "skills"
_PAGES = _REPO / "frontend" / "pages"


def _dim_constants(skill: str) -> set:
    src = (_SKILLS / skill / "scripts" / "model.py").read_text(encoding="utf-8")
    return set(re.findall(r'^DIM_\w+\s*=\s*"([^"]+)"', src, re.MULTILINE))


def _js_block(page: str, name: str) -> str:
    src = (_PAGES / page).read_text(encoding="utf-8")
    m = re.search(r"const %s = \{(.*?)\};" % name, src, re.S)
    assert m, "%s 里找不到 %s" % (page, name)
    return m.group(1)


def test_wdr_page_dimension_names_are_the_skills_constants():
    used = set(re.findall(r":\s*'([^']+)'", _js_block("wdr.js", "DIMS")))
    real = _dim_constants("gaussdb-wdr")
    assert used and used <= real, "wdr.js 的 DIMS 有技能里不存在的维度名:%s(真实的:%s)" % (sorted(used - real), sorted(real))


def test_health_page_titles_are_keyed_by_the_skills_constants():
    used = set(re.findall(r"'([^']+)'\s*:", _js_block("health.js", "TITLES")))
    real = _dim_constants("gaussdb-health")
    assert used and used <= real, "health.js 的 TITLES 有技能里不存在的维度名:%s" % sorted(used - real)
    # 健康检查实际会产出的维度(此刻巡检 collectors.py + 时段体检 window.py)都要有中文名,不然页面上中英混排
    produced = set(re.findall(r"dimension=(DIM_\w+)", _health_producers()))
    src = (_SKILLS / "gaussdb-health" / "scripts" / "model.py").read_text(encoding="utf-8")
    names = {k: v for k, v in re.findall(r'^(DIM_\w+)\s*=\s*"([^"]+)"', src, re.MULTILINE)}
    missing = {names[k] for k in produced if k in names} - used
    assert not missing, "健康检查会产出、页面却没有中文名的维度:%s" % sorted(missing)


def test_wdr_kpi_columns_exist_in_the_collectors_headers():
    src = (_PAGES / "wdr.js").read_text(encoding="utf-8")
    cols = set(re.findall(r"col:\s*'([^']+)'", src)) | set(re.findall(r"val\(d, '\w+', '([^']+)'\)", src))
    coll = (_SKILLS / "gaussdb-wdr" / "scripts" / "collectors.py").read_text(encoding="utf-8")
    headers = [h for block in re.findall(r"headers=\[([^\]]*)\]", coll) for h in re.findall(r'"([^"]+)"', block)]
    missing = [c for c in cols if not any(h.startswith(c) for h in headers)]
    assert cols and not missing, "WDR 页按列名取值,采集代码的表头里没有这些列:%s" % missing


def _health_producers() -> str:
    """产出健康检查维度的源码:此刻巡检(collectors.py)与时段体检(window.py)。"""
    d = _SKILLS / "gaussdb-health" / "scripts"
    return "\n".join((d / f).read_text(encoding="utf-8") for f in ("collectors.py", "window.py"))


def test_health_key_figures_read_real_collector_columns():
    """健康检查各维度的关键读数按表头前缀取值(pages/health.js 里的 cell(d, 行, '列名')):列名要在采集代码的表头里,
    否则真数据上维度格只剩「—」。"""
    src = (_PAGES / "health.js").read_text(encoding="utf-8")
    cols = set(re.findall(r"cell\(d, \w+, '([^']+)'\)", src))
    coll = _health_producers()
    headers = [h for block in re.findall(r"headers=\[([^\]]*)\]", coll) for h in re.findall(r'"([^"]+)"', block)]
    missing = [c for c in cols if not any(h.startswith(c) for h in headers)]
    assert cols and not missing, "健康检查页按列名取读数,采集代码的表头里没有这些列:%s" % missing


def test_health_window_key_figures_read_real_item_names():
    """时段体检的「项 / 值」表按项名取值(pages/health.js 里的 item(d, '项名')):项名要在 window.py 里原样出现,
    否则时段体检的维度格只剩「—」。"""
    src = (_PAGES / "health.js").read_text(encoding="utf-8")
    names = set(re.findall(r"item\(d, '([^']+)'\)", src))
    win = (_SKILLS / "gaussdb-health" / "scripts" / "window.py").read_text(encoding="utf-8")
    missing = [n for n in names if '"%s"' % n not in win]
    assert names and not missing, "健康检查页按项名取读数,window.py 里没有这些项:%s" % missing


def test_kb_outline_contract(samples):
    """「目录」页签(渐进式披露)四条路:由 docker/kboutline 从样例知识库(含一套小的官方文档集)真实生成。"""
    _check("kb_outline", _load(samples / "_kb" / "outline.json"))
    _check("kb_list", _load(samples / "_kb" / "list.json"))
    for p in sorted((samples / "_kb" / "item").glob("*.json")):
        _check("kb_item", _load(p))
    for p in sorted((samples / "_kb" / "evidence").glob("*.json")):
        _check("kb_evidence", _load(p))
    assert len(list((samples / "_kb" / "item").glob("*.json"))) == 2, "官方文档一条、本行条目一条"


def test_kb_catalog_contract(samples):
    """知识库三个页签读的目录:由 docker/kbcatalog.build 从样例知识库真实生成。"""
    _check("kb_catalog", _load(samples / "_kb" / "catalog.json"))


def test_topsql_tabs_are_the_skills_sort_keys():
    """大盘 Top SQL 的页签(pages/topsql.js 的 BY)只能是技能 --by 认的维度 —— 页签多一个,点开就是「还没取过」、
    去会话里取也会被技能拒掉;维度改名,存档文件名 <时间戳>.<维度>.json 也就对不上了。"""
    src = (_SKILLS / "gaussdb-topsql" / "scripts" / "topsql.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'"(\w+)"', re.search(r"^SORT_KEYS\s*=\s*\[(.*?)\]", src, re.M).group(1)))
    page = (_PAGES / "topsql.js").read_text(encoding="utf-8")
    tabs = re.findall(r"\['(\w+)', '[^']+', '\w+'\]", page.split("export const BY", 1)[1].split("];", 1)[0])
    assert {"time", "avg", "self", "self_avg"} <= set(tabs), tabs
    if not {"self", "self_avg"} <= keys:
        pytest.skip("agent/ 的 gaussdb-topsql 还没同步 gh_skill 60615b4(--by self / self_avg),同步后这条才生效")
    assert set(tabs) <= keys, "大盘有、技能没有的维度:%s" % (set(tabs) - keys)


def test_kb_type_names_on_the_page_are_the_libs():
    """大盘知识库页的类型简称与顺序(lib/kb-docs.js 的 KTYPES)要与 common/kb/types.py 的 TYPE_NAMES 一致:
    模型在回答里写〔手册 RB-LCK-001〕,大盘上同一条也叫「手册」。"""
    from common.kb import types as kbtypes
    src = (_REPO / "frontend" / "lib" / "kb-docs.js").read_text(encoding="utf-8")
    m = re.search(r"export const KTYPES = \[(.*?)\];", src, re.S)
    assert m, "kb-docs.js 里找不到 KTYPES"
    js = re.findall(r"\['([^']+)', '([^']+)', '([^']+)'\]", m.group(1))
    assert [(c, s, n) for c, s, n in js] == [(c, s, n) for c, (n, s) in kbtypes.TYPE_NAMES.items()]
    eight = re.search(r"export const EIGHT = \[(.*?)\];", src).group(1)
    assert set(re.findall(r"'([^']+)'", eight)) == set(kbtypes.EIGHT_TYPES)
