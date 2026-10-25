"""k8s/tools/runtime-doctor.sh 的约束闸。

这个脚本是给客户现场一键诊断 / 一键修复用的:跑在客户的 Windows Git Bash 上,
会改客户集群里的出网策略与配置。下面每一条都对应一个真实的坑或一条不能破的约束。
行为本身在本地集群上端到端验过(复刻客户现场的问题);前半钉住容易被改坏的写法,
后半加载脚本里的函数(DOCTOR_LIB=1),直接喂探测结果核对判读与诊断意见。
"""
import os
import pathlib
import re
import subprocess
import textwrap

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "k8s" / "tools" / "runtime-doctor.sh"


def _code_lines():
    """去掉注释行之后的脚本行(注释里会提到 python/jq 之类的字眼)。"""
    return [l for l in _SCRIPT.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")]


def test_ships_with_the_package_and_the_docs_point_to_it():
    """离线包只打 k8s/ 与 docs/:脚本必须在 k8s/ 下,手册里要有入口,客户才找得到。"""
    assert _SCRIPT.is_file()
    for name in ("从源码构建到K8s运行-完整手册.md",):
        assert "runtime-doctor.sh" in (_ROOT / "docs" / name).read_text(encoding="utf-8"), name


def test_needs_only_bash_and_kubectl():
    """客户的 Git Bash 里不一定有 python,也没有 jq。"""
    code = "\n".join(_code_lines())
    for tool in ("python", "jq ", "base64"):
        assert tool not in code, "脚本用到了 %s —— 客户的 Git Bash 里不一定有" % tool.strip()


def test_disables_git_bash_path_conversion():
    """Git Bash 会把参数里像路径的 /spec/egress/... 改写成 Windows 路径,补丁就打歪了。"""
    assert re.search(r"^export MSYS_NO_PATHCONV=1", _SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)


def test_egress_probe_waits_before_testing():
    """**第一版踩过。** 出网策略对新 Pod 要过一会儿才生效(OrbStack 实测约 1 秒):
    探测抢在生效前跑,受约束的 Pod 也连得上,被挡的模型就误报成「通」。"""
    text = _SCRIPT.read_text(encoding="utf-8")
    probe = re.search(r"^PROBE_SH='(.*?)'$", text, re.MULTILINE | re.DOTALL)
    assert probe, "找不到 PROBE_SH"
    body = probe.group(1)
    m = re.search(r"sleep (\d+)", body)
    assert m and body.index("sleep") < body.index("curl"), "探测前必须先等策略生效"
    assert int(m.group(1)) >= 5


def test_never_reads_the_trust_secret_value():
    """--login 只需要知道共享密钥配没配:数长度;真正登录时由 Pod 从 Secret 直接引用,脚本不经手。"""
    lines = [l for l in _code_lines() if "secret gateway-auth" in l]
    assert lines and all("wc -c" in l for l in lines), lines
    assert "secretKeyRef: {name: gateway-auth, key: TRUST_SECRET" in _SCRIPT.read_text(encoding="utf-8")


def test_never_prints_the_model_key():
    """读 Secret model-api 只为判断 key 填没填:只许数长度,不许解码或打印。"""
    lines = [l for l in _code_lines() if "secret model-api" in l]
    assert lines, "脚本不再检查 model-api?"
    for l in lines:
        assert "wc -c" in l, "读 model-api 的地方必须只数长度:" + l.strip()


def test_every_policy_patch_is_guarded_by_a_test_op():
    """改出网策略一律走 patch_rule:先 test 那条规则还是刚才看到的样子,再 replace。
    别人在这期间改过就拒绝,而不是按下标盲改、把别的规则覆盖掉。"""
    code = _code_lines()
    patch_calls = [l for l in code if "patch networkpolicy" in l]
    assert len(patch_calls) == 1, "patch networkpolicy 只应出现在 patch_rule 里:\n" + "\n".join(patch_calls)
    body = "\n".join(code)
    fn = body[body.index("patch_rule() {"):]
    fn = fn[:fn.index("\n}")]
    assert '\\"op\\":\\"test\\"' in fn and "patch networkpolicy" in fn


# ---- 行为:加载脚本里的函数(DOCTOR_LIB=1)直接喂探测结果 ---------------------------------------


def _bash(snippet, env=None, args=()):
    """在 bash 里加载脚本的函数再跑 snippet;返回 (退出码, stdout+stderr)。"""
    full = {k: v for k, v in os.environ.items() if k.lower() != "https_proxy"}   # 本机的代理别混进来
    full.update(DOCTOR_LIB="1", **(env or {}))
    cmd = ["bash", "-c", '. "$0"; ' + snippet, str(_SCRIPT)] + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, env=full, timeout=30)
    return p.returncode, p.stdout + p.stderr


