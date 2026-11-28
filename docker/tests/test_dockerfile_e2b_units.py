"""docker/Dockerfile.e2b 守卫(2026-11-17)。

平台的模板构建服务解层时不处理 OCI whiteout(实测:上层删掉的文件全回来、`.wh.*` 标记留在根文件系统里),
所以两个模板目标必须压成一层:`FROM scratch` + `COPY --from=<准备阶段> / /`,之后不再有 RUN / COPY / ADD。
压层丢掉的镜像配置要照 docker/Dockerfile 原样补回:ENV(键与值)、USER、WORKDIR、ENTRYPOINT。
平台模板构建要现场 apt-get 的系统包(2026-11-27 现场不通外网,模板 error)在两个准备阶段预装好。
"""
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MAIN = (_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
_E2B = (_ROOT / "docker" / "Dockerfile.e2b").read_text(encoding="utf-8")
# python:3.12-slim 基础镜像自带、与运行无关的键(PATH 单独核对)
_BASE_IMAGE_ONLY = {"GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"}


def _stages(text):
    """阶段名 → (FROM 的对象, [(指令, 参数)]);续行合并,注释去掉。"""
    lines, buf = [], ""
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        lines.append(buf + s)
        buf = ""
    stages, cur = {}, None
    for ln in lines:
        op, _, arg = ln.partition(" ")
        op = op.upper()
        if op == "FROM":
            m = re.match(r"(\S+)(?:\s+AS\s+(\S+))?", arg, re.I)
            cur = m.group(2) or m.group(1)
            stages[cur] = (m.group(1), [])
        elif cur is not None:
            stages[cur][1].append((op, arg.strip()))
    return stages


def _env(instrs):
    out = {}
    for op, arg in instrs:
        if op == "ENV":
            out.update(dict(re.findall(r"(\w+)=(\S+)", arg)))
    return out


def _last(instrs, op):
    vals = [a for o, a in instrs if o == op]
    return vals[-1] if vals else None


def _chain(stages, name):
    """沿 FROM 往上把同一文件里的阶段串起来(base → runtime)。"""
    seq = []
    while name in stages:
        seq.insert(0, stages[name][1])
        name = stages[name][0]
    return [i for s in seq for i in s]


MAIN, E2B = _stages(_MAIN), _stages(_E2B)
# (压层目标, 压层来源阶段, 来源默认指向的准备阶段, docker/Dockerfile 里的镜像)
PAIRS = (("runtime-e2b", "runtime-src", "runtime-prep", "runtime"),
         ("kbimport-e2b", "kbimport-src", "kbimport-prep", "kb-import"))
_PREP_ARGS = {"runtime-src": "RUNTIME_PREP", "kbimport-src": "KBIMPORT_PREP"}


def test_both_templates_carry_the_official_docsets():
    for prep in ("runtime-prep", "kbimport-prep"):
        assert ("COPY", "--from=docsets /out/kb/docsets /data/kb/docsets") in E2B[prep][1], \
            "%s:状态服务不给两种环境下发 docsets,模板里要自带" % prep


def test_both_template_targets_are_flattened_to_a_single_layer():
    for target, prep, _default, _src in PAIRS:
        base, instrs = E2B[target]
        assert base == "scratch", "%s 要从 scratch 起(压成一层)" % target
        assert instrs[0] == ("COPY", "--from=%s / /" % prep), target
        assert not [op for op, _a in instrs[1:] if op in ("RUN", "COPY", "ADD")], \
            "%s 压层之后不能再加文件层(删除会重新产生 whiteout)" % target


def test_the_flattened_targets_restore_the_image_config_of_docker_dockerfile():
    for target, _prep, _default, src in PAIRS:
        want_all = _chain(MAIN, src)
        want, got = _env(want_all), _env(E2B[target][1])
        for k, v in want.items():
            assert got.get(k) == v, "%s 的 ENV %s 要与 docker/Dockerfile 一致" % (target, k)
        assert not set(got) - set(want) - {"PATH"}, "%s 多出了 docker/Dockerfile 里没有的 ENV" % target
        assert got["PATH"].startswith("/usr/local/bin:") and "/usr/bin" in got["PATH"]
        for op in ("USER", "WORKDIR", "ENTRYPOINT"):
            assert _last(E2B[target][1], op) == _last(want_all, op), "%s 的 %s" % (target, op)


def test_the_launcher_supplies_the_same_image_env_to_its_systemd_units():
    import sys
    sys.path.insert(0, str(_ROOT / "docker"))
    import fuyao
    base = {k: v for k, v in _env(_chain(MAIN, "runtime")).items() if k not in _BASE_IMAGE_ONLY}
    assert base == fuyao._IMAGE_ENV, "systemd 服务拿不到镜像 ENV,启动器补的那份要与 Dockerfile 一致"


# 平台模板构建(E2B provision.sh)逐个 dpkg-query 检查、缺了才联网装的包,按 E2B 版本(上游 e2b-dev/infra)
_PROVISION_PACKAGES = {
    "2025": "systemd systemd-sysv openssh-server sudo chrony linuxptp socat curl",
    "2026-01": "systemd systemd-sysv openssh-server sudo chrony linuxptp socat curl ca-certificates fuse3 iptables git",
    "current": "systemd systemd-sysv openssh-server sudo chrony socat curl ca-certificates fuse3 iptables git "
               "nfs-common less nftables iputils-ping jq iproute2",
}


def _preinstall_run(prep):
    runs = [a for op, a in E2B[prep][1] if op == "RUN" and "$E2B_BASE_PACKAGES" in a]
    assert len(runs) == 1, "%s 要有且只有一条预装系统包的 RUN" % prep
    return runs[0]


def test_the_package_list_covers_every_e2b_provisioning_profile():
    m = re.search(r'^ARG E2B_BASE_PACKAGES="([^"]+)"$', _E2B, re.M)
    assert m, "E2B_BASE_PACKAGES 要在第一个 FROM 之前声明默认值"
    have = set(m.group(1).split())
    for version, pkgs in _PROVISION_PACKAGES.items():
        missing = set(pkgs.split()) - have
        assert not missing, "E2B %s 版模板构建会联网装 %s:不通外网的现场模板构建失败" % (version, sorted(missing))


def test_both_preps_preinstall_the_same_packages_and_verify_them():
    runs = {}
    for prep in ("runtime-prep", "kbimport-prep"):
        instrs = E2B[prep][1]
        for name in ("E2B_BASE_PACKAGES", "MOUNTPOINT_BASE_URL"):
            assert ("ARG", name) in instrs, "%s 要重新声明 ARG %s,否则阶段里取不到全局默认值、什么也没装" % (prep, name)
        run = runs[prep] = _preinstall_run(prep)
        assert "apt-get install" in run and "--no-install-recommends $E2B_BASE_PACKAGES" in run, prep
        assert "mount-s3.deb" in run, "%s:新版平台还从 S3 下载 mount-s3" % prep
        assert "dpkg-query -W -f='${Status}'" in run and "install ok installed" in run, \
            "%s:要按平台的判断方式逐个核对,装不全就构建失败" % prep
        assert "rm -rf /var/lib/apt/lists/*" in run, prep
        order = [op for op, _a in instrs]
        assert order.index("RUN") < order.index("COPY"), "%s:预装放在文档集与占位文件之前" % prep
    assert runs["runtime-prep"] == runs["kbimport-prep"], "两个准备阶段的预装要逐字相同"


def test_the_flatten_source_defaults_to_the_prep_stage_and_can_be_overridden():
    """rootspec-overlay.sh 换根规范时把压层来源指向「已出的模板镜像 + 换规范的一层」,不从头重建(从头建要外网装包)。"""
    for _target, src, default, _img in PAIRS:
        arg = _PREP_ARGS[src]
        base, instrs = E2B[src]
        assert base == "${%s}" % arg, "%s 要 FROM ${%s}" % (src, arg)
        assert not instrs, "%s 只是转一手,不能自己加层" % src
        assert re.search(r"^ARG %s=%s$" % (arg, re.escape(default)), _E2B, re.M), \
            "%s 默认要指向 %s(正常构建照旧)" % (arg, default)


def test_rootspec_overlay_rebuilds_templates_on_the_shipped_template_images():
    sh = (_ROOT / "scripts" / "rootspec-overlay.sh").read_text(encoding="utf-8")
    for target, src, _default, _img in PAIRS:
        assert "%s:%s" % (target, _PREP_ARGS[src]) in sh, "rootspec-overlay.sh 要按 %s 指定 %s" % (target, _PREP_ARGS[src])
    assert '--build-arg "$prep=$SRC-rootspec-src"' in sh
    assert 'docker load -i "$ETAR"' in sh, "换规范要在包里已出的模板镜像上做"
    assert not [ln for ln in sh.splitlines() if "apt-get" in ln and not ln.lstrip().startswith("#")], "换规范不能联网装包"
