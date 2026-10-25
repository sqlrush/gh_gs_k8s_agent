"""固定池化清单的守卫(k8s/tools/render-pool.py,2026-10-23)。

隔离里有好几条是**清单**决定的,代码管不到,改一行就悄悄没了:不共享进程空间、只有管家挂 users/ 与持有控制密钥、
槽位各自的 uid 与盘、指令目录只读、端口与代码一致、正式清单里没有测试开关。按结构解析渲染结果逐条核对。
"""
import importlib.util
import pathlib
import re
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import poolrun  # noqa: E402
import poolslot  # noqa: E402
import poolsteward  # noqa: E402
import slotrun  # noqa: E402

_spec = importlib.util.spec_from_file_location("render_pool", _ROOT / "k8s" / "tools" / "render-pool.py")
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

IMAGE = "ghcr.io/x/gaussdb-agent-runtime:t"


def _docs(*extra):
    return list(yaml.safe_load_all(rp.to_yaml(rp.render(rp.parse(["--image", IMAGE] + list(extra))))))


@pytest.fixture(scope="module")
def docs():
    return _docs()


def _kind(docs, kind, name=None):
    return next(d for d in docs if d["kind"] == kind and (name is None or d["metadata"]["name"] == name))


@pytest.fixture(scope="module")
def spec(docs):
    return _kind(docs, "StatefulSet")["spec"]["template"]["spec"]


def _slots(spec):
    return [c for c in spec["containers"] if c["name"].startswith("slot-")]


def _steward(spec):
    return next(c for c in spec["containers"] if c["name"] == "steward")


def _env(c):
    return {e["name"]: e for e in c.get("env", [])}


def test_renders_the_five_objects_with_the_defaults(docs, spec):
    assert sorted(d["kind"] for d in docs) == ["NetworkPolicy", "NetworkPolicy", "PodDisruptionBudget", "Service",
                                              "StatefulSet"]
    sts = _kind(docs, "StatefulSet")["spec"]
    assert sts["replicas"] == 16 and len(_slots(spec)) == 8
    assert [c["name"] for c in spec["containers"]] == ["slot-%d" % i for i in range(8)] + ["steward"]
    assert all(c["image"] == IMAGE for c in spec["containers"])


def test_statefulset_never_rolls_pods_by_itself_and_service_publishes_not_ready(docs):
    sts = _kind(docs, "StatefulSet")["spec"]
    assert sts["updateStrategy"] == {"type": "OnDelete"}, "改清单不能自动滚掉正在用的 Pod"
    assert sts["podManagementPolicy"] == "Parallel"
    svc = _kind(docs, "Service")["spec"]
    assert svc["clusterIP"] == "None" and svc["publishNotReadyAddresses"] is True, "某个槽位重启时整个 Pod NotReady"
    assert _kind(docs, "PodDisruptionBudget")["spec"]["maxUnavailable"] == 1


