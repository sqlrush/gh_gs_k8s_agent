"""前端镜像的 nginx 配置守卫。起真 nginx 太重,对 conf 文本钉住几条不能回退的约束:

· 只服务 /dash/,别的路径 404 —— 这个容器只端大盘页面,不该顺手端出别的东西;
· /dash/health 这类无斜杠路径要 try_files 回落到 index.html(shell.js 按 pathname 切页);
· /dash 无斜杠要 301 到 /dash/(否则相对解析全错);
· no-cache:大盘改版要立刻生效;
· autoindex off:目录列表关掉;
· /healthz 给探针;
· 监听 8080:nginx-unprivileged 镜像非 root,绑不了 80。
"""
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CONF = (_ROOT / "docker" / "nginx-dash.conf").read_text(encoding="utf-8")
_DOCKERFILE = (_ROOT / "docker" / "Dockerfile.frontend").read_text(encoding="utf-8")


def test_listens_on_8080_as_unprivileged_image_requires():
    assert re.search(r"listen\s+8080\s*;", _CONF)
    assert re.search(r"^FROM\s+nginxinc/nginx-unprivileged:\d[\w.\-]*", _DOCKERFILE, re.M), "基础镜像要钉明确标签,不能 latest"


def test_only_dash_is_served_and_root_is_404():
    assert re.search(r"location\s+/dash/\s*\{", _CONF)
    assert re.search(r"location\s+/\s*\{[^}]*return\s+404", _CONF, re.S), "非 /dash/ 路径要 404"


def test_spa_fallback_and_slashless_redirect():
    assert re.search(r"try_files\s+\$uri\s+\$uri/\s+/dash/index\.html", _CONF), "/dash/health 要回落到 index.html"
    assert re.search(r"location\s*=\s*/dash\s*\{[^}]*return\s+301\s+/dash/", _CONF, re.S), "/dash 要 301 到 /dash/"


def test_no_cache_no_autoindex_and_healthz():
    assert re.search(r"add_header\s+Cache-Control\s+\"?no-cache", _CONF)
    assert re.search(r"autoindex\s+off", _CONF)
    assert re.search(r"location\s*=\s*/healthz\s*\{[^}]*return\s+200", _CONF, re.S)


def test_dockerfile_copies_frontend_and_conf_only():
    assert "COPY frontend/ /usr/share/nginx/html/dash/" in _DOCKERFILE
    assert "COPY docker/nginx-dash.conf /etc/nginx/conf.d/default.conf" in _DOCKERFILE
    assert "COPY docker/nginx-main.conf /etc/nginx/nginx.conf" in _DOCKERFILE, "主配置要用我方的(固定进程数)"
    assert "agent/" not in _DOCKERFILE and "gateway/" not in _DOCKERFILE, "前端镜像里不该有技能或网关代码"


def test_every_frontend_file_is_world_readable():
    """**2026-10-15 抓到。** `COPY frontend/` 原样带上工作树里的文件权限;nginx-unprivileged 以 uid 101 运行,
    哪个文件是 0600 它就回 403 —— 当时是 styles/press.css,文艺风格整页没有样式。git 只记 644,
    所以从干净克隆构建没事,从改过的工作树构建就出事。这里在构建前把关。"""
    import stat
    bad = [str(p.relative_to(_ROOT)) for p in (_ROOT / "frontend").rglob("*")
           if p.is_file() and not p.stat().st_mode & stat.S_IROTH]
    assert not bad, "这些文件 others 不可读,进镜像后 nginx 会回 403:%s(chmod 644 即可)" % bad


_MAIN = (_ROOT / "docker" / "nginx-main.conf").read_text(encoding="utf-8")


def test_main_conf_pins_a_small_worker_count():
    """**2026-11-17 鲲鹏抓到。** 基础镜像自带的主配置是 `worker_processes auto`:按节点 CPU 数起进程,
    160 核 + 64K 页内核下起 160 个,超出容器 128Mi 上限被 OOM 杀,大盘 502。固定成小值,与节点核数无关。"""
    m = re.search(r"^\s*worker_processes\s+(\S+)\s*;", _MAIN, re.M)
    assert m and m.group(1).isdigit() and 1 <= int(m.group(1)) <= 4, "worker_processes 要固定成 1–4,不能 auto"


def test_main_conf_keeps_what_the_unprivileged_image_needs():
    """只改进程数,其余照基础镜像:非 root 写不了 /var/run,pid 与临时目录都在 /tmp(根文件系统只读);
    server 段仍从 conf.d 引入。"""
    assert re.search(r"^\s*pid\s+/tmp/nginx\.pid\s*;", _MAIN, re.M)
    for d in ("proxy", "client_body", "fastcgi", "uwsgi", "scgi"):
        assert re.search(r"%s_temp_path\s+/tmp/" % d, _MAIN), "缺 %s_temp_path(只读根文件系统下写不了默认目录)" % d
    assert re.search(r"include\s+/etc/nginx/conf\.d/\*\.conf\s*;", _MAIN)
    assert re.search(r"include\s+/etc/nginx/mime\.types\s*;", _MAIN)
