"""deploy.sh 的端到端测试 —— 真跑脚本，不是 dry-run。

这是客户装完拿到手第一个执行的东西，270 行、5 步交互，此前只用
`--dry-run` 喂 `yes y` 跑过。dry-run 把每条会改动系统的命令都换成打印，
恰恰把所有真正会出问题的地方跳过了。

而 shell 在 `set -u` 下的典型坏法就是**没走到的分支才炸**：某个变量只在
另一条路上赋值，这条路上一引用就是 unbound。所以这里按「客户会怎么点」
把分支一条条走一遍，HOME / 安装目录 / $GSDB_HOME 全部重定向到临时目录。
"""
import os
import pathlib
import site
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DEPLOY = _ROOT / "deploy.sh"

# 测试要把 HOME 指到临时目录（否则会往真实的 ~/.zshrc 里写东西），但 Python
# 的用户级 site-packages 路径是从 HOME 推出来的 —— 一改 HOME，psycopg2/yaml
# 就找不到了，脚本第一步就判「缺少依赖」。把真实路径显式注回去。
_USER_SITE = site.getusersitepackages()


def _run(tmp, answers, *args, env_extra=None):
    """跑一次 deploy.sh，把交互答案按顺序喂进去。"""
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"HOME": str(home), "GSDB_HOME": str(tmp / "gdaa"),
                "TERM": "dumb", "NO_COLOR": "1",
                "PYTHONPATH": os.pathsep.join(
                    [p for p in (_USER_SITE, env.get("PYTHONPATH", "")) if p])})
    env.pop("GRMP_AUTH_TOKEN", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["bash", str(_DEPLOY), *args],
        input="\n".join(answers) + "\n",
        capture_output=True, text=True, cwd=str(_ROOT), env=env, timeout=300)
    # 未捕获异常一律是 BUG —— 客户看到的应该是能照做的提示，不是一段栈。
    assert "Traceback" not in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    assert "unbound variable" not in proc.stdout + proc.stderr, \
        "set -u 下引用了没赋值的变量：\n" + proc.stdout + proc.stderr
    return proc


def _gsql_answers(dest, ghome, rc, *, app="app1", conn="og-prod"):
    return [str(dest), str(ghome), "y", str(rc), "1",
            app, conn, "opengauss", "127.0.0.1", "5432", "postgres",
            "gaussdb", "psycopg2", ""]


def _api_answers(dest, ghome, rc, *, host="127.0.0.1", port="8769",
                 host_env="GRMP_API_HOST"):
    # 顺序必须与 deploy.sh 的提问顺序一字不差：多一问少一问都会让后面的答案
    # 整体错位（a57f37f 加了 host_env 那问,答案没跟上,port 就吃到了令牌变量名）。
    return [str(dest), str(ghome), "y", str(rc), "2",
            host, host_env, port, "GRMP_AUTH_TOKEN"]


@pytest.fixture()
def box(tmp_path):
    return {"tmp": tmp_path,
            "dest": tmp_path / "skills",
            "ghome": tmp_path / "gdaa",
            "rc": tmp_path / "home" / ".zshrc"}


# --- 取消路径：退出前不留半套东西 --------------------------------------------

def test_cancel_at_confirmation_writes_nothing(box):
    """确认那一步选 N，必须什么都没建。"""
    proc = _run(box["tmp"], [str(box["dest"]), str(box["ghome"]), "n"])
    assert proc.returncode == 0
    assert "已取消" in proc.stdout
    assert not box["dest"].exists()
    assert not box["ghome"].exists()


def test_dry_run_writes_nothing(box):
    _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"]),
         "--dry-run")
    assert not box["dest"].exists(), "dry-run 装了 skill"
    assert not (box["ghome"] / "config.yaml").exists(), "dry-run 写了配置"


# --- gsql 全流程 --------------------------------------------------------------

