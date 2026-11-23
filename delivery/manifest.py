"""交付清单:测试仓库(本仓库)转成两个交付包时,**选哪些文件**——按清单选,不是按排除法。

两个交付包,对应两个镜像:
  diag      诊断技能包      runtime 镜像    17 个诊断技能 + gaussdb-kb(只读查询) + SQL 白名单
  kbimport  知识库导入技能包 kb-import 镜像  gaussdb-kb-import + gaussdb-kb-init + gaussdb-kb

common 只有一份(agent/common/),每个文件在这里标明归属:
  shared    两个包都带(知识库库 kb/、连接层、报告存档、发现与审计……)
  diag      只有诊断包带(中间件客户端 grmp/、SQL 分析、存储过程定位……)
  kbimport  只有导入包带(入库、网页剪藏、PDF 分页、图片转换……)
新增 common 文件必须在这里登记,守卫测试(delivery/tests/)会核对:每个文件恰好一个归属;shared 不依赖只属于
某一个包的模块;每个技能的依赖都落在本包范围内。

**连接层(config / credential / db / backends / session)在 shared 里**:common/__init__.py 统一导出
common.find、common.DBError 等名字,所有技能都从这里取。导入包带着这几个文件,但不连库(没有白名单、不装 gaussdb-login)。

只用标准库:转化脚本在交付执行人的机器上只需要 git + Python 3.9+。
"""

FORMAT = 1                  # 清单格式版本;转化脚本读取标签里的清单,格式对不上就停

TEST_REPO = "gh_agent_k8s"  # 测试仓库(写进补充说明与 .delivery.json)
SOURCE_ROOT = "agent"       # 技能代码在测试仓库里的位置

# 装配前在完整源码上跑的核对(工作目录 agent/,{agent} 换成它的绝对路径):注入到各 SKILL.md / AGENTS.md 的
# 红线、回答模板、知识库契约必须与正文一致。交付包里不带这些工具,所以只能在裁剪前核对。
PRECHECKS = (
    ("红线注入", ("tools/inject_red_lines.py", "--check", "--root", "{agent}")),
    ("回答模板注入", ("tools/inject_answer_templates.py", "--check", "--root", "{agent}")),
    ("知识库契约注入", ("skills/gaussdb-kb-import/scripts/kb.py", "contract", "--skills-dir", "{agent}/skills")),
)
WHITELIST_DIR = "docs/delivery"   # 给中间件团队的白名单 DML(全量 + 历次增量 + 清单),在测试仓库根下

# 技能与 common 之外随包带的文件:(测试仓库里的位置, 交付包里的位置, 补充说明里的一句话)
# 根规范检索脚本:两份根提示词都执行 $GSDB_ROOTSPEC_HOME/search_docs.py(镜像里在 /workspace/),自建环境也要有
ROOTSPEC_SCRIPT = ("docker/search_docs.py", "rootspec/search_docs.py",
                   "根规范检索脚本,放到 `$GSDB_ROOTSPEC_HOME/` 下(见「根规范目录约定」)")

# 转化脚本从标签里取哪些路径(git archive);delivery/ 本身总是先取
EXTRACT = ("agent", "delivery", WHITELIST_DIR, ROOTSPEC_SCRIPT[0])

DIAG_SKILLS = (
    "gaussdb-config", "gaussdb-explain", "gaussdb-health", "gaussdb-kb", "gaussdb-lockwait", "gaussdb-login",
    "gaussdb-memanalyze", "gaussdb-procinfo", "gaussdb-proctune", "gaussdb-slowsql", "gaussdb-sqlfetch",
    "gaussdb-sqlreview", "gaussdb-sqltune", "gaussdb-topproc", "gaussdb-topsql", "gaussdb-vacuum",
    "gaussdb-waitevent", "gaussdb-wdr",
)
KBIMPORT_SKILLS = ("gaussdb-kb", "gaussdb-kb-import", "gaussdb-kb-init")

