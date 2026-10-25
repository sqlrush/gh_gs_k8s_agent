#!/usr/bin/env bash
# 后端 Pod 入口。顺序:目录 → 渲染配置 → .owner 独占 → **NAS→本地加载状态** → db 自检
#                      → 日志清理 → opencode serve + 后台定期回写
#                      → SIGTERM → WAL 合并 → **完整回写** → 退出备份 → 释放独占。
#
# 2026-09-21 状态策略变更(用户拍板):原来 XDG_* 直接指向 NAS,每次写立刻落盘、零丢失窗口,
# 代价是 SQLite 跑在 NFS 上。现在改成启动时拷到本地、读写在本地、定期回写、销毁前完整同步。
# **用户已确认接受「优雅关闭零丢失,非优雅关闭丢一个同步周期」**;非优雅包括 OOMKilled、
# 节点故障、--force 删 Pod —— 那三种没有 trap 的机会,是架构性代价。细节见 statesync.py。
set -euo pipefail

# ---- 固定池化的槽位(2026-10-23)---------------------
# 槽位容器重启后 /data、/tmp、/dev/shm 里上一个使用者的文件都还在。这里**什么都不碰**,直接把 1 号进程
# 交给 slotrun:它先清盘,再拷配置、渲染、等管家的指令。-E -s:不读 PYTHON* 环境变量与用户 site-packages;
# -E 也让 PYTHONUNBUFFERED 失效,所以另加 -u,日志才实时进 kubectl logs。
if [ "${POOL_MODE:-0}" = "slot" ]; then
  exec python3 -E -s -u /opt/agent/podctl.py pool-slot
fi
# 固定池化的管家:载入 / 回写 / 腾挪 + 控制接口 4098。模型不在这个容器里执行命令,只要 1 号进程交给它
if [ "${POOL_MODE:-0}" = "steward" ]; then
  exec python3 -u /opt/agent/podctl.py pool-steward
fi

NAS_ME=${NAS_ME:-/nas/me}
NAS_KB=${NAS_KB:-/nas/kb}
POD_NAME=${POD_NAME:-$(hostname)}
OPENCODE_PORT=${OPENCODE_PORT:-4096}
STATE_LOCAL=${STATE_LOCAL:-/data/state}
STATE_SYNC_SECONDS=${STATE_SYNC_SECONDS:-60}
PODCTL="python3 /opt/agent/podctl.py"


# ---- 目录 -------------------------------------------------------------------
# XDG_* 指本地态;$NAS_ME 仍是真相所在,由 statesync 在启动/定期/收尾三个时机同步。
export XDG_DATA_HOME=$STATE_LOCAL/xdg-data
export XDG_STATE_HOME=$STATE_LOCAL/xdg-state
# opencode 加载配置时会往 $XDG_CONFIG_HOME/opencode/ 写 .gitignore、往 $XDG_CACHE_HOME/opencode/ 写 models.json 临时文件;
# 镜像目录只读(readOnlyRootFilesystem)会让配置加载失败、所有 API 报错(探针 /global/health 不碰配置,发现不了)。
# 所以每次启动把镜像里的 skills/AGENTS.md(3 MB)与 models.json/rg(8 MB)复制到 /data 下再指过去。
export XDG_CONFIG_HOME=/data/config
export XDG_CACHE_HOME=/data/cache
# **gdaa 整个留在 NAS**:里面既有登录句柄又有加密凭据,凭据不能落本地盘;
# 句柄是几百字节的小文件,本地化没有性能收益(statesync.py 里有这条的完整理由)。
export GSDB_HOME=$NAS_ME/gdaa
export GSDB_KB_DIR=$NAS_KB                        # 共享知识库,**绝不本地化**
export GSDB_REPORTS_DIR=$NAS_ME/reports           # 技能报告存档;大盘经 4097 只读端口读它,随 Pod 重建不丢
export HOME=/data/home                            # 有些库要写 $HOME;放本地可写目录
export OPENCODE_CONFIG=/data/oc/opencode.json     # 渲染出来的运行时配置(含 key,0600)
export WORKSPACE=$STATE_LOCAL/workspace
DB=$XDG_DATA_HOME/opencode/opencode.db
BACKUP=$NAS_ME/backup                             # 备份要活得比 Pod 长,直接写 NAS
mkdir -p "$GSDB_HOME" "$NAS_ME/workspace" "$BACKUP" "$GSDB_REPORTS_DIR" \
         /data/home /data/oc /data/config/opencode /data/cache/opencode