def test_gsql_flow_installs_and_writes_config(box):
    proc = _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"]))
    cfg = box["ghome"] / "config.yaml"
    assert cfg.exists(), proc.stdout
    text = cfg.read_text(encoding="utf-8")
    assert "connection_mode: gsql" in text
    assert "og-prod" in text
    # 期望值从源码树反推，不写死数字。install-opencode.sh 判断"要不要装某个
    # skill"用的就是 `[ -f "${d}SKILL.md" ] || continue`（第 202 行）—— 这里数
    # 的必须是同一个条件，否则测试测的是另一件事。写死数字的坏处不是麻烦，
    # 是每加一个 skill（如 gaussdb-waitevent、gaussdb-vacuum）都要有人判断
    # "数字变了是新增合理还是装漏了"，而顺手的答案永远是"改数字"——一个
    # 总被无脑放行的门禁等于没有门禁。别把这里改回字面量。
    expected = len(list(_ROOT.glob("skills/*/SKILL.md")))
    installed = len(list(box["dest"].glob("gaussdb-*")))
    assert installed == expected, (
        "skill 没装全：源码树里带 SKILL.md 的 skill 目录有 %d 个，实际装了 %d 个"
        % (expected, installed))


def test_generated_config_carries_no_plaintext_password(box):
    """**配置文件里绝不允许出现明文口令。**

    这是 v4 的硬约束，config.py 加载时会直接拒绝。部署脚本自己生成的配置
    要是带了 password，客户第一次调 skill 就会被自己的部署脚本坑到。
    """
    _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"]))
    text = (box["ghome"] / "config.yaml").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("password:"), "生成的配置带了明文口令：" + line


def test_generated_config_is_actually_loadable(box):
    """生成的配置要能被 config.py 真的加载 —— 缩进错一格就是「装完连不上」。"""
    _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"]))
    proc = subprocess.run(
        ["python3", "-c",
         "import sys; sys.path.insert(0, %r)\n"
         "from common import config\n"
         "print(config.mode()); print([c.qualified for c in config.load()])"
         % str(box["dest"])],
        capture_output=True, text=True,
        env={**os.environ, "GSDB_HOME": str(box["ghome"])})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "gsql" in proc.stdout and "og-prod" in proc.stdout


def test_config_file_is_chmod_600(box):
    _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"]))
    mode = oct((box["ghome"] / "config.yaml").stat().st_mode)[-3:]
    assert mode == "600", "config.yaml 权限是 %s" % mode


def test_gsdb_home_is_chmod_700(box):
    _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"]))
    assert oct(box["ghome"].stat().st_mode)[-3:] == "700"


def test_missing_credential_skips_connectivity_instead_of_reporting_red(box):
    """口令没设成时不要跑连通性测试。

    跑出来的红既不是环境问题也不是代码问题，是「你还没设口令」——
    但报出来像是连不上，会把人指向错误的方向。
    """
    proc = _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"]))
    assert "跳过连通性测试" in proc.stdout, proc.stdout


# --- 环境变量写入 -------------------------------------------------------------

def test_env_var_written_once_and_not_duplicated(box):
    """同一个变量在 rc 里出现三遍，下次有人改错一处会花很久才发现。"""
    answers = _gsql_answers(box["dest"], box["ghome"], box["rc"])
    _run(box["tmp"], answers)
    assert box["rc"].read_text(encoding="utf-8").count("GSDB_HOME=") == 1

    # 第二次部署：配置已存在，选不覆盖
    again = [str(box["dest"]), str(box["ghome"]), "y", str(box["rc"]), "n"]
    proc = _run(box["tmp"], again)
    assert box["rc"].read_text(encoding="utf-8").count("GSDB_HOME=") == 1, \
        "重复部署把 GSDB_HOME 写了两遍"
    assert "已有 GSDB_HOME" in proc.stdout


# --- api 模式 -----------------------------------------------------------------

def test_api_flow_writes_api_config(box):
    _run(box["tmp"], _api_answers(box["dest"], box["ghome"], box["rc"]))
    text = (box["ghome"] / "config.yaml").read_text(encoding="utf-8")
    assert "connection_mode: api" in text
    assert "token_env: GRMP_AUTH_TOKEN" in text
    assert "token:" not in text.replace("token_env:", ""), "令牌不该落盘"


def test_api_missing_token_is_reported_as_failure(box):
    """**令牌没设必须判失败。**

    这里原先写成无论如何退出 0，于是令牌根本没设也显示 ✓ ——
    一个「前置条件没满足却报通过」的检查，比没有这个检查更糟。
    """
    proc = _run(box["tmp"], _api_answers(box["dest"], box["ghome"], box["rc"]))
    assert "项失败" in proc.stdout, proc.stdout
    assert "全部通过" not in proc.stdout