PACKAGES = {
    "diag": {
        "title": "诊断技能包",
        "image": "gaussdb-agent-runtime",
        "skills": DIAG_SKILLS,
        "agents_md": "AGENTS.md",                 # 相对 agent/;交付包里都叫 AGENTS.md
        "whitelist": True,
        "extra": (ROOTSPEC_SCRIPT,),
    },
    "kbimport": {
        "title": "知识库导入技能包",
        "image": "gaussdb-agent-kb-import",
        "skills": KBIMPORT_SKILLS,
        "agents_md": "AGENTS-kb-import.md",
        "whitelist": False,                       # 导入不连库,不带白名单
        "extra": (ROOTSPEC_SCRIPT,),
    },
}

# 技能目录里**不进交付**的子目录:测试与演示数据(运行时不读,守卫测试核对)
SKILL_EXCLUDE_DIRS = ("testdata", "tests", "__pycache__")

# common/ 下每个文件的归属(相对 agent/common/;目录写法 "x/*" 表示该目录下全部文件)
COMMON = {
    "shared": (
        "__init__.py", "audit.py", "config.py", "credential.py", "credential_cli.py", "db.py", "finding.py",
        "reports.py", "session.py", "version.py", "VERSION",
        "backends/__init__.py", "backends/base.py", "backends/gsql_backend.py", "backends/gsql_protocol.py",
        "backends/psycopg2_backend.py",
        "kb/__init__.py", "kb/atomic.py", "kb/bm25.py", "kb/cases.py", "kb/catalog.py", "kb/chmdoc.py",
        "kb/citestyle.py", "kb/config.py", "kb/docindex.py", "kb/docnav.py", "kb/docset.py", "kb/docsplit.py",
        "kb/embed.py", "kb/extract.py", "kb/graphfiles.py", "kb/htmldoc.py", "kb/inbox.py", "kb/indexer.py",
        "kb/layers.py", "kb/query.py", "kb/render.py", "kb/rulesfile.py", "kb/store.py", "kb/store_files.py",
        "kb/store_graph.py", "kb/store_sqlite.py", "kb/supersede.py", "kb/text.py", "kb/topics.py", "kb/types.py",
        "kb/versions.py",
        # 非代码:配置样例、红线与回答模板的正文(已注入各 SKILL.md / AGENTS.md,正文随包留一份供核对)
        "config.yaml", "config.example.yaml", "red_lines.md", "answer_templates/*",
    ),
    "diag": (
        "access.py", "catalog_scope.py", "cli.py", "exec_db.py", "explain_actual.py", "guc_policy.py", "kbsection.py",
        "kernel_funcs.py",
        "lockmodes.py", "proc_locate.py", "proc_source.py", "procname.py", "schema_infer.py", "search_path.py",
        "spillnote.py", "sqlnotfound.py", "sqlqualify.py", "sqlrefs.py", "sysdict.py", "sysobjects.py",
        "syssql.py", "timewin.py", "windata.py",
        # 交互耗时报告(v1.4.14):运维在终端里运行,不是技能(模型看不到)
        "timing/__init__.py", "timing/analysis.py", "timing/cli.py", "timing/grmplog.py", "timing/mask.py",
        "timing/render.py", "timing/store.py", "timing/report.html",
        "sql.py",           # 已清空,但 13 个诊断技能按「common/sql.py 存在」定位 common 目录,不能删
        "grmp/__init__.py", "grmp/client.py", "grmp/columns.py", "grmp/deltas.py", "grmp/errors.py", "grmp/hints.py",
        "grmp/params.py", "grmp/paths.py", "grmp/placeholder.py", "grmp/proxy.py", "grmp/registry.py", "grmp/routing.py",
        "grmp/runner.py", "grmp/script.py", "grmp/serialize.py", "grmp/settings.py", "grmp/signing.py",
        "grmp/sm2.py", "grmp/sm3.py", "grmp/statement.py", "grmp/timing.py", "grmp/userid.py", "grmp/values.py",
    ),
    "kbimport": (
        "kb/docset_pack.py", "kb/imgconv.py", "kb/ingest.py", "kb/initstd.py", "kb/lock.py", "kb/pdfpages.py",
        "kb/webclip.py",
    ),
}

# ---------------------------------------------------------------- 补充说明的素材(守卫测试与代码 / Dockerfile 核对)

PYTHON = "3.12(镜像里的版本,测试都在 3.12 上跑;行内另建环境请用同一版本)"
OPENCODE_VERSION = "1.18.27"   # 与 docker/Dockerfile 的 ARG OPENCODE_VERSION 一致(守卫测试核对)

