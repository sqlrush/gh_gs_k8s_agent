"""entrypoint / podctl 用到的东西必须真的在镜像里。

2026-09-21 踩到:新增 docker/statesync.py,Dockerfile 的 COPY 只列了 podctl.py 与
entrypoint.sh,结果 Pod 起来就 `MododuleNotFoundError: No module named 'statesync'`,
探针 120 秒不过。**单测全绿,镜像却起不来** —— 因为没有一条测试看过 Dockerfile。

这条闸很便宜:凡是 docker/ 下被 podctl 或 entrypoint 引用到的 .py,都要出现在 COPY 行里。
"""
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOCKERFILE = _ROOT / "docker" / "Dockerfile"
_PODCTL = _ROOT / "docker" / "podctl.py"
_ENTRY = _ROOT / "docker" / "entrypoint.sh"


def _copied_files() -> set:
    """Dockerfile 里 COPY 进 /opt/agent/ 的文件名。"""
    out = set()
    for m in re.finditer(r"^COPY\s+(.+?)\s+/opt/agent/\s*$", _DOCKERFILE.read_text(encoding="utf-8"),
                         re.MULTILINE):
        for token in m.group(1).split():
            out.add(pathlib.PurePosixPath(token).name)
    return out


def test_podctl_and_entrypoint_are_copied():
    assert {"podctl.py", "entrypoint.sh"} <= _copied_files()


def test_every_local_module_podctl_imports_is_copied():
    """podctl 里 `import X` 且 docker/X.py 存在 → X.py 必须也被 COPY 进镜像。"""
    src = _PODCTL.read_text(encoding="utf-8")
    copied = _copied_files()
    missing = []
    for name in set(re.findall(r"^\s*import\s+([a-z_][a-z0-9_]*)\s*$", src, re.MULTILINE)):
        if (_ROOT / "docker" / ("%s.py" % name)).is_file() and ("%s.py" % name) not in copied:
            missing.append(name)
    assert not missing, "podctl 会 import 但镜像里没有:%s —— Pod 一起来就 ModuleNotFoundError" % missing


def _registered_subcommands() -> set:
    """podctl 里注册过的子命令:直接 add_parser("x") 的,和 for name in (...) 循环注册的。"""
    src = _PODCTL.read_text(encoding="utf-8")
    names = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', src))
    for group in re.findall(r"for name in \(([^)]*)\):", src):
        names |= {t.strip().strip('"').strip("'") for t in group.split(",") if t.strip()}
    return names


def test_entrypoint_only_calls_podctl_subcommands_that_exist():
    """entrypoint 调的每个 `$PODCTL <子命令>` 都要注册过。

    拼错一个子命令,表现是 Pod 启动到那一行就退出(argparse 退 2 而 set -e 直接终止),
    日志里只有一句 usage —— 看着像参数问题,其实是名字打错了。
    """
    used = set(re.findall(r"\$PODCTL\s+([a-z][a-z-]*)", _ENTRY.read_text(encoding="utf-8")))
    unknown = sorted(used - _registered_subcommands())
    assert not unknown, "entrypoint 调了 podctl 没注册的子命令:%s" % unknown


def test_the_new_state_subcommands_are_wired():
    """状态策略的三个动作都要能从命令行调到 —— 少一个 entrypoint 就断在那一步。"""
    assert {"state-load", "state-sync", "state-finish"} <= _registered_subcommands()


def test_every_local_module_any_copied_module_imports_is_copied():
    """不只 podctl:被拷进镜像的每个 .py,它 import 的本地模块也要在 COPY 里(传递闭包)。

    2026-10-14 statesync 开始 import fdtree——上面那条只看 podctl 的 import,看不到这一层;
    漏拷的后果与 09-21 一模一样:Pod 一起来就 ModuleNotFoundError,单测全绿。
    """
    copied = _copied_files()
    missing = []
    for fname in sorted(f for f in copied if f.endswith(".py")):
        src = (_ROOT / "docker" / fname).read_text(encoding="utf-8")
        for name in set(re.findall(r"^\s*(?:import|from)\s+([a-z_][a-z0-9_]*)\b", src, re.MULTILINE)):
            if (_ROOT / "docker" / ("%s.py" % name)).is_file() and ("%s.py" % name) not in copied:
                missing.append("%s → %s.py" % (fname, name))
    assert not missing, "镜像里缺本地模块:%s" % missing


