"""网关配置层:这一层的职责是 fail closed。

网关按请求头里的工号决定「进谁的环境」。信任配置漏了,它就是一个
「任填工号即可进入任何人环境」的接口 —— **而且从外面看它工作得好好的**,
没有报错、没有告警,只有等到出事才知道。所以宁可拒绝启动。

同理:回收模式、闲置时长这类值不认识时也拒绝启动,不要悄悄用默认值 ——
运维以为配的是 delete 实际跑的是 scale,Pod 一个都没回收,过一周才发现。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import config as cfg  # noqa: E402
from gateway import reaper  # noqa: E402

_MIN = {"GATEWAY_NAMESPACE": "gaussdb-agent", "GATEWAY_IMAGE_TAG": "v1",
        "GATEWAY_TRUST_SECRET": "s" * 32}


def test_minimal_config_loads():
    c = cfg.load(_MIN)
    assert c.namespace == "gaussdb-agent" and c.image_tag == "v1" and c.trust_configured


def test_no_trust_source_refuses_to_start():
    """**本层最重要的一条。**"""
    env = {k: v for k, v in _MIN.items() if k != "GATEWAY_TRUST_SECRET"}
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load(env)
    assert "任填工号" in str(exc.value), "报错要说清为什么不能这么跑,不然运维会去找开关关掉它"


def test_cidr_alone_is_enough():
    env = {k: v for k, v in _MIN.items() if k != "GATEWAY_TRUST_SECRET"}
    env["GATEWAY_TRUST_CIDRS"] = "10.9.0.0/24, 10.9.1.0/24"
    c = cfg.load(env)
    assert c.trust_cidrs == ("10.9.0.0/24", "10.9.1.0/24")


def test_short_secret_is_refused():
    with pytest.raises(cfg.ConfigError):
        cfg.load({**_MIN, "GATEWAY_TRUST_SECRET": "short"})


def test_missing_image_tag_is_refused():
    """不给默认值:默认成 latest 会让「升级」悄悄发生在下一次用户登录时。"""
    env = {k: v for k, v in _MIN.items() if k != "GATEWAY_IMAGE_TAG"}
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load(env)
    assert "latest" in str(exc.value)


def test_missing_namespace_is_refused():
    env = {k: v for k, v in _MIN.items() if k != "GATEWAY_NAMESPACE"}
    with pytest.raises(cfg.ConfigError):
        cfg.load(env)


def test_unknown_reap_mode_is_refused():
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load({**_MIN, "GATEWAY_REAP_MODE": "destroy"})
    assert "destroy" in str(exc.value)


@pytest.mark.parametrize("mode", [reaper.MODE_DELETE, reaper.MODE_SCALE])
def test_both_reap_modes_accepted(mode):
    assert cfg.load({**_MIN, "GATEWAY_REAP_MODE": mode}).reap_mode == mode


def test_too_short_idle_is_refused():
    """模型跑长任务时几分钟没有流量是正常的;闲置配得太短会在用户等结果时收掉 Pod。"""
    with pytest.raises(cfg.ConfigError):
        cfg.load({**_MIN, "GATEWAY_IDLE_SECONDS": "60"})


def test_non_integer_is_refused_not_silently_defaulted():
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load({**_MIN, "GATEWAY_IDLE_SECONDS": "半小时"})
    assert "GATEWAY_IDLE_SECONDS" in str(exc.value)


def test_admin_token_empty_means_control_api_off():
    assert cfg.load(_MIN).admin_token == ""


def test_headers_are_configurable():
    """头名按客户实际填。示例一律用中性值 —— 本仓是公开仓,不放客户标识。"""
    c = cfg.load({**_MIN, "GATEWAY_USER_HEADER": "X-Staff-No", "GATEWAY_ROLES_HEADER": "X-Staff-Groups"})
    assert c.user_header == "X-Staff-No" and c.roles_header == "X-Staff-Groups"


def test_frontend_service_defaults_and_overrides():
    """/dash/* 代理到哪个 Service。默认 frontend;ConfigMap 键 FRONTEND_SERVICE 经 gateway.yaml 映射进来。"""
    assert cfg.load(_MIN).frontend_service == "frontend"
    assert cfg.load({**_MIN, "GATEWAY_FRONTEND_SERVICE": "dash-web"}).frontend_service == "dash-web"


def test_image_registry_is_optional_and_normalised():
    assert cfg.load(_MIN).image_registry == ""
    assert cfg.load({**_MIN, "GATEWAY_IMAGE_REGISTRY": " registry.example/gaussdb-agent/ "}).image_registry \
        == "registry.example/gaussdb-agent"


@pytest.mark.parametrize("bad", ["http://registry.example/x", "registry.example/ x"])
def test_image_registry_with_scheme_or_spaces_is_refused(bad):
    """写成 http://... 的话 k8s 拒收镜像名,但要等到第一个用户登录才以 504 的形态暴露。"""
    with pytest.raises(cfg.ConfigError):
        cfg.load({**_MIN, "GATEWAY_IMAGE_REGISTRY": bad})


# -- 固定池化(2026-10-23) ----------------------------------------------------------------

def test_runtime_mode_defaults_to_on_demand():
    c = cfg.load(_MIN)
    assert c.runtime_mode == cfg.RUNTIME_ONDEMAND and c.pool_idle_seconds == 1800 and c.pool_wait_seconds == 60


def test_unknown_runtime_mode_is_refused():
    with pytest.raises(cfg.ConfigError):
        cfg.load(dict(_MIN, GATEWAY_RUNTIME_MODE="sticky"))


def test_pool_mode_needs_the_control_key():
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load(dict(_MIN, GATEWAY_RUNTIME_MODE="pool"))
    assert "GATEWAY_POOL_CONTROL_KEY" in str(exc.value)
    c = cfg.load(dict(_MIN, GATEWAY_RUNTIME_MODE="POOL", GATEWAY_POOL_CONTROL_KEY="k" * 32,
                      GATEWAY_POOL_IDLE_SECONDS="900", GATEWAY_POOL_EARLY_IDLE_SECONDS="300"))
    assert c.runtime_mode == cfg.RUNTIME_POOL and c.pool_idle_seconds == 900 and c.pool_early_idle_seconds == 300


@pytest.mark.parametrize("key,value", [("GATEWAY_POOL_IDLE_SECONDS", "60"), ("GATEWAY_POOL_EARLY_IDLE_SECONDS", "10"),
                                       ("GATEWAY_POOL_WAIT_SECONDS", "3"), ("GATEWAY_POOL_RETAIN_DAYS", "0"),
                                       ("GATEWAY_POOL_SWEEP_SECONDS", "1")])
def test_pool_timers_below_their_floor_are_refused(key, value):
    with pytest.raises(cfg.ConfigError):
        cfg.load(dict(_MIN, **{key: value}))


def test_early_retain_longer_than_idle_is_refused():
    with pytest.raises(cfg.ConfigError):
        cfg.load(dict(_MIN, GATEWAY_POOL_IDLE_SECONDS="600", GATEWAY_POOL_EARLY_IDLE_SECONDS="900"))


def test_the_test_switch_lowers_the_floors_and_is_not_in_any_manifest():
    c = cfg.load(dict(_MIN, **{cfg.TEST_SHORT_TIMERS: "1", "GATEWAY_POOL_IDLE_SECONDS": "40",
                               "GATEWAY_POOL_EARLY_IDLE_SECONDS": "20", "GATEWAY_POOL_WAIT_SECONDS": "10"}))
    assert c.pool_idle_seconds == 40 and c.pool_early_idle_seconds == 20
    assert not cfg.TEST_SHORT_TIMERS.startswith("GATEWAY_"), "带 GATEWAY_ 前缀会被守卫要求接进正式清单"
    for path in (_ROOT / "k8s" / "base").glob("*.yaml"):
        assert cfg.TEST_SHORT_TIMERS not in path.read_text(encoding="utf-8"), "%s 带了测试开关" % path.name
    for path in (_ROOT / "k8s" / "templates").glob("*.yaml"):
        assert cfg.TEST_SHORT_TIMERS not in path.read_text(encoding="utf-8")