PY_DEPS = {   # 包名 → (版本要求, 用途, 哪些包要)
    "psycopg2-binary": ("==2.9.10", "数据库驱动(与 psycopg2 同一个模块名,有编译条件也可装 psycopg2==2.9.10)", ("diag", "kbimport")),
    "cryptography": (">=41", "口令的 AES-256-GCM 解密", ("diag", "kbimport")),
    "PyYAML": (">=6", "配置与白名单脚本定义的解析", ("diag", "kbimport")),
    "sqlite-vec": ("==0.1.9", "知识库向量检索(没装时只有关键词检索)", ("diag", "kbimport")),
    "pillow": ("==12.3.0", "照片 / 截图(HEIC、TIFF)转 JPEG 给模型看图", ("kbimport",)),
    "pillow-heif": ("==1.8.1", "pillow 读 HEIC", ("kbimport",)),
}

SYS_DEPS = {  # 程序 → (用途, 哪些包要)
    "opencode " + OPENCODE_VERSION: ("智能体运行环境(版本与镜像一致)", ("diag", "kbimport")),
    "ripgrep (rg)": ("opencode 的文件检索", ("diag", "kbimport")),
    "pdftotext / pdftoppm(poppler-utils)": ("PDF 抽文字、扫描件逐页渲染", ("kbimport",)),
    "7zz(7zip)": ("解 GaussDB 官方文档 CHM", ("kbimport",)),
    "gsql(可选)": ("只在直连模式(driver: gsql)用;走中间件不需要。可用 GDAA_GSQL 指定路径", ("diag",)),
}