def test_the_pool_subcommands_are_wired_and_their_modules_copied():
    assert {"pool-slot", "pool-steward"} <= _registered_subcommands()
    assert not {"pool-serve", "pool-run"} & _registered_subcommands(), "v1.2.0 池化(平台管理的沙箱)已删"
    assert {"poolrun.py", "poolsync.py", "fdtree.py", "slotrun.py", "poolslot.py", "poolsteward.py",
            "poolauth.py", "netcheck.py"} <= _copied_files()


def test_slot_users_exist_in_the_image():
    """固定池化的槽位以 uid 2000–2015 运行,与 agent 同组;没有 passwd 条目时 git 等工具报不认识的用户。"""
    src = _DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"for i in \$\(seq 0 15\); do useradd --uid \$\(\(2000 \+ i\)\) --gid agent", src)


def _stages() -> dict:
    """Dockerfile 每个 FROM … AS <名> 段落的正文。"""
    out, name = {}, None
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^FROM\s+\S+\s+AS\s+(\S+)", line)
        if m:
            name = m.group(1)
            out[name] = []
        elif name:
            out[name].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _code(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_image_decoders_go_into_kb_import_only():
    """2026-10-22:HEIC 照片要转成 JPEG 才能给模型看(common/kb/imgconv.py),pillow / pillow-heif 只装在导入镜像。

    诊断镜像不看图;base 里装了,runtime 就会跟着多出两套带原生库的包(扫描面变大,还没人用)。
    装进 kb-import 之后要当场 import 一次:轮子装错架构或缺库时构建就失败,而不是等管理员传了照片才发现。
    """
    st = _stages()
    assert {"base", "imgdeps", "runtime", "kb-import"} <= set(st)
    for name in ("base", "runtime"):
        assert not re.search(r"pillow|imgdeps", _code(st[name]), re.IGNORECASE), "%s 段不该装看图依赖" % name
    assert re.search(r'"pillow==[\d.]+"', st["imgdeps"]) and re.search(r'"pillow-heif==[\d.]+"', st["imgdeps"])
    assert "--only-binary=:all:" in st["imgdeps"], "内网构建机未必有编译器,只用现成轮子"
    ki = _code(st["kb-import"])
    assert re.search(r"^COPY --from=imgdeps /opt/imgdeps/ /usr/local/lib/python3\.12/site-packages/$", ki, re.MULTILINE)
    assert "import PIL.Image, pillow_heif" in ki and "register_heif_opener" in ki


def test_image_decoder_site_packages_matches_the_base_python():
    """拷到的 site-packages 路径里写死了 python3.12:基础镜像升 Python 时这里必须一起改,否则 import 不到。"""
    assert re.search(r"^FROM python:3\.12-", _DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert re.search(r"^FROM python:3\.12-\S+ AS imgdeps$", _DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)


def test_sqlite_vec_is_in_both_images_with_the_pinned_version():
    """2026-10-23 知识库向量库改为 index/store.sqlite + sqlite-vec:runtime 查、kb-import 写,两个镜像都要有,
    且必须是 agent/requirements.txt 锁的那个版本(未到 1.0,版本间行为不保证一致)。

    装在 base 段,构建期当场 load 一次:Python 不许加载扩展或轮子架构不对时,构建就失败。
    """
    st = _stages()
    base = _code(st["base"])
    m = re.search(r'"sqlite-vec==([\d.]+)"', base)
    assert m, "base 段要装 sqlite-vec(runtime 与 kb-import 都从 base 来)"
    req = (_ROOT / "agent" / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^sqlite-vec==%s\b" % re.escape(m.group(1)), req, re.MULTILINE), "镜像与 requirements.txt 的版本要一致"
    assert "sqlite_vec.load(c)" in base and "enable_load_extension(True)" in base
