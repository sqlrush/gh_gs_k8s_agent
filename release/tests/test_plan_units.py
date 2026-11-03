"""升级计划:识别当前版本、首次部署 / 升级 / 回滚 / 接着发、累计变化、池子替换方式。"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import plan as pl  # noqa: E402
from release.package import Package  # noqa: E402

V3, V4, V5 = "agent-v1.4.3-oc1.18.27", "agent-v1.4.4-oc1.18.27", "agent-v1.4.5-oc1.18.27"
HISTORY = {
    "agent-v1.4.5": {"version": "agent-v1.4.5", "mixed_with": ["agent-v1.4.4"], "required": [{"map": "agent-config", "key": "NEW_X"}],
                     "steps": ["rebuild_kb_import"], "whitelist": ["d5.sql"], "notes": ["五"]},
    "agent-v1.4.4": {"version": "agent-v1.4.4", "mixed_with": ["agent-v1.4.3"], "required": [], "steps": [], "whitelist": [],
                     "notes": ["四"]},
    "agent-v1.4.3": {"version": "agent-v1.4.3", "mixed_with": [], "required": [], "steps": [], "whitelist": ["d3.sql"], "notes": []},
    "agent-v1.4.0": {"version": "agent-v1.4.0", "mixed_with": [], "required": [{"map": "agent-config", "key": "GRMP_PATH_PREFIX"}],
                     "steps": ["rebuild_kb_import"], "whitelist": [], "notes": []},
}


class FakeKube:
    def __init__(self, gw_tag="", cm_tag="", pool=None):
        self.gw_tag, self.cm_tag, self.pool = gw_tag, cm_tag, pool or {}

    def get(self, kind, name, namespaced=True):
        if kind == "deploy" and self.gw_tag:
            return {"spec": {"template": {"spec": {"containers": [{"image": "reg.x/ns/gaussdb-agent-gateway:" + self.gw_tag}]}}}}
        if kind == "configmap" and self.cm_tag:
            return {"data": {"USER_IMAGE_TAG": self.cm_tag}}
        return None

    def list(self, kind, selector="", namespaced=True):
        return [{"metadata": {"name": p}, "spec": {"containers": [{"name": c, "image": "gaussdb-agent-runtime:" + t}
                                                                  for c, t in cs.items()]}} for p, cs in self.pool.items()]


def pkg(tag, legacy=False, minimum="agent-v1.4.0"):
    return Package(tag=tag, root=pathlib.Path("/r"), files={"arm64": pathlib.Path("a"), "k8s": pathlib.Path("k")},
                   release=None if legacy else {"tag": tag, "min_upgrade_from": minimum})


def steps(p):
    return {s["id"]: s for s in p.steps}


def test_detect_reads_the_three_places():
    k = FakeKube(V3, V3, {"agent-pool-0": {"slot-0": V3, "steward": V3}})
    cur = pl.detect(k)
    assert cur.state == "ok" and cur.version == "agent-v1.4.3" and cur.registry == "reg.x/ns"
    k.pool["agent-pool-0"]["slot-0"] = V4
    assert pl.detect(k).state == "partial" and pl.detect(k).lagging() == ["agent-pool-0/slot-0"]
    assert pl.detect(FakeKube()).state == "none"


def test_upgrade_one_version_with_mixed_running_swaps_slots_online():
    p = pl.build(pl.Current("ok", V3, tags={"deploy/gateway": V3}), pkg(V4), HISTORY)
    assert (p.kind, p.from_v, p.to_v, p.pool_mode) == ("upgrade", "agent-v1.4.3", "agent-v1.4.4", "slot")
    assert p.problems == [] and p.notes == ["agent-v1.4.4:四"] and p.whitelist_files == []
    s = steps(p)
    assert s["kbimport"]["skip"] and not s["whitelist"]["skip"] and "按槽位在线替换" in s["pool"]["why"]


def test_skipping_versions_accumulates_requirements_and_falls_back_to_pod_replacement():
    p = pl.build(pl.Current("ok", V3, tags={"deploy/gateway": V3}), pkg(V5), HISTORY)
    assert p.required == [{"map": "agent-config", "key": "NEW_X"}] and p.special == ["rebuild_kb_import"]
    assert p.whitelist_files == ["d5.sql"] and p.notes == ["agent-v1.4.4:四", "agent-v1.4.5:五"]
    assert p.pool_mode == "pod", "v1.4.5 只声明能与 v1.4.4 混跑,从 v1.4.3 来就按 Pod 整体替换"
    s = steps(p)
    assert s["pool"]["name"] == "池子按 Pod 替换" and not s["kbimport"]["skip"]


def test_rollback_skips_whitelist_and_kbstd_and_uses_the_newer_versions_compatibility():
    p = pl.build(pl.Current("ok", V4, tags={"deploy/gateway": V4}), pkg(V3, legacy=True), HISTORY)
    assert (p.kind, p.pool_mode) == ("rollback", "slot") and p.problems == []
    s = steps(p)
    assert s["whitelist"]["skip"] and s["kbstd"]["skip"] and s["kbimport"]["skip"]


def test_fresh_deploy_requires_everything_ever_required():
    p = pl.build(pl.Current("none"), pkg(V4), HISTORY)
    assert p.kind == "deploy" and p.pool_mode == "fresh"
    assert {"map": "agent-config", "key": "GRMP_PATH_PREFIX"} in p.required
    s = steps(p)
    assert s["gateway"]["name"] == "部署基础资源" and s["pool"]["name"] == "部署池子"


def test_an_unfinished_release_can_only_be_resumed_or_rolled_back():
    cur = pl.Current("partial", V4, tags={"deploy/gateway": V4, "agent-pool-0/slot-0": V3})
    assert pl.build(cur, pkg(V4), HISTORY).kind == "resume"
    assert pl.build(cur, pkg(V4), HISTORY).problems == []
    p = pl.build(cur, pkg(V5), HISTORY)
    assert any("没有走完" in x and "agent-pool-0/slot-0" in x for x in p.problems)
    assert pl.build(cur, pkg(V3, legacy=True), HISTORY).kind == "rollback"


def test_refusals_say_why():
    assert any("已经是" in x for x in pl.build(pl.Current("ok", V4), pkg(V4), HISTORY).problems)
    assert any("只能作为回滚目标" in x for x in pl.build(pl.Current("ok", V3), pkg(V4, legacy=True), HISTORY).problems)
    old = pl.Current("ok", "agent-v1.3.2-oc1.18.27")
    assert any("§14" in x for x in pl.build(old, pkg(V4), HISTORY).problems)
    assert any("没有变化说明" in x for x in pl.build(pl.Current("ok", V3), pkg(V4), {}).problems)


def test_detect_reads_versions_from_digest_pinned_images():
    pinned = V3 + "@sha256:" + "c" * 64
    k = FakeKube(pinned, V3, {"agent-pool-0": {"slot-0": pinned, "steward": pinned}})
    cur = pl.detect(k)
    assert cur.state == "ok" and cur.version == "agent-v1.4.3" and cur.registry == "reg.x/ns", "钉了 digest 也要认得出版本"