def test_api_token_present_passes(box):
    proc = _run(box["tmp"], _api_answers(box["dest"], box["ghome"], box["rc"]),
                env_extra={"GRMP_AUTH_TOKEN": "t0ken-for-test"})
    assert "全部通过" in proc.stdout, proc.stdout
    assert "t0ken-for-test" not in proc.stdout, "令牌被打印出来了"


def test_api_missing_path_prefix_is_reported_as_failure(box, monkeypatch):
    """中间件接口路径前缀是部署配置(GRMP_PATH_PREFIX),没配必须判失败并说清缺什么 —— 不拿猜的前缀去请求。"""
    monkeypatch.delenv("GRMP_PATH_PREFIX", raising=False)
    proc = _run(box["tmp"], _api_answers(box["dest"], box["ghome"], box["rc"]),
                env_extra={"GRMP_AUTH_TOKEN": "t0ken-for-test"})
    assert "项失败" in proc.stdout and "GRMP_PATH_PREFIX" in proc.stdout, proc.stdout
    assert "全部通过" not in proc.stdout


# --- 保留已有配置 -------------------------------------------------------------

def test_keeping_an_existing_api_config_still_tests_api(box):
    """**保留已有配置时，连通性测试要按配置文件里的模式走。**

    MODE_SEL 只在「重新生成配置」那条路上赋值。选了不覆盖，它就没有值，
    兜底成 gsql —— 于是保留着 api 配置的客户，会看到脚本拿一个编出来的
    连接名 og-prod 去直连数据库，报一堆指向错误方向的红。
    """
    _run(box["tmp"], _api_answers(box["dest"], box["ghome"], box["rc"]))
    assert "connection_mode: api" in \
        (box["ghome"] / "config.yaml").read_text(encoding="utf-8")

    proc = _run(box["tmp"],
                [str(box["dest"]), str(box["ghome"]), "y", str(box["rc"]), "n"])
    assert "保留原配置" in proc.stdout
    assert "登录并验证连接" not in proc.stdout, \
        "保留的是 api 配置，却跑了 gsql 的直连测试：\n" + proc.stdout
    assert "中间件端点已配置" in proc.stdout, \
        "保留 api 配置后没有验中间件端点：\n" + proc.stdout


def test_keeping_an_existing_gsql_config_backs_nothing_up_and_keeps_it(box):
    """选不覆盖就真的别动它 —— 连备份都不该产生（没改怎么会有备份）。"""
    _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"]))
    cfg = box["ghome"] / "config.yaml"
    before = cfg.read_text(encoding="utf-8")

    _run(box["tmp"],
         [str(box["dest"]), str(box["ghome"]), "y", str(box["rc"]), "n"])
    assert cfg.read_text(encoding="utf-8") == before, "选了不覆盖，配置却被改了"
    assert not list(box["ghome"].glob("config.yaml.bak.*")), \
        "没覆盖却产生了备份文件"


def test_keeping_a_gsql_config_uses_its_real_connection_name(box):
    """同一个坑的另一半：连接名叫 my-og，保留配置后却拿默认的 og-prod 去测，
    报的红是「连接不存在」而不是真实的连通性问题。"""
    _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"],
                                   app="myapp", conn="my-og"))
    proc = _run(box["tmp"],
                [str(box["dest"]), str(box["ghome"]), "y", str(box["rc"]), "n"])
    assert "myapp/my-og" in proc.stdout, \
        "保留配置后没有沿用真实的连接名：\n" + proc.stdout
    assert "og-prod" not in proc.stdout


def test_overwriting_an_existing_config_backs_it_up(box):
    _run(box["tmp"], _gsql_answers(box["dest"], box["ghome"], box["rc"],
                                   conn="first-conn"))
    answers = [str(box["dest"]), str(box["ghome"]), "y", str(box["rc"]), "y",
               "1", "app1", "second-conn", "opengauss", "127.0.0.1", "5432",
               "postgres", "gaussdb", "psycopg2", ""]
    _run(box["tmp"], answers)
    cfg = (box["ghome"] / "config.yaml").read_text(encoding="utf-8")
    assert "second-conn" in cfg
    baks = list(box["ghome"].glob("config.yaml.bak.*"))
    assert baks, "覆盖了却没备份旧配置"
    assert "first-conn" in baks[0].read_text(encoding="utf-8")
