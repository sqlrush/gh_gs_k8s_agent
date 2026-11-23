"""docker/Dockerfile.e2b 守卫(2026-11-17)。

平台的模板构建服务解层时不处理 OCI whiteout(实测:上层删掉的文件全回来、`.wh.*` 标记留在根文件系统里),
所以两个模板目标必须压成一层:`FROM scratch` + `COPY --from=<准备阶段> / /`,之后不再有 RUN / COPY / ADD。
压层丢掉的镜像配置要照 docker/Dockerfile 原样补回:ENV(键与值)、USER、WORKDIR、ENTRYPOINT。
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
PAIRS = (("runtime-e2b", "runtime-prep", "runtime"), ("kbimport-e2b", "kbimport-prep", "kb-import"))


def test_both_templates_carry_the_official_docsets():
    for prep in ("runtime-prep", "kbimport-prep"):
        assert ("COPY", "--from=docsets /out/kb/docsets /data/kb/docsets") in E2B[prep][1], \
            "%s:状态服务不给两种环境下发 docsets,模板里要自带" % prep


def test_both_template_targets_are_flattened_to_a_single_layer():
    for target, prep, _src in PAIRS:
        base, instrs = E2B[target]
        assert base == "scratch", "%s 要从 scratch 起(压成一层)" % target
        assert instrs[0] == ("COPY", "--from=%s / /" % prep), target
        assert not [op for op, _a in instrs[1:] if op in ("RUN", "COPY", "ADD")], \
            "%s 压层之后不能再加文件层(删除会重新产生 whiteout)" % target


def test_the_flattened_targets_restore_the_image_config_of_docker_dockerfile():
    for target, _prep, src in PAIRS:
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
