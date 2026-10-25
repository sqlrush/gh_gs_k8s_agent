"""统一 Finding —— health / wdr / memanalyze 原先各抄一份同样的 dataclass。

汇总层要靠它做跨 skill 的机器可读通道，所以 to_dict 的形状是**契约**：
health 解析子 skill 的 json 时认的就是这几个键，改键名等于改协议。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.finding import (  # noqa: E402
    Finding, Severity, findings_from_json, findings_to_json, worst,
)


def test_severity_ordering_is_worst_last():
    assert Severity.OK < Severity.NOTICE < Severity.WARN < Severity.CRITICAL


def test_worst_of_empty_is_ok():
    assert worst([]) is Severity.OK


def test_worst_picks_highest():
    assert worst([Severity.NOTICE, Severity.CRITICAL, Severity.WARN]) is Severity.CRITICAL


def test_labels_are_stable():
    assert Severity.CRITICAL.label() == "🔴严重"
    assert Severity.WARN.label() == "🟠告警"
    assert Severity.NOTICE.label() == "🟡关注"
    assert Severity.OK.label() == "🟢健康"


def _f(**kw):
    base = dict(dimension="Locks", code="LOCK_BLOCKED", severity=Severity.WARN,
                metric="阻塞会话数", value="3", threshold=">0", evidence="sess 2260 等 2259")
    base.update(kw)
    return Finding(**base)


def test_to_dict_keys_are_the_contract():
    d = _f().to_dict()
    assert set(d) == {"dimension", "code", "severity", "metric", "value",
                      "threshold", "evidence", "sql_id", "skill"}
    assert d["severity"] == 2, "severity 序列化成 int —— 跨进程要能比大小"


def test_findings_survive_a_json_round_trip():
    """**这是跨进程契约。** health 起子进程读 stdout，形状对不上就全盘失效。"""
    src = [_f(), _f(code="LOCK_ROOT", severity=Severity.CRITICAL)]
    back = findings_from_json(findings_to_json(src, skill="gaussdb-lockwait"))
    assert [f.code for f in back] == ["LOCK_BLOCKED", "LOCK_ROOT"]
    assert back[0].severity is Severity.WARN
    assert all(f.skill == "gaussdb-lockwait" for f in back), "来源必须带上"


def test_to_json_stamps_the_skill_name():
    payload = json.loads(findings_to_json([_f()], skill="gaussdb-vacuum"))
    assert payload["skill"] == "gaussdb-vacuum"
    assert payload["findings"][0]["skill"] == "gaussdb-vacuum"


def test_from_json_rejects_a_wrong_shape():
    """形状不对要当场报错，不能返回空列表 —— 空列表会被读成「这个 skill 没风险」。"""
    with pytest.raises(ValueError):
        findings_from_json(json.dumps({"nope": 1}))


def test_from_json_rejects_findings_being_none():
    """findings 是 None 不该冒出 TypeError —— 调用方只捕获 ValueError。"""
    with pytest.raises(ValueError):
        findings_from_json(json.dumps({"skill": "x", "findings": None}))


def test_from_json_rejects_findings_being_a_scalar():
    with pytest.raises(ValueError):
        findings_from_json(json.dumps({"skill": "x", "findings": 5}))


def test_from_json_rejects_a_list_of_non_dicts():
    with pytest.raises(ValueError):
        findings_from_json(json.dumps({"skill": "x", "findings": [1, 2, 3]}))


def test_finding_is_immutable():
    with pytest.raises(Exception):
        _f().code = "X"