cp -R /opt/agent/config/opencode/. /data/config/opencode/
cp -R /opt/agent/cache/opencode/. /data/cache/opencode/
chmod 700 "$GSDB_HOME"
[ -n "${GSDB_KB_INBOX:-}" ] && mkdir -p "$GSDB_KB_INBOX"
# exec 进容器排障时 `. /data/env.sh` 即得同样的目录变量;只放路径,不放令牌与密钥
env | grep -E '^(XDG_[A-Z_]+|GSDB_HOME|GSDB_KB_DIR|GSDB_KB_INBOX|OPENCODE_CONFIG|HOME)=' | sed 's/^/export /' > /data/env.sh

# ---- 配置:每次启动重生成,配置不是状态 ----------------------------------------
$PODCTL render-opencode-config --out "$OPENCODE_CONFIG"
$PODCTL render-gdaa-config --out "$GSDB_HOME/config.yaml"

# ---- 独占:同一用户目录同一时刻只许一个 Pod 打开 db --------------------------------
$PODCTL owner-acquire --dir "$NAS_ME" --pod "$POD_NAME"
( while sleep 30; do $PODCTL owner-refresh --dir "$NAS_ME" --pod "$POD_NAME" || exit 0; done ) &
REFRESHER=$!

# ---- 状态:NAS → 本地。**必须在 .owner 之后** ---------------------------------
# 独占没拿到就不该碰状态:两个 Pod 各拷一份本地副本再各自回写,是整份互相覆盖。
# state-load 内部会检查上次是否非优雅退出(dirty 标记),是就先给 NAS 留一份快照并记一行。
$PODCTL state-load --nas "$NAS_ME" --local "$STATE_LOCAL"
mkdir -p "$XDG_DATA_HOME/opencode" "$XDG_STATE_HOME/opencode" "$WORKSPACE"

# ---- 自检与清理 -------------------------------------------------------------
$PODCTL db-check --db "$DB" --backup-dir "$BACKUP"
$PODCTL log-prune --dir "$XDG_DATA_HOME/opencode/log" --days 7

# ---- 工作目录做成 git 仓库(只在首次):opencode 以此识别「项目」,Web 界面按项目列会话,撤销功能也靠它 ----
if [ ! -d "$WORKSPACE/.git" ]; then
  git -C "$WORKSPACE" init -q \
    && git -C "$WORKSPACE" -c user.name=agent -c user.email=agent@pod commit -q --allow-empty -m "workspace init" \
    && echo "工作目录: 已初始化为 git 仓库($WORKSPACE)"
fi

# ---- 启动 opencode + 后台定期回写;SIGTERM 转发给它,等它退出后再做收尾 --------------
cd "$WORKSPACE"
opencode serve --hostname 0.0.0.0 --port "$OPENCODE_PORT" &
OC=$!
# 报告只读端口:大盘的数据口。不是就绪条件,起不来只影响大盘,不影响对话。
$PODCTL serve-reports --dir "$GSDB_REPORTS_DIR" --port 4097 --kb-dir "$GSDB_KB_DIR" &
REPORTS=$!
# 定期回写。db 走 SQLite online backup(边写边拷也一致),其余文件按 mtime 增量。
# 每轮先 checkpoint 把 WAL 合回主库,免得回写出去的那份把最近的提交落在 -wal 里。
( while sleep "$STATE_SYNC_SECONDS"; do
    $PODCTL db-checkpoint --db "$DB" >/dev/null 2>&1 || true
    $PODCTL state-sync --nas "$NAS_ME" --local "$STATE_LOCAL" >/dev/null 2>&1 || true
  done ) &
SYNCER=$!
term() { kill -TERM "$OC" 2>/dev/null || true; }
trap term TERM INT
set +e
wait "$OC"; RC=$?
set -e
kill "$REFRESHER" "$SYNCER" "$REPORTS" 2>/dev/null || true
# 收尾顺序不能改:合 WAL → 完整回写并清 dirty → 备份 → 释放独占。
# 先释放独占的话,另一个 Pod 可能在我们还没写完时就开始加载。
$PODCTL db-checkpoint --db "$DB"
$PODCTL state-finish --nas "$NAS_ME" --local "$STATE_LOCAL"
$PODCTL db-backup --db "$NAS_ME/xdg-data/opencode/opencode.db" --backup-dir "$BACKUP" --keep 3
$PODCTL owner-release --dir "$NAS_ME" --pod "$POD_NAME"
echo "opencode 已退出(rc=$RC),状态已完整回写到 NAS"
exit "$RC"
