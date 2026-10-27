# syntax=docker/dockerfile:1.7
# 两个后端镜像出自同一文件:--target runtime / --target kb-import。
# 内网不联外:opencode、ripgrep、models.json 都在构建期取好。
ARG OPENCODE_VERSION=1.18.27

# ---------------------------------------------------------------- base
FROM python:3.12-slim-bookworm AS base
ARG OPENCODE_VERSION
ARG TARGETARCH
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai LANG=C.UTF-8
# upgrade:基础镜像里 libpcre2 / liblzma 等有 deb12u 安全更新,构建时一并拿到
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates curl ripgrep git tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --user-group --create-home --shell /bin/bash agent

# opencode 官方 release 包(glibc 版);包内是单个可执行文件
RUN set -eux; case "$TARGETARCH" in amd64) A=x64 ;; arm64) A=arm64 ;; *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; esac; \
    curl -fsSL -o /tmp/oc.tgz "https://github.com/sst/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${A}.tar.gz"; \
    mkdir -p /tmp/oc && tar xzf /tmp/oc.tgz -C /tmp/oc && install -m 0755 "$(find /tmp/oc -type f -name opencode | head -1)" /usr/local/bin/opencode; \
    rm -rf /tmp/oc /tmp/oc.tgz; opencode --version

# 缓存目录预置:模型清单与 ripgrep,运行时不再下载
RUN mkdir -p /opt/agent/cache/opencode/bin \
    && curl -fsSL -o /opt/agent/cache/opencode/models.json https://models.dev/api.json \
    && cp /usr/bin/rg /opt/agent/cache/opencode/bin/rg

# Python 依赖(agent/requirements.txt 那四项;psycopg2-binary 与 psycopg2 同模块名)
# sqlite-vec:知识库向量检索(2026-10-23 起索引是 NAS 上的 index/store.sqlite,原 openGauss 向量库下线)。
#   runtime 与 kb-import 都要:一个只读查,一个写索引。装完当场加载一次 —— 镜像里的 Python 不许加载扩展、
#   或轮子架构不对时构建就失败,而不是上线后知识库悄悄没了向量召回。
# 装完依赖就把 pip 连同 ensurepip 自带的 pip 轮子删掉:运行时用不到,而 trivy 报的 python 包漏洞全部来自
# pip 自带的 vendored msgpack / pkg_resources(setuptools),不是我们的依赖
RUN pip install --no-cache-dir "psycopg2-binary==2.9.10" "cryptography>=41" "PyYAML>=6" "sqlite-vec==0.1.9" \
    && pip uninstall -y pip && rm -rf /usr/local/lib/python3.12/ensurepip/_bundled \
    && python3 -c "import psycopg2, cryptography, yaml, sqlite3, sqlite_vec; c = sqlite3.connect(':memory:'); \
c.enable_load_extension(True); sqlite_vec.load(c); \
print('sqlite', sqlite3.sqlite_version, 'sqlite-vec', c.execute('select vec_version()').fetchone()[0])"

# 技能代码:用仓库自己的安装脚本装进 opencode 的技能目录(common/、scripts/registry/ 一起)
COPY agent/ /opt/agent/src/
RUN cd /opt/agent/src && bash install-opencode.sh --dest /opt/agent/config/opencode/skills --no-backup \
        --agents-md /opt/agent/config/opencode/AGENTS.md >/tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; } \
    && grep -E "installed" /tmp/install.log | tail -1 && rm -rf /opt/agent/src /tmp/install.log
COPY docker/podctl.py docker/statesync.py docker/fdtree.py docker/poolsync.py docker/poolrun.py docker/slotrun.py docker/poolslot.py docker/poolsteward.py docker/poolauth.py docker/netcheck.py docker/credproxy.py docker/clipd.py docker/kbcatalog.py docker/kboutline.py docker/entrypoint.sh /opt/agent/
# 权限按镜像统一:构建目录里的文件可能是 0600(编辑器 / sed -i 重写过),槽位 uid 2000+ 读不到就起不来
# (2026-10-25 测试环境:/opt/agent/kboutline.py 0600,报告端口反复崩、所有人的槽位 opencode_not_ready)
RUN chmod 0755 /opt/agent/entrypoint.sh && chown -R agent:agent /opt/agent && chmod -R a+rX /opt/agent \
    && mkdir -p /data /nas && chown agent:agent /data
# 固定池化的槽位以 uid 2000+i 运行(与管家 agent 同组):给它们 passwd 条目,git / getpwuid 才不报「不认识的用户」
RUN for i in $(seq 0 15); do useradd --uid $((2000 + i)) --gid agent --no-create-home --home-dir /data/home \
        --shell /usr/sbin/nologin "slot$i"; done

