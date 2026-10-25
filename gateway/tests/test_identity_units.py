"""网关身份层:凭什么相信请求头里的工号。

这是整个网关的安全边界。客户的 SSO / WAF 认完人把工号放进头里交过来,
**谁能把这个头发进来谁就能冒充任何人** —— 所以测试的重点在「不可信来源一律拒」,
而不是「可信来源解析得对」。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import identity as idt  # noqa: E402

SECRET = "s" * 32
H = "x-agent-user"
R = "x-agent-roles"


def _extract(headers, peer="10.9.0.5", secret=SECRET, cidrs=()):
    return idt.extract(headers, peer, header_name=H, roles_header=R, secret=secret, cidrs=cidrs)


# --- 信任 ---------------------------------------------------------------------

def test_shared_secret_lets_it_through():
    assert _extract({idt.HEADER_TRUST: SECRET, H: "u1234"}).user_id == "u1234"


def test_without_the_secret_it_is_refused():
    """**核心**:没有信任凭据时,哪怕工号写得再漂亮也不认。"""
    with pytest.raises(idt.IdentityError):
        _extract({H: "u1234"})


def test_wrong_secret_is_refused():
    with pytest.raises(idt.IdentityError):
        _extract({idt.HEADER_TRUST: "x" * 32, H: "u1234"})


def test_cidr_alone_can_establish_trust():
    """有些环境不方便配共享密钥,只能按来源网段。"""
    assert _extract({H: "u1234"}, peer="10.9.0.5", secret="", cidrs=("10.9.0.0/24",)).user_id == "u1234"


def test_peer_outside_the_cidr_is_refused():
    with pytest.raises(idt.IdentityError):
        _extract({H: "u1234"}, peer="192.168.1.1", secret="", cidrs=("10.9.0.0/24",))


def test_no_trust_configured_refuses_everything():
    """两道门都没配时一律拒。config 层还会在启动时就拦住这种部署,这里是第二道。"""
    with pytest.raises(idt.IdentityError):
        _extract({H: "u1234"}, secret="", cidrs=())


def test_malformed_cidr_does_not_open_the_door():
    """配错一个网段不能让整条链路变成可信。"""
    with pytest.raises(idt.IdentityError):
        _extract({H: "u1234"}, peer="10.9.0.5", secret="", cidrs=("not-a-cidr",))


def test_trust_header_name_is_case_insensitive():
    """HTTP 头名不区分大小写,上游可能发 X-Agent-Trust。"""
    assert _extract({"X-Agent-Trust": SECRET, H: "u1234"}).user_id == "u1234"


# --- 工号格式 -----------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "u1234; rm -rf /",          # 命令注入
    "../../etc/passwd",         # 路径穿越 —— 工号会拼进 NAS 路径 users/<工号>
    "u_1234",                   # 下划线不是 RFC 1123 标签的合法字符
    "-u1234", "u1234-",         # 首尾必须是字母数字
    "u" * 60,                   # runtime-<工号> 会超过 63 字符的标签上限
    "用户1234",                  # 非 ASCII
])
def test_illegal_user_ids_are_refused(bad):
    with pytest.raises(idt.IdentityError):
        _extract({idt.HEADER_TRUST: SECRET, H: bad})


def test_error_does_not_echo_the_bad_value():
    """报错里不要回显原值 —— 它会被原样写进日志与错误页,成了注入的落点。"""
    with pytest.raises(idt.IdentityError) as exc:
        _extract({idt.HEADER_TRUST: SECRET, H: "<script>alert(1)</script>"})
    assert "script" not in str(exc.value)


def test_user_id_is_lowercased():
    """大小写不统一会变成两个 Pod、两份 NAS 目录、两套互相看不见的历史。"""
    assert _extract({idt.HEADER_TRUST: SECRET, H: "U1234"}).user_id == "u1234"


def test_missing_user_header_is_refused():
    with pytest.raises(idt.IdentityError):
        _extract({idt.HEADER_TRUST: SECRET})


# --- 角色 ---------------------------------------------------------------------

def test_roles_are_parsed_normalized_and_stripped():
    """本模块只负责把角色解析出来并归一化(去空白、转小写)。

    「哪些角色算知识库管理员」是配置项 KB_ADMIN_ROLES,判定在 server.py ——
    这里曾经有个 is_kb_admin 属性把 "kb-admin" 写死,两条路并存迟早会让那个
    配置项静默失效:谁顺手用了属性,配置改了也不生效。已删掉,只留解析。
    """
    i = _extract({idt.HEADER_TRUST: SECRET, H: "u1234", R: "dba, KB-Admin"})
    assert i.roles == ("dba", "kb-admin")


def test_no_roles_header_means_empty_roles():
    assert _extract({idt.HEADER_TRUST: SECRET, H: "u1234"}).roles == ()