def _run(args, path_dir=None):
    """直接执行脚本(不是加载函数);path_dir 放假的 kubectl / reg。"""
    env = dict(os.environ)
    env.pop("DOCTOR_LIB", None)
    if path_dir:
        env["PATH"] = "%s%s%s" % (path_dir, os.pathsep, env["PATH"])
    p = subprocess.run(["bash", str(_SCRIPT)] + list(args), capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout + p.stderr


@pytest.mark.parametrize("rc,nc,want", [
    ("0", "1", "OK"),
    ("52", "1", "OK"),            # 连上了、对方没回内容:网络是通的
    ("28", "0", "TCP_TIMEOUT"),
    ("28", "1", "SLOW"),
    ("7", "0", "NOCONNECT"),      # 没拿到原因时只说 TCP 没连上,不再一律说「拒绝」
    ("6", "0", "DNS"),
    ("5", "0", "PROXY_DNS"),
    ("35", "1", "TLS"),
    ("60", "1", "CERT"),
    ("56", "1", "CUT"),
])
def test_classify_goes_by_whether_tcp_connected(rc, nc, want):
    """第二个参数是 curl 的 num_connects。不用 time_connect:curl 7.88 在 TLS 失败时把它报成 0。"""
    code, out = _bash('classify "%s" "%s"' % (rc, nc))
    assert code == 0 and out.strip() == want


# 现场照片的原样:受约束的 Pod 连接超时,不受约束的 Pod TCP 连上了但 TLS 握手失败
_RT_TIMEOUT = "MODEL 28 000 0 curl: (28) Connection timed out after 10002 milliseconds\nVIA "
_FREE_TLS = ("MODEL 35 000 1 curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL "
             "in connection to api.deepseek.com:443\nVIA ")
_FREE_TIMEOUT = "MODEL 28 000 0 curl: (28) Connection timed out after 10001 milliseconds\nVIA "


def _verdict(rt, free, env=None):
    snippet = textwrap.dedent('''
        U=u0001; LP=${LP:-}; LP_SRC=${LP_SRC:-}; LP_PAC=""; MODEL_BLOCKED=0; GRMP_BLOCKED=0
        probe_verdict "模型服务" MODEL "$RT" "$FREE" "https://api.deepseek.com"
        echo "BLOCKED=$MODEL_BLOCKED"
        printf '%s' "$ADV"
    ''')
    return _bash(snippet, env=dict({"RT": rt, "FREE": free}, **(env or {})))[1]


def test_probe_counts_connects_not_connect_time():
    text = _SCRIPT.read_text(encoding="utf-8")
    body = re.search(r"^PROBE_SH='(.*?)'$", text, re.MULTILINE | re.DOTALL).group(1)
    assert "%{num_connects}" in body and "time_connect" not in body


def test_policy_block_is_found_even_when_tls_also_fails():
    """**现场踩过。** 第一版把「连接超时」和「TLS 握手失败」都算连不上,结论成了「集群本身不通」;
    其实不受约束的 Pod TCP 连上了 —— 是出网策略挡的,TLS 是放开之后的第二个问题,两条都要报。"""
    out = _verdict(_RT_TIMEOUT, _FREE_TLS)
    assert "BLOCKED=1" in out
    assert "被出网策略挡了" in out and "集群本身连不上" not in out
    assert "放开出网策略之后还会遇到" in out and "SSL_ERROR_SYSCALL" in out, "TLS 失败要原样带上 curl 的报错"
    assert "可以试:bash " in out and "u0001 --fix" in out


def test_both_time_out_means_the_cluster_cannot_reach_it():
    out = _verdict(_RT_TIMEOUT, _FREE_TIMEOUT)
    assert "BLOCKED=0" in out and "集群本身连不上" in out
    assert "--proxy" in out, "集群出不去时给经代理的修复命令"


def test_tls_failure_in_the_user_pod_is_not_blamed_on_the_policy():
    out = _verdict(_FREE_TLS, _FREE_TLS)
    assert "BLOCKED=0" in out and "被出网策略挡了" not in out
    assert "TLS 握手失败" in out and "代理" in out


def test_401_without_key_counts_as_reachable():
    out = _verdict("MODEL 0 401 1 \nVIA ", "MODEL 0 401 1 \nVIA ")
    assert "[正常]" in out and "没带 key" in out and "BLOCKED=0" in out


@pytest.mark.parametrize("lp,want", [
    ("10.1.2.9:8080", "u0001 --fix --proxy 10.1.2.9:8080"),
    ("127.0.0.1:7890", "连不到它"),
    ("proxy.corp.example:8080", "nslookup proxy.corp.example"),
])
def test_proxy_command_follows_this_machines_proxy(lp, want):
    out = _verdict(_RT_TIMEOUT, _FREE_TIMEOUT, env={"LP": lp, "LP_SRC": "Windows 系统代理"})
    assert want in out


def _fake_bin(tmp_path, name, body):
    f = tmp_path / name
    f.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    f.chmod(0o755)
    return tmp_path


def test_reads_the_windows_system_proxy(tmp_path):
    """Git Bash 里没有 HTTPS_PROXY 时,读 Windows 注册表里的系统代理(分协议写法取 https 的那个)。"""
    _fake_bin(tmp_path, "reg", textwrap.dedent(r'''
        case "$*" in
          *ProxyEnable*) printf '\r\nHKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings\r\n    ProxyEnable    REG_DWORD    0x1\r\n\r\n' ;;
          *ProxyServer*) printf '\r\nHKEY_CURRENT_USER\\...\r\n    ProxyServer    REG_SZ    http=10.1.2.3:8080;https=10.1.2.4:8443\r\n\r\n' ;;
          *) exit 1 ;;
        esac
    '''))
    code, out = _bash('detect_local_proxy; echo "LP=$LP SRC=$LP_SRC"', env={"PATH": "%s%s%s" % (tmp_path, os.pathsep, os.environ["PATH"])})
    assert "LP=10.1.2.4:8443 SRC=Windows 系统代理" in out


def test_reads_the_proxy_env_var():
    code, out = _bash('detect_local_proxy; echo "LP=$LP"', env={"HTTPS_PROXY": "http://me:pw@10.1.2.9:3128/"})
    assert "LP=10.1.2.9:3128" in out, "账号密码与协议头要去掉,只留 IP:端口"


def test_advice_commands_are_one_per_line():
    code, out = _bash('advise "问题甲" "意见甲" "cmd one" "" "cmd two"; advise "问题乙" "意见乙"; printf "%s" "$ADV"')
    assert out.splitlines() == ["1. 问题甲", "   意见:意见甲", "   可以试:cmd one", "          cmd two",
                                "2. 问题乙", "   意见:意见乙"]


# ---- 用法错误:现场敲成 ./runtime-doctor.sh bash u0001 ------------------------------------------

def test_two_user_ids_are_refused_instead_of_taking_the_last():
    code, out = _run(["bash", "u0001"])
    assert code == 2 and "只能给一个工号" in out and "「bash」「u0001」" in out


@pytest.mark.parametrize("args,want", [
    (["u0001", "--proxy", "10.1.2.9:8080"], "要和 --fix 一起用"),
    (["u0001", "--fix", "--proxy", "proxy.corp:8080"], "要写成 IP:端口"),
    (["u0001", "--fix", "--grmp", "localhost:8080"], "要写成 IP:端口"),
])
def test_bad_options_are_refused(args, want):
    code, out = _run(args)
    assert code == 2 and want in out


def test_unknown_user_hints_at_a_typo(tmp_path):
    """没开通、也没登录过的工号:不往下诊断一堆「问题」,直接提示参数是不是写错了。"""
    _fake_bin(tmp_path, "kubectl", 'case "$*" in *"configmap agent-config"*) exit 0 ;; *) exit 1 ;; esac\n')
    code, out = _run(["bash"], path_dir=tmp_path)
    assert code == 2 and "参数写错了" in out and "grmp-token-bash" in out



# ---- v1.2.0:地址校验、退出码 7 的原因、配了代理时不绕圈子、NO_PROXY、初始化容器、--login --------------

@pytest.mark.parametrize("args,want", [
    (["u0001", "--fix", "--grmp", "127.0.0.1:8080"], "回环"),       # 2026-10 现场真这么填过
    (["u0001", "--fix", "--proxy", "127.0.0.1:7890"], "回环"),
    (["u0001", "--fix", "--proxy", "0.0.0.0:8080"], "不是一个能连的地址"),
    (["u0001", "--fix", "--proxy", "10.1.2.300:8080"], "255"),
    (["u0001", "--fix", "--grmp", "10.1.2.3:0"], "1–65535"),
    (["u0001", "--fix", "--grmp", "10.1.2.3:70000"], "1–65535"),
    (["u0001", "--model-cidr", "10.0.0.0/40"], "地址段"),
    (["u0001", "--login", "--fix"], "分开跑"),
])
def test_unusable_addresses_are_refused_up_front(args, want):
    code, out = _run(args)
    assert code == 2 and want in out, out


@pytest.mark.parametrize("why,want", [("refused", "REFUSED"), ("noroute", "UNREACHABLE"),
                                      ("unreachable", "UNREACHABLE"), ("timeout", "TCP_TIMEOUT"), ("-", "NOCONNECT")])
def test_exit_7_is_told_apart_by_the_reason_curl_gave(why, want):
    """**2026-10 现场:** 「Failed to connect to 172.20.10.5 port 8080 after 5384 ms」被报成「连接被拒绝」,
    让人去查代理服务开没开;其实请求根本没出去。原因只在 curl -v 里,探测把它摘成一个词。"""
    code, out = _bash('classify 7 0 "%s"' % why)
    assert out.strip() == want


def test_probe_reports_the_reason_but_never_the_verbose_dump():
    body = re.search(r"^PROBE_SH='(.*?)'$", _SCRIPT.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL).group(1)
    assert " -v " in body and "_WHY" in body
    assert "cat /tmp/v" not in body and "</tmp/v" not in body, "-v 原文里有代理认证头,不能整段打印"


_PROXY = "VIA http://172.20.10.5:8080"
_NOROUTE = ("MODEL 7 000 0 curl: (7) Failed to connect to 172.20.10.5 port 8080 after 5384 ms: Couldn't connect to server\n"
            "MODEL_WHY noroute\n" + _PROXY)


def test_both_pods_unable_to_reach_a_configured_proxy_is_not_sent_round_in_circles():
    """**2026-10 现场的形态(地址已换成样例)。** 已经 --fix --proxy 配了代理,两个 Pod 都连不上代理。
    以前的意见又让执行一次 --fix --proxy(配的正是这个代理),绕回原地。现在要说「集群连不上代理本身」,
    并指出代理 IP 落在 Docker / kind 常用的网段里。"""
    out = _verdict(_NOROUTE, _NOROUTE)
    assert "集群连不上代理 172.20.10.5:8080" in out and "网络不可达" in out
    assert "172.16.0.0/12" in out and "docker network inspect" in out
    assert "--fix --proxy" not in out, "不能再让人配一遍同一个代理"


def test_a_different_local_proxy_is_offered_when_there_is_one():
    out = _verdict(_NOROUTE, _NOROUTE, env={"LP": "10.9.9.9:3128", "LP_SRC": "Windows 系统代理"})
    assert "u0001 --fix --proxy 10.9.9.9:3128" in out and "不同" in out


def test_policy_blocking_the_proxy_points_at_the_proxy_rule_not_plain_fix():
    rt = "MODEL 7 000 0 curl: (7) Failed to connect\nMODEL_WHY refused\n" + _PROXY
    free = "MODEL 0 401 1 \nMODEL_WHY -\n" + _PROXY
    out = _verdict(rt, free)
    assert "BLOCKED=1" in out and "出网策略不放行代理 172.20.10.5:8080" in out
    assert "u0001 --fix --proxy 172.20.10.5:8080" in out


@pytest.mark.parametrize("host,want", [
    ("10.1.2.3", "localhost,127.0.0.1,.svc,.cluster.local,10.1.2.3"),
    ("127.0.0.1", "localhost,127.0.0.1,.svc,.cluster.local"),        # 以前会出现两个 127.0.0.1
    ("grmp.corp.example", "localhost,127.0.0.1,.svc,.cluster.local"),
])
def test_no_proxy_has_no_duplicates_and_no_loopback_middleware(host, want):
    assert _bash('no_proxy_for "%s"' % host)[1].strip() == want


def test_failed_prepare_home_is_reported_with_its_own_message():
    """初始化容器失败时主容器没启动:只看 containerStatuses 会显示成「运行中、重启 0 次」。"""
    snippet = textwrap.dedent('''
        U=u0001; D=runtime-u0001; ADV=""; ADV_N=0; NEED_DIR=0
        G() { echo "prepare-home|1||||个人目录 users/u0001 不可写(属主 0:0)"; }
        init_status; echo "RC=$? NEED_DIR=$NEED_DIR"; printf '%s' "$ADV"
        G() { echo "prepare-home|0||||"; }
        init_status; echo "RC2=$?"
    ''')
    out = _bash(snippet)[1]
    assert "卡在初始化" in out and "users/u0001 不可写" in out and "RC=0 NEED_DIR=1" in out
    assert "u0001 --fix" in out and "RC2=1" in out


@pytest.mark.parametrize("args,want", [
    (("200", "1000", "1000", "1", "1", ""), "D:"),
    (("200", "800", "1000", "1", "1", ""), "A:"),               # 活动时间比登录还早 90 秒以上
    (("200", "1000", "1000", "0", "0", ""), "B:"),
    (("200", "1000", "1000", "1", "0", ""), "C:"),
    (("403", "0", "1000", "0", "0", ""), "403"),
    (("503", "0", "1000", "0", "0", "您的环境尚未开通"), "没开通"),
])
def test_login_verdicts(args, want):
    snippet = ('U=u0001; D=runtime-u0001; ADV=""; ADV_N=0; runtime_status() { echo RS-CALLED; }; '
               'login_verdict "$@"; printf "%s" "$ADV"')
    out = _bash(snippet, args=args)[1]
    assert want in out, out
    if want == "C:":
        assert "RS-CALLED" in out, "结论 C 要自动打印容器状态、日志与事件"


def test_login_failures_that_are_about_the_container_show_its_status():
    snippet = 'U=u0001; D=runtime-u0001; ADV=""; ADV_N=0; runtime_status() { echo RS-CALLED; }; login_verdict "$@"'
    for args in (("503", "0", "1", "0", "0", "您的环境启动失败(拉不到镜像)"), ("504", "0", "1", "0", "0", "")):
        assert "RS-CALLED" in _bash(snippet, args=args)[1]



@pytest.mark.parametrize("line,want", [
    ("DIR users/u0001 1000 no 555", "权限是 555"),        # 属主没错、是权限不让写:别说成「属主不对」
    ("DIR users/u0001 0 no 755", "属主是 uid 0(权限 755)"),
    ("DIR users/u0001 1000 yes 775", "可写"),
])
def test_nas_check_says_whether_it_is_the_owner_or_the_mode(line, want):
    snippet = textwrap.dedent('''
        U=u0001; IMG=x; dirs=users/u0001; p_nas=doctor-nas-t; ADV=""; ADV_N=0; NEED_DIR=0; CMD="bash runtime-doctor.sh"
        wait_pod() { printf '%s\\n' "$LINE"; }
        check_nas
    ''')
    out = _bash(snippet, env={"LINE": line})[1]
    assert want in out, out


def test_fix_is_verified_as_uid_1000_not_by_the_owner_root_sees():
    """**2026-10-16 测试环境抓到。** 有的存储按访问者映射属主:root 的修复 Pod 看到的属主永远是 0。
    以前按属主判成「改不了属主」,修好了也不重启用户容器,它停在旧的失败上。"""
    text = _SCRIPT.read_text(encoding="utf-8")
    fix = text[text.index('if [ "$NEED_DIR" = 1 ] && [ -n "$IMG" ]; then'):]
    fix = fix[:fix.index("\n  fi\n")]
    assert 'start_pod "$pv" 1000' in fix and "$NAS_SH" in fix, "修完要以 uid 1000 再查一遍"
    assert "$3 != 1000" not in fix


def test_fallback_image_carries_the_registry_prefix():
    """**2026-10-25 照《从源码构建到 K8s 运行》在全新 kind 集群上验证时发现。** 工号还没登录过、集群里一个用户
    Deployment 都没有时,临时 Pod 的镜像按 gateway-config 拼;以前只拼了 USER_IMAGE_TAG,镜像在内网仓库的部署里
    临时 Pod 全部 ImagePullBackOff,② 报成「NAS 挂不上」、④ 出网不测 —— 而网关建用户 Pod 时是带仓库前缀的。"""
    text = _SCRIPT.read_text(encoding="utf-8")
    start = text.index('IMG=$(G deploy "$D"')
    fb = text[text.index('if [ -z "$IMG" ]; then', start):]
    fb = fb[:fb.index("\nfi\n")]
    assert "USER_IMAGE_REGISTRY" in fb and "USER_IMAGE_TAG" in fb
    snippet = textwrap.dedent('''
        G() { case "$*" in *USER_IMAGE_TAG*) echo t1 ;; *USER_IMAGE_REGISTRY*) echo "$REG" ;; esac; }
        IMG=""
    ''') + fb + '\nfi\necho "IMG=$IMG"\n'
    assert _bash(snippet, env={"REG": "registry.example/gaussdb-agent/"})[1].strip() == \
        "IMG=registry.example/gaussdb-agent/gaussdb-agent-runtime:t1"
    assert _bash(snippet, env={"REG": ""})[1].strip() == "IMG=gaussdb-agent-runtime:t1"


@pytest.mark.parametrize("prefix,want", [
    ("/example/grmp/diagnostic/agent", "[正常] 中间件接口路径前缀 /example/grmp/diagnostic/agent"),
    ("", "GRMP_PATH_PREFIX 没填或格式不对"),
    ("grmp/diagnostic", "GRMP_PATH_PREFIX 没填或格式不对"),
])
def test_config_check_reports_the_grmp_path_prefix(prefix, want):
    """中间件接口路径前缀是部署配置(代码里不写死):① 要查它配了没有,没配给出 patch 命令。"""
    snippet = textwrap.dedent('''
        U=u0001; D=runtime-u0001; KN="kubectl -n gaussdb-agent"; CMD="bash runtime-doctor.sh"; ADV=""; ADV_N=0
        G() { case "$*" in
          *MODEL_BASE_URL*) echo https://m.example.com/v1 ;; *MODEL_ID*) echo m1 ;;
          *GRMP_API_HOST*) echo 10.1.1.1 ;; *GRMP_API_PORT*) echo 8080 ;; *GRMP_PATH_PREFIX*) echo "$PRE" ;;
          *MODEL_API_KEY*) echo a2V5a2V5 ;; *"secret grmp-token-"*) echo secret/grmp-token-u0001 ;; *) echo "" ;;
        esac; }
        check_config
        printf '%s\\n' "$ADV"
    ''')
    out = _bash(snippet, env={"PRE": prefix})[1]
    assert want in out, out
    if not prefix.startswith("/"):
        assert "GRMP_PATH_PREFIX" in out.split("== ① 配置与开通 ==")[-1] and "patch configmap agent-config" in out, out