# 环境变量:名字 → (哪些包读, 必填, 说明)。守卫测试核对「交付代码里出现的变量名、提示词与技能文档里写的 $变量」
# 与这里一致,新增变量必须先登记。
ENV = {
    "GSDB_SKILLS_DIR": (("diag", "kbimport"), "必填", "技能目录(放 common/、scripts/ 与各技能的那一层);根提示词里的命令按它写。镜像里由启动脚本设为 opencode 的技能目录"),
    "GSDB_ROOTSPEC_HOME": (("diag", "kbimport"), "必填", "根规范目录:下面是 docs/(规范文档)与检索脚本 search_docs.py,见「根规范目录约定」。镜像里是 /workspace"),
    "ROOTSPEC_DIR": (("diag", "kbimport"), "否", "直接指定根规范文档目录(优先于 $GSDB_ROOTSPEC_HOME/docs/);一般不用设"),
    "GSDB_HOME": (("diag", "kbimport"), "否", "状态目录(连接配置、会话、加密口令)。不设时为 /workspace/.opencode/skills/common"),
    "GDAA_HOME": (("diag", "kbimport"), "否", "GSDB_HOME 的旧名,兼容保留"),
    "GSDB_PASSWORD": (("diag", "kbimport"), "否", "直连模式的数据库口令(优先于加密口令文件);走中间件不用"),
    "GDAA_PASSWORD": (("diag", "kbimport"), "否", "GSDB_PASSWORD 的旧名,兼容保留"),
    "GDAA_GSQL": (("diag", "kbimport"), "否", "gsql 程序路径,不设时按 PATH 找 gsql"),
    "GSDB_SESSION": (("diag", "kbimport"), "否", "登录会话句柄(gaussdb-login 给出;平台也可按人注入)"),
    "GSDB_SESSION_TTL_HOURS": (("diag", "kbimport"), "否", "登录会话有效期(小时),不设时 12"),
    "GSDB_USER_ID": (("diag", "kbimport"), "容器部署必填", "当前用户工号:审计、报告落款、剪藏批次归属"),
    "GSDB_REPORTS_DIR": (("diag", "kbimport"), "否", "报告存档目录;不设时不存档"),
    "GSDB_DASH_BASE_URL": (("diag", "kbimport"), "否", "报告页面的站点前缀(报告接口用);留空为相对地址"),
    "GSDB_KB_DIR": (("diag", "kbimport"), "否", "知识库目录;不设时为安装根下的 kb/(与 skills/ 同级)"),
    "GSDB_KB_INBOX": (("diag", "kbimport"), "否", "导入文件收件目录;不设时为 <知识库>/inbox/uploads"),
    "KB_PDF_MAX_PAGES": (("kbimport",), "否", "扫描件 PDF 一次最多渲染的页数,不设时 30"),
    "OPENCODE_CONFIG": (("diag", "kbimport"), "否", "opencode 配置文件路径(读 embedding 配置用);不设时按 opencode 默认位置找"),
    "GRMP_REGISTRY": (("diag",), "否", "白名单脚本定义目录;不设时为包里的 scripts/registry/"),
    "GRMP_API_HOST": (("diag",), "按连接配置", "中间件地址(连接配置里 host_env 指向的变量)"),
    "GRMP_AUTH_TOKEN": (("diag",), "按连接配置", "中间件令牌(连接配置里 token_env 指向的变量;容器部署由凭据代理注入)"),
    "GRMP_APPKEY": (("diag",), "开签名时必填", "中间件签名的 Appkey"),
    "GRMP_PATH_PREFIX": (("diag",), "必填", "中间件接口路径前缀(中间件团队给出);没有默认值,不设直接报错"),
    "GRMP_PROXY_SOCKET": (("diag",), "否", "凭据代理的套接字(容器部署由平台设置);设了就经代理访问中间件"),
    "GRMP_SM2_PRIVATE_KEY": (("diag",), "全签名档必填", "SM2 私钥(容器部署只在凭据代理里,不进模型所在容器)"),
    "GRMP_SIGN_MODE": (("diag",), "否", "签名档位:full(默认)/ headers-only"),
    "GRMP_SIGN_TIMESTAMP": (("diag",), "否", "时间戳单位:ms(默认)/ s"),
    "GRMP_SIGN_FORMAT": (("diag",), "否", "签名格式:raw(默认)/ der"),
    "GRMP_SIGN_ENCODING": (("diag",), "否", "签名编码:hex(默认)/ base64"),
    "GRMP_SIGN_PAYLOAD": (("diag",), "否", "签名原文的拼法(按中间件约定)"),
    "GRMP_SIGN_USER_ID": (("diag",), "否", "SM2 签名 userId(国标默认值;行方用什么要问清)"),
    "GRMP_USER_ID_HEADER": (("diag",), "否", "调用人工号放进哪个请求头"),
    "GRMP_USER_ID_PARAM": (("diag",), "否", "调用人工号放进哪个报文键"),
    "G_EXPORT_DIR": (("diag",), "否", "慢 SQL 导出文件的目录"),
    "GSDB_PLATFORM_ACCOUNTS": (("diag",), "否", "云平台运维账号(逗号分隔),它们执行的 SQL 不进业务 Top SQL / 慢 SQL;默认已含 rdsAdmin、rdsBackup、rdsMetric、rdsRepl、rdsProxy,这里填的是额外再加的"),
    "KB_CLIP_ORIGINS": (("kbimport",), "否", "允许剪藏的网站来源(逗号分隔);空 = 网页剪藏关闭"),
    "KB_CLIP_RECEIVER": (("kbimport",), "否", "设为 1 表示本环境有剪藏接收服务(容器部署由平台设置)"),
}

# 根规范(行方 3 份规范文档)的约定:不在交付包里,随镜像发布
ROOTSPEC = ("根提示词按「本行知识库 → 根规范 → 模型自带知识」的顺序检索。根规范放在 `$GSDB_ROOTSPEC_HOME/docs/`,"
            "用 `$GSDB_ROOTSPEC_HOME/search_docs.py` 检索(md / txt / docx;装了 pdftotext 时也搜 pdf;只读、不联网)。"
            "部署时把本包的 `rootspec/search_docs.py` 放到 `$GSDB_ROOTSPEC_HOME/` 下;行内正式规范由行内放进 `$GSDB_ROOTSPEC_HOME/docs/`,"
            "不进代码仓库。镜像里 `GSDB_ROOTSPEC_HOME=/workspace`,预留了三份模板(第一行带 ROOTSPEC-TEMPLATE 标记,不参与检索)。")

# 交付包里允许出现的顶层条目(自检核对:多一个都不行)
TOP_LEVEL = {
    "diag": ("AGENTS.md", "common", "skills", "scripts", "whitelist", "rootspec", "补充说明.md", ".delivery.json"),
    "kbimport": ("AGENTS.md", "common", "skills", "rootspec", "补充说明.md", ".delivery.json"),
}