USER agent
WORKDIR /data
EXPOSE 4096
# 设了 OPENCODE_SERVER_PASSWORD 时 /global/health 也要基本认证,探针带上口令(k8s 模板里的 exec 探针同样写法)
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS ${OPENCODE_SERVER_PASSWORD:+-u "opencode:$OPENCODE_SERVER_PASSWORD"} http://127.0.0.1:4096/global/health || exit 1
ENTRYPOINT ["/opt/agent/entrypoint.sh"]

# ---------------------------------------------------------------- imgdeps:只给 kb-import 的看图依赖
# read 打不开 HEIC / TIFF(手机直出的照片多是 HEIC),kb-init scan 用 pillow + pillow-heif 转成 JPEG 副本给模型看。
# 单独一段装到 /opt/imgdeps 再拷进 kb-import:base 已经删了 pip,而 runtime 不看图、不该多出这些带原生库的包。
# --only-binary:两种架构都有 manylinux 轮子(libheif 已打包在 pillow-heif 轮子里),不需要编译器与 apt 包。
FROM python:3.12-slim-bookworm AS imgdeps
RUN pip install --no-cache-dir --only-binary=:all: --target /opt/imgdeps "pillow==12.3.0" "pillow-heif==1.8.1"

# ---------------------------------------------------------------- runtime:诊断 skill + 知识库查询
FROM base AS runtime
USER root
# 知识库的导入与入库前标准化只属于管理员镜像:runtime 里必须没有,否则模型会引导用户去跑写知识库的命令
RUN rm -rf /opt/agent/config/opencode/skills/gaussdb-kb-import \
           /opt/agent/config/opencode/skills/gaussdb-kb-init \
    && test "$(ls -d /opt/agent/config/opencode/skills/gaussdb-* | wc -l)" = "18"
USER agent
# image.source 不只是元数据:GHCR 靠它把镜像**关联到仓库**,关联之后镜像继承仓库的访问权限。
# 没有它,镜像是游离的用户级 package,只能靠单独发令牌来授权 —— 而那种令牌要么权限过宽,
# 要么要额外管一套轮换。关联之后,把人加成仓库只读协作者即可,按人授权、可审计、可单独回收。
LABEL org.opencontainers.image.title="gaussdb-agent-runtime" \
      org.opencontainers.image.source="https://github.com/sqlrush/gh_agent_k8s" \
      org.opencontainers.image.description="opencode + 17 diagnostic skills + gaussdb-kb (query); kb/ mounted read-only"

# ---------------------------------------------------------------- kb-import:知识库查询 + 导入
FROM base AS kb-import
USER root
RUN cd /opt/agent/config/opencode/skills && for d in gaussdb-*; do case "$d" in gaussdb-kb|gaussdb-kb-import|gaussdb-kb-init) ;; *) rm -rf "$d" ;; esac; done \
    && test "$(ls -d gaussdb-* | wc -l)" = "3"
COPY --from=imgdeps /opt/imgdeps/ /usr/local/lib/python3.12/site-packages/
RUN python3 -c "import PIL.Image, pillow_heif; pillow_heif.register_heif_opener(); print('pillow', PIL.__version__, 'pillow-heif', pillow_heif.__version__)"
# 7zip(7zz):解 GaussDB 官方文档 CHM(kb.py ingest <CHM> → 官方文档集);poppler-utils(pdftotext):导 PDF 规范
# —— 2026-10-25 发现容器里一直没有 pdftotext,PDF 在导入容器里导不进去。装完当场各跑一次,缺了构建就失败
RUN apt-get update && apt-get install -y --no-install-recommends 7zip poppler-utils \
    && rm -rf /var/lib/apt/lists/* && 7zz i >/dev/null && pdftotext -v 2>&1 | head -1
USER agent
ENV GSDB_KB_INBOX=/nas/kb/inbox/uploads
# opencode 工具权限按角色给(docker/podctl.py:opencode_permissions):导入容器里 kb-init 要用 write 工具写草稿
ENV AGENT_ROLE=kb-import
LABEL org.opencontainers.image.title="gaussdb-agent-kb-import" \
      org.opencontainers.image.source="https://github.com/sqlrush/gh_agent_k8s" \
      org.opencontainers.image.description="opencode + gaussdb-kb + gaussdb-kb-import + gaussdb-kb-init (HEIC/TIFF -> JPEG for image reading; 7zip for CHM docsets; poppler for PDF); kb/ mounted read-write; no GRMP token"
