#!/usr/bin/env bash
# deploy.sh —— 交互式一键部署到 OpenCode。
#
# 做五件事，每一步都可以中途退出，退出前不会留下半套东西：
#   1. 检查前置（python3 + 依赖 + opencode）
#   2. 问清装到哪、$GSDB_HOME 放哪，并把环境变量写进 shell 配置
#   3. 安装 skill（复用 install-opencode.sh）
#   4. 交互生成 config.yaml，口令**加密存进凭据目录**（配置里不留明文）
#   5. 跑一轮连通性测试并展示结果
#
# 用法：
#   ./deploy.sh              # 全程交互
#   ./deploy.sh --dry-run    # 只看会做什么，不落盘
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
YEL=$'\033[33m'; RST=$'\033[0m'

say()  { printf "%s\n" "$*"; }
head1(){ printf "\n%s── %s %s\n" "$BOLD" "$*" "$RST"; }
ok()   { printf "  %s✓%s %s\n" "$GRN" "$RST" "$*"; }
bad()  { printf "  %s✗%s %s\n" "$RED" "$RST" "$*"; }
warn() { printf "  %s!%s %s\n" "$YEL" "$RST" "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf "  %s[dry-run] %s%s\n" "$DIM" "$*" "$RST"; else eval "$@"; fi; }

ask() {  # ask <提示> <默认值>  → 结果写进全局 REPLY_VAL
  local prompt="$1" def="${2:-}" ans
  if [ -n "$def" ]; then
    read -r -p "  $prompt [$def]: " ans || ans=""
    REPLY_VAL="${ans:-$def}"
  else
    read -r -p "  $prompt: " ans || ans=""
    REPLY_VAL="$ans"
  fi
}

confirm() {  # confirm <提示>  → 0 表示确认
  local ans
  read -r -p "  $1 [y/N]: " ans || ans=""
  [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

# ── 1. 前置检查 ──────────────────────────────────────────────────────────
head1 "1/5  前置检查"

command -v python3 >/dev/null || { bad "python3 未安装，装完再来"; exit 1; }
ok "python3 $(python3 -V 2>&1 | awk '{print $2}')"

# MISSING=""
# for m in psycopg2 cryptography yaml; do
#   python3 -c "import $m" 2>/dev/null || MISSING="$MISSING $m"
# done
# if [ -n "$MISSING" ]; then
#   bad "缺少 Python 依赖:$MISSING"
#   say "      python3 -m pip install -r \"$SRC/requirements.txt\""
#   confirm "现在装吗？" && run "python3 -m pip install -r \"$SRC/requirements.txt\"" || exit 1
# else
#   ok "Python 依赖齐全（psycopg2 / cryptography / yaml）"
# fi
# 
# if command -v opencode >/dev/null; then
#   ok "opencode $(opencode --version 2>/dev/null | head -1)"
# else
#   warn "没找到 opencode 命令 —— skill 仍可用命令行调用，但 OpenCode 里看不到"
# fi

# ── 2. 目录与环境变量 ────────────────────────────────────────────────────
head1 "2/5  安装位置与环境变量"

ask "skill 安装到" "${HOME}/.config/opencode/skills"; DEST="$REPLY_VAL"
ask "配置与凭据放在（\$GSDB_HOME）" "${GSDB_HOME:-${HOME}/.gdaa}"; GHOME="$REPLY_VAL"

say ""
say "  将要写入："
say "    skill        → $DEST"
say "    配置/凭据    → $GHOME  ${DIM}(权限 700)${RST}"
confirm "确认？" || { say "  已取消，未改动任何文件。"; exit 0; }

run "mkdir -p \"$GHOME\" && chmod 700 \"$GHOME\""
ok "$GHOME 已就绪"

# 环境变量写进 shell 配置。**先查重**：重复 export 不会出错，但同一个变量
# 在文件里出现三遍，下次有人改错一处就会花很久才发现改的那处没生效。
SHELL_RC="${HOME}/.zshrc"
[ -n "${BASH_VERSION:-}" ] && SHELL_RC="${HOME}/.bashrc"
ask "环境变量写进哪个文件" "$SHELL_RC"; SHELL_RC="$REPLY_VAL"

if grep -q "GSDB_HOME=" "$SHELL_RC" 2>/dev/null; then
  warn "$SHELL_RC 里已有 GSDB_HOME，跳过写入（避免同名变量出现多次）"
  say "      当前值：$(grep 'GSDB_HOME=' "$SHELL_RC" | tail -1)"
else
  run "printf '\n# gaussdb skills\nexport GSDB_HOME=%s\n' \"$GHOME\" >> \"$SHELL_RC\""
  ok "已写入 $SHELL_RC"
fi
export GSDB_HOME="$GHOME"

# ── 3. 安装 skill ────────────────────────────────────────────────────────
head1 "3/5  安装 skill"

# 快照由 install-opencode.sh 负责（它带保留策略和版本戳）。这里不再自己拷一份，
# 否则每次部署会留下两个内容相同的快照目录，回滚时不知道该用哪个。
if [ -d "$DEST" ] && [ "$DRY" = 0 ]; then
  say "      ${DIM}旧安装会先被快照，回滚：bash ${SRC}/install-opencode.sh --rollback${RST}"
fi

if [ "$DRY" = 1 ]; then
  run "bash \"$SRC/install-opencode.sh\" --dest \"$DEST\" --dry-run"
else
  bash "$SRC/install-opencode.sh" --dest "$DEST" >/dev/null 2>&1 \
    && ok "已安装 $(ls -d "$DEST"/gaussdb-* 2>/dev/null | wc -l | tr -d ' ') 个 skill" \
    || { bad "安装失败，重跑 install-opencode.sh 看详细输出"; exit 1; }
fi

# ── 4. 生成配置 ──────────────────────────────────────────────────────────
head1 "4/5  生成配置"

CFG="$GHOME/config.yaml"
if [ -f "$CFG" ]; then
  warn "$CFG 已存在"
  confirm "覆盖它？（旧文件会备份）" || { say "  保留原配置，跳到连通性测试。"; SKIP_CFG=1; }
  [ "${SKIP_CFG:-0}" = "0" ] && run "cp \"$CFG\" \"$CFG.bak.$(date +%Y%m%d-%H%M%S)\""
fi

if [ "${SKIP_CFG:-0}" = "0" ]; then
  say "  两种接入方式："
  say "    ${BOLD}1) gsql${RST}  直连数据库（本机/内网可直达）"
  say "    ${BOLD}2) api${RST}   走 GRMP 中间件（客户生产环境通常是这个）"
  ask "选哪种" "1"; MODE_SEL="$REPLY_VAL"

  if [ "${MODE_SEL:-1}" = "2" ]; then
    ask "中间件 host" "grmp-web.example.internal"; API_HOST="$REPLY_VAL"
    ask "中间件 host_dev" "GRMP_API_HOST"; HOST_ENV="$REPLY_VAL"
    ask "中间件 port" "80"; API_PORT="$REPLY_VAL"
    ask "令牌环境变量名" "GRMP_AUTH_TOKEN"; TOK_ENV="$REPLY_VAL"
    run "cat > \"$CFG\" <<YAML
# 由 deploy.sh 生成。首行决定所有 skill 怎么连库。
connection_mode: api

api_connection:
  - host: $API_HOST
    host_env: $HOST_ENV
    port: $API_PORT
    # 令牌放环境变量，不落盘 —— 它是长期有效、无重放保护的静态凭据
    token_env: $TOK_ENV
YAML"
    ok "已写入 ${CFG}（api 模式）"
    say ""
    warn "还要设置令牌和API HOST（本次会话 + 持久化各一次）："
    say "      export $TOK_ENV='<令牌>'"
    say "      echo \"export $TOK_ENV='<令牌>'\" >> $GHOME/grmp.env && chmod 600 $GHOME/grmp.env"
    say "      export $HOST_ENV='<HOST>'"
    say "      echo \"export $HOST_ENV='<HOST>'\" >> $GHOME/grmp.env && chmod 600 $GHOME/grmp.env"
    warn "还要设置中间件接口路径前缀（中间件团队提供，以 / 开头，只写路径）："
    say "      export GRMP_PATH_PREFIX='<路径前缀>'"
    say "      echo \"export GRMP_PATH_PREFIX='<路径前缀>'\" >> $GHOME/grmp.env"
  else
    ask "应用分组名" "app1"; APP="$REPLY_VAL"
    ask "连接名" "og-prod"; CNAME="$REPLY_VAL"
    ask "数据库类型 (opengauss/gaussdb)" "opengauss"; CTYPE="$REPLY_VAL"
    ask "host" "127.0.0.1"; CHOST="$REPLY_VAL"
    ask "port" "5432"; CPORT="$REPLY_VAL"
    ask "database" "postgres"; CDB="$REPLY_VAL"
    ask "user" "gaussdb"; CUSER="$REPLY_VAL"
    ask "driver (psycopg2/gsql)" "psycopg2"; CDRV="$REPLY_VAL"

    run "cat > \"$CFG\" <<YAML
# 由 deploy.sh 生成。首行决定所有 skill 怎么连库。
connection_mode: gsql

db_connections:
  $APP:
    - name: $CNAME
      type: $CTYPE
      host: $CHOST
      port: $CPORT
      database: $CDB
      user: $CUSER
      driver: $CDRV
      # 注意:这里**不写 password** —— 配置文件不允许出现明文口令,
      # 口令加密存在 $GHOME/credentials/ 下,由脚本自动解密
YAML"
    ok "已写入 ${CFG}（gsql 模式，无明文口令）"

    say ""
    say "  ${BOLD}口令加密存放${RST}（配置文件里不留明文）"
    if [ "$DRY" = 1 ]; then
      run "python3 -m common.credential_cli set \"$CNAME\""
    else
      if ( cd "$SRC" && python3 -m common.credential_cli set "$CNAME" ); then
        ok "口令已加密存入 $GHOME/credentials/$CNAME.enc"
      else
        CRED_MISSING=1
        warn "口令未设置。补上再验连通："
        say "      cd $SRC && python3 -m common.credential_cli set $CNAME"
      fi
    fi
  fi
fi

# 保留了已有配置时，MODE_SEL / APP / CNAME 这一轮**根本没被赋值** —— 下面
# 第 5 步的 ${MODE_SEL:-1} 会兜底成 gsql，${CNAME:-og-prod} 兜底成一个编出来
# 的连接名。于是保留着 api 配置的客户，会看到脚本拿 og-prod 去直连数据库并
# 报一串红：既不是他的环境问题，也不是代码问题，纯粹是这里猜错了模式。
# 按文件里实际写的来。
if [ "${SKIP_CFG:-0}" = "1" ]; then
  if grep -qE '^[[:space:]]*connection_mode:[[:space:]]*api' "$CFG" 2>/dev/null; then
    MODE_SEL=2
    TOK_ENV="$(sed -nE 's/^[[:space:]]*token_env:[[:space:]]*([A-Za-z_][A-Za-z0-9_]*).*/\1/p' "$CFG" | head -1)"
    TOK_ENV="${TOK_ENV:-GRMP_AUTH_TOKEN}"
    ok "沿用已有配置：api 模式，令牌变量 ${TOK_ENV}"
  else
    MODE_SEL=1
    FIRST_CONN="$(cd "$SRC" && GSDB_HOME="$GHOME" python3 -c "
from common import config
c = config.load()
print('%s %s' % (c[0].app or '', c[0].name) if c else '')" 2>/dev/null || true)"
    APP="${FIRST_CONN%% *}"; CNAME="${FIRST_CONN#* }"
    [ -n "${APP:-}" ] || APP="app1"
    [ -n "${CNAME:-}" ] || CNAME="og-prod"
    ok "沿用已有配置：gsql 模式，连接 ${APP}/${CNAME}"
  fi
fi

run "chmod 600 \"$CFG\" 2>/dev/null || true"

# ── 5. 连通性测试 ────────────────────────────────────────────────────────
head1 "5/5  连通性测试"

if [ "$DRY" = 1 ]; then
  say "  ${DIM}[dry-run] 跳过实际连接${RST}"
  say ""
  ok "dry-run 结束，未改动任何文件"
  exit 0
fi

LOGIN="$DEST/gaussdb-login/scripts/login.py"
PASS=0; FAIL=0
check() {  # check <名称> <命令...>
  local name="$1"; shift
  if out=$("$@" 2>&1); then ok "$name"; PASS=$((PASS+1));
  else bad "$name"; printf "      %s\n" "$(printf '%s' "$out" | tail -2)"; FAIL=$((FAIL+1)); fi
}

check "配置文件可解析" python3 -c "
import sys; sys.path.insert(0, '$DEST')
from common import config
config.mode(); config.load()
print('ok')"

if [ "${MODE_SEL:-1}" = "2" ]; then
  say "  ${DIM}api 模式需要实例 IP 与库名才能验连通，登录时提供：${RST}"
  say "      python3 $LOGIN --ip <实例IP> --database <库名>"
  # **令牌缺失必须判失败。**
  #
  # 这里原先写成 `print('ok' if e.resolve_token() else 'no-token')` 然后
  # 无论如何退出 0 —— check 只看退出码，于是令牌根本没设也显示 ✓。
  # 真跑时抓到的：一个「前置条件没满足却报通过」的检查，比没有这个检查更糟。
  check "中间件端点已配置" python3 -c "
import sys; sys.path.insert(0, '$DEST')
from common import config
e = config.api_endpoint()
assert e.host and e.port, '端点 host/port 不完整'
print('端点 %s:%s' % (e.host, e.port))"

  check "中间件接口路径前缀已配置（GRMP_PATH_PREFIX）" python3 -c "
import sys; sys.path.insert(0, '$DEST')
from common.grmp import paths
try:
    print('路径前缀 %s' % paths.require())
except paths.PrefixError as exc:
    raise SystemExit(str(exc))"

  check "令牌可读取（${TOK_ENV}）" python3 -c "
import sys; sys.path.insert(0, '$DEST')
from common import config
tok = config.api_endpoint().resolve_token()
if not tok:
    raise SystemExit('环境变量 $TOK_ENV 未设置 —— 登录时会在第一次调用中间件时失败')
print('令牌已就绪（长度 %d，内容不显示）' % len(tok))"
elif [ "${CRED_MISSING:-0}" = "1" ]; then
  # **口令没设成就不要跑连通性测试。** 跑出来的三条红既不是环境问题也不是
  # 代码问题,是「你还没设口令」—— 但报出来像是连不上,会把人指向错误的方向。
  warn "跳过连通性测试：口令还没设置，此时连必然失败，报出来的红会误导"
  say "      设完口令后单独验："
  say "      python3 $LOGIN --app ${APP:-app1} --conn ${CNAME:-og-prod}"
else
  check "登录并验证连接" python3 "$LOGIN" --app "${APP:-app1}" --conn "${CNAME:-og-prod}"
  check "取数（topsql）" python3 "$DEST/gaussdb-topsql/scripts/topsql.py" --limit 1
  check "健康检查（1 个维度）" python3 "$DEST/gaussdb-health/scripts/health.py" --include conn
fi

say ""
if [ "$FAIL" = 0 ]; then
  printf "  %s全部通过（%d 项）%s\n" "$GRN" "$PASS" "$RST"
else
  printf "  %s%d 项通过，%d 项失败%s\n" "$YEL" "$PASS" "$FAIL" "$RST"
  say "  失败多半是口令、网络或令牌 —— 上面每条都带了原始错误。"
fi

head1 "完成"
say "  ${BOLD}新开一个终端${RST}（或 source ${SHELL_RC}）让 GSDB_HOME 生效，然后："
say ""
say "    python3 $LOGIN --list          # 看有哪些库可连"
say "    python3 $LOGIN --status        # 现在连的是哪个"
say ""
say "  OpenCode 里直接说人话即可，模型会先调 gaussdb-login 再取数。"
say "  回滚见 docs/delivery/09-版本与回滚.md"