def test_no_shared_process_namespace_no_sa_token_and_everything_locked(spec):
    assert spec.get("shareProcessNamespace", False) is False
    assert spec["automountServiceAccountToken"] is False and spec["enableServiceLinks"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True and spec["securityContext"]["fsGroup"] == 1000
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    for c in spec["containers"]:
        sc = c["securityContext"]
        assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False, c["name"]
        assert sc["capabilities"] == {"drop": ["ALL"]} and "privileged" not in sc, c["name"]
        assert sc["runAsNonRoot"] is True and sc["runAsGroup"] == 1000, c["name"]


def test_every_slot_has_its_own_uid_and_the_steward_is_1000(spec):
    uids = [c["securityContext"]["runAsUser"] for c in _slots(spec)]
    assert uids == [poolsteward.SLOT_UID_BASE + i for i in range(8)] and rp.SLOT_UID_BASE == poolsteward.SLOT_UID_BASE
    assert _steward(spec)["securityContext"]["runAsUser"] == 1000


def test_only_the_steward_sees_users_and_holds_the_control_key(spec):
    st = _steward(spec)
    assert [m["subPath"] for m in st["volumeMounts"] if m["name"] == "nas"] == ["users"]
    assert _env(st)["SANDBOX_CONTROL_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "sandbox-control", "key": "KEY"}
    for c in _slots(spec):
        nas = [m for m in c["volumeMounts"] if m["name"] == "nas"]
        assert nas == [{"name": "nas", "mountPath": "/nas/kb", "subPath": "kb", "readOnly": True}], c["name"]
        text = yaml.safe_dump(c)
        for word in ("sandbox-control", "SANDBOX_CONTROL_KEY", "GRMP_AUTH_TOKEN", "grmp-token", "MODEL_API_KEY",
                     "model-api", "GRMP_SM2_PRIVATE_KEY", "grmp-sm2", "OPENCODE_SERVER_PASSWORD", "/pool"):
            assert word not in text, "%s 里不能有 %s(凭据都在管家的凭据代理里)" % (c["name"], word)
    for name, ref in (("MODEL_API_KEY", {"name": "model-api", "key": "MODEL_API_KEY"}),
                      ("GRMP_SM2_PRIVATE_KEY", {"name": "grmp-sm2", "key": "GRMP_SM2_PRIVATE_KEY", "optional": True})):
        assert _env(st)[name]["valueFrom"]["secretKeyRef"] == ref, "凭据代理在管家:%s" % name


def test_slots_mount_only_their_own_disks_and_ctl_read_only(spec):
    for i, c in enumerate(_slots(spec)):
        mine = {m["name"] for m in c["volumeMounts"]} - {"nas"}
        assert mine == {"s%d-%s" % (i, k) for k in ("data", "state", "ctl", "tmp", "shm", "sock")}, c["name"]
        for k in ("ctl", "sock"):
            m = next(m for m in c["volumeMounts"] if m["name"] == "s%d-%s" % (i, k))
            assert m["readOnly"] is True, "指令目录与凭据代理套接字目录在槽位里必须只读"
    st = {m["name"] for m in _steward(spec)["volumeMounts"]}
    assert {"s%d-%s" % (i, k) for i in range(8) for k in ("state", "ctl", "sock")} <= st
    assert not any(re.match(r"s\d+-(data|tmp|shm)$", n) for n in st), "管家不需要槽位的配置盘、tmp 与 shm"


def test_ports_and_paths_match_the_code(spec):
    for i, c in enumerate(_slots(spec)):
        assert [p["containerPort"] for p in c["ports"]] == list(slotrun.slot_ports(i)) == list(rp.slot_ports(i))
        mounts = {m["mountPath"] for m in c["volumeMounts"]}
        rc = poolrun.Config()
        assert {str(rc.ctl_dir), str(rc.state), str(rc.kb_dir)} <= mounts
        assert {str(p) for p in slotrun.SlotConfig(slot=i, run=rc).wipe} <= mounts, "清盘的每个路径都要是自己的盘"
        env = _env(c)
        assert env["POOL_MODE"]["value"] == "slot" and env["SLOT_INDEX"]["value"] == str(i)
    st = _steward(spec)
    cfg = poolsteward.Config(key=b"k" * 16, pod="x")
    mounts = {m["name"]: m["mountPath"] for m in st["volumeMounts"]}
    assert mounts["nas"] == str(cfg.users_dir) and mounts["priv"] == str(cfg.priv_dir)
    assert mounts["s3-state"] == str(cfg.slots_dir / "3" / "state") and mounts["s3-ctl"] == str(cfg.slots_dir / "3" / "ctl")
    assert st["ports"] == [{"containerPort": cfg.port, "name": "control"}]
    assert st["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": cfg.port}
    env = _env(st)
    assert env["POOL_MODE"]["value"] == "steward" and env["SLOTS_PER_POD"]["value"] == "8"
    ep = (_ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert '"${POOL_MODE:-0}" = "slot"' in ep and '"${POOL_MODE:-0}" = "steward"' in ep


def test_grace_period_covers_the_parallel_retain(spec):
    assert spec["terminationGracePeriodSeconds"] >= poolslot.Settings(pod="x").stop_wait_seconds + 60


def test_emptydirs_are_bounded_and_only_shm_is_in_memory(spec):
    for v in spec["volumes"]:
        if "emptyDir" in v:
            assert "sizeLimit" in v["emptyDir"], v
            assert ("medium" in v["emptyDir"]) == v["name"].endswith("-shm"), v


def test_production_render_has_no_test_switches(docs):
    for d in docs:
        assert rp.TEST_LABEL not in (d["metadata"].get("labels") or {})
    st = _env(_steward(_kind(docs, "StatefulSet")["spec"]["template"]["spec"]))
    assert set(st) == {"POOL_MODE", "POD_NAME", "SLOTS_PER_POD", "SANDBOX_CONTROL_KEY", "MODEL_API_KEY",
                       "GRMP_SM2_PRIVATE_KEY"}


def test_test_overrides_are_labelled():
    docs = _docs("--test-override", "STATE_SYNC_SECONDS=10", "--pool-size", "3", "--slots-per-pod", "3")
    sts = _kind(docs, "StatefulSet")
    assert sts["metadata"]["labels"][rp.TEST_LABEL] == "true"
    assert sts["spec"]["template"]["metadata"]["labels"][rp.TEST_LABEL] == "true"
    spec = sts["spec"]["template"]["spec"]
    assert _env(_steward(spec))["STATE_SYNC_SECONDS"]["value"] == "10" and len(_slots(spec)) == 3


def test_network_policies_admit_only_the_gateway_on_exactly_the_pool_ports(docs):
    want = {4098} | {p for i in range(8) for p in slotrun.slot_ports(i)}
    pol = _kind(docs, "NetworkPolicy", "pool-policy")["spec"]
    assert pol["podSelector"]["matchLabels"] == {"app": "gaussdb-agent", "role": "pool"}
    assert set(pol["policyTypes"]) == {"Ingress", "Egress"}
    assert {p["port"] for r in pol["ingress"] for p in r["ports"]} == want
    for r in pol["ingress"]:
        for src in r["from"]:
            sel = (src.get("podSelector") or src.get("namespaceSelector"))["matchLabels"]
            assert sel in ({"app": "agent-gateway"}, {"kubernetes.io/metadata.name": "agent-gateway"}), src
    out = {p["port"] for r in pol["egress"] for p in r.get("ports", [])}
    assert 2049 not in out and 111 not in out, "不能放行 NFS:模型会自己去连 NFS 服务器"
    gw = _kind(docs, "NetworkPolicy", "gateway-to-pool")["spec"]
    assert gw["policyTypes"] == ["Egress"] and {p["port"] for r in gw["egress"] for p in r["ports"]} == want


def test_extra_egress_targets_are_rendered_and_bad_ones_refused():
    docs = _docs("--egress", "10.1.2.3/32:11434", "--egress", "10.9.0.0/16:8443,9443")
    eg = _kind(docs, "NetworkPolicy", "pool-policy")["spec"]["egress"]
    assert {"to": [{"ipBlock": {"cidr": "10.1.2.3/32"}}], "ports": [{"port": 11434, "protocol": "TCP"}]} in eg
    assert {p["port"] for r in eg if r["to"][0].get("ipBlock", {}).get("cidr") == "10.9.0.0/16" for p in r["ports"]} == \
        {8443, 9443}
    for bad in ("10.1.2.3:11434", "10.1.2.3/32:", "10.1.2.3/32:x"):
        with pytest.raises(SystemExit):
            rp.parse(["--image", IMAGE, "--egress", bad])


def test_the_image_has_users_for_every_possible_slot():
    src = (_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"for i in \$\(seq 0 (\d+)\); do useradd --uid \$\(\((\d+) \+ i\)\)", src)
    assert m and int(m.group(1)) + 1 >= rp.MAX_SLOTS and int(m.group(2)) == rp.SLOT_UID_BASE


@pytest.mark.parametrize("args", [["--pool-size", "0"], ["--slots-per-pod", "17"], ["--slot-memory-request", "lots"],
                                  ["--test-override", "lower=1"], ["--test-override", "NOEQUALS"],
                                  ["--model-ports", "x"]])
def test_bad_arguments_are_refused(args):
    with pytest.raises(SystemExit):
        rp.parse(["--image", IMAGE] + args)


def test_yaml_output_round_trips_tricky_strings():
    obj = {"a": "true", "b": "1", "c": "x: y", "d": "", "e": [], "f": {}, "g": [{"h": "中文", "i": None}],
           "on": False, "k/l": 3}
    assert yaml.safe_load(rp.to_yaml([obj])) == obj
