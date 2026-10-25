#!/usr/bin/env bash
# grmp-mock 核心场景演示 —— 自己起服务、自己停，输出全打到屏幕。
#
# 用法：
#   bash grmp_middleware/grmp_demo.sh          跑全部场景
#   bash grmp_middleware/grmp_demo.sh 6        只跑第 6 个
#   bash grmp_middleware/grmp_demo.sh 6 7 8    跑第 6、7、8 个
#
# 前置：og5 在 127.0.0.1:5433，~/.gdaa/grmp/ 下已有 instances.yaml
#      （没有的话本脚本会自动建）

set -u
cd "$(dirname "$0")/.." || exit 1

export GSDB_HOME="${GSDB_HOME:-$HOME/.gdaa}"
export GRMP_AUTH_TOKEN="${GRMP_AUTH_TOKEN:-0123456789abcdef0123456789abcdef}"

IP=10.0.0.9
DB=/tmp/grmp_demo.db
INST=$HOME/.gdaa/grmp/instances.yaml
# 默认 8769：与 ~/.gdaa/config.yaml 里 og-grmp 的端口一致，场景 10 才能直接跑
PORT=${GRMP_DEMO_PORT:-8769}
# 接口路径前缀:与客户端、模拟中间件同一个变量;没设就用样例前缀
export GRMP_PATH_PREFIX="${GRMP_PATH_PREFIX:-/example/grmp/diagnostic/agent}"
BASE="http://127.0.0.1:$PORT$GRMP_PATH_PREFIX/common-operations"
INVOKE="$BASE/invoke"
Q="'"                                   # 单引号，避免多层转义

C_H=$'\033[1;36m'; C_K=$'\033[1;33m'; C_R=$'\033[0m'; C_W=$'\033[1;31m'

hdr() { printf '\n%s══ 场景 %s：%s%s\n' "$C_H" "$1" "$2" "$C_R"; }
note() { printf '%s   %s%s\n' "$C_K" "$1" "$C_R"; }
warn() { printf '%s   %s%s\n' "$C_W" "$1" "$C_R"; }

# 打印请求与响应。$1=URL  $2=JSON体  $3=额外 curl 参数（可空）
call() {
  local url="$1" body="$2"; shift 2
  printf '   → POST %s\n' "${url#http://127.0.0.1:$PORT}"
  printf '     %s\n' "$body"
  printf '   ← '
  curl -s -X POST -H "auth: $GRMP_AUTH_TOKEN" -H "Content-Type: application/json" \
       -d "$body" "$@" "$url" |
    python3 -c 'import sys,json
raw=sys.stdin.read()
try: print(json.dumps(json.loads(raw),ensure_ascii=False,indent=1))
except Exception: print(raw)'
}

# 只打印几个关心的字段
brief() {
  curl -s -X POST -H "auth: $GRMP_AUTH_TOKEN" -H "Content-Type: application/json" \
       -d "$2" "$1" | python3 -c "import sys,json;d=json.load(sys.stdin);$3"
}

# ---------- 准备 ----------
[ -f "$INST" ] || { mkdir -p "$(dirname "$INST")"; printf '%s: og\n' "$IP" > "$INST"; }

printf '%s准备：注册脚本到 %s%s\n' "$C_H" "$DB" "$C_R"
rm -f "$DB"
python3 -m grmp_middleware.grmp_register --db "$DB" || exit 1

# 先探端口。被占用时 mock 会 bind 失败退出，而后续调用会打到别人的服务上，
# 报出一堆与本项目无关的错——必须在这里就停住。
if ! python3 -c "
import socket, sys
s = socket.socket()
try:
    s.bind(('127.0.0.1', $PORT))
except OSError as exc:
    sys.exit('端口 $PORT 已被占用（%s）。换一个：GRMP_DEMO_PORT=8770 bash grmp_middleware/grmp_demo.sh' % exc)
finally:
    s.close()
"; then exit 1; fi

printf '\n%s准备：启动 grmp-mock（端口 %s）%s\n' "$C_H" "$PORT" "$C_R"
# 横幅与访问日志都走 stderr。全打到屏幕会把每条 200 日志混进报文里，
# 所以导到文件：横幅在下面单独打一次，访问日志留在文件里备查。
LOG=/tmp/grmp_demo.log
python3 -m grmp_middleware.grmp_mock --db "$DB" --instances "$INST" --port "$PORT" \
        > /dev/null 2> "$LOG" &
MOCK=$!
trap 'kill $MOCK 2>/dev/null; wait $MOCK 2>/dev/null' EXIT

# 等起来，并确认应答的确实是我们这个服务（而不是碰巧占着端口的别家）
PROBE=$(curl -s --retry-connrefused --retry 25 --retry-delay 1 \
        -X POST -H "auth: $GRMP_AUTH_TOKEN" -H "Content-Type: application/json" \
        -d "{\"dataIp\":\"$IP\"}" "$BASE")
case "$PROBE" in
  *'"result"'*) ;;    # 成功信封才算数：错误信封里也有 "code"，只判它等于没判
  *) printf '%s端口 %s 上的应答不是预期的清单，收到：%s%s\n' \
       "$C_W" "$PORT" "${PROBE:0:160}" "$C_R"
     printf '%s服务端日志：%s%s\n' "$C_W" "$LOG" "$C_R"; exit 1 ;;
esac

# 启动横幅（两条 ==== 之间），它列着本进程当前的全部未证实假设
awk '/^={10,}/{n++} {print} n==2{exit}' "$LOG"
note "访问日志在 ${LOG}（不打到屏幕，免得混进报文里）"

# ---------- 按逻辑名解析 ID（不硬编码，与客户环境的做法一致）----------
# 先落盘再解析：把 curl 和 python 塞进一层 $( ) 里嵌套引号，出问题时看不出
# 是哪一环坏的（实测就踩过：报文正常但嵌套里取到的是错误信封）。
LIST_JSON=/tmp/grmp_demo_list.json
curl -s -X POST -H "auth: $GRMP_AUTH_TOKEN" -H "Content-Type: application/json" \
     -d "{\"dataIp\":\"$IP\",\"limit\":1000}" "$BASE" > "$LIST_JSON"

IDS=$(python3 - "$LIST_JSON" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
if d.get("code") != "0":
    sys.exit("查清单失败：%s" % d.get("msg", d))
for x in d["result"]["list"]:
    print("ID_%s=%s" % (x["cmd_name"].replace(".", "_").upper(), x["id"]))
PY
) || { echo "$IDS"; exit 1; }
eval "$IDS"

printf '\n%s脚本 ID（按 cmd_name 解析得来，未硬编码）：%s\n' "$C_H" "$C_R"
printf '   health.db_info=%s  session.by_user=%s  session.top_by=%s  session.active_only=%s  slowsql.slow_sql=%s\n' \
  "$ID_HEALTH_DB_INFO" "$ID_SESSION_BY_USER" "$ID_SESSION_TOP_BY" \
  "$ID_SESSION_ACTIVE_ONLY" "$ID_SLOWSQL_SLOW_SQL"

WANT="$*"
run() { [ -z "$WANT" ] || [[ " $WANT " == *" $1 "* ]]; }

# ============================================================
run 1 && {
hdr 1 "接口一：查命令清单（看 PageInfo 全 18 字段）"
note "看点：code 是字符串 \"0\"；navigatepageNums 第二个 p 小写；prePage/nextPage 是 0 不是 null"
call "$BASE" "{\"dataIp\":\"$IP\",\"offset\":1,\"limit\":2}"
}

run 2 && {
hdr 2 "接口一：dataIp 查不到实例（与客户样例逐字比对）"
note "看点：HTTP 200 而不是 4xx；msg 必须一字不差"
call "$BASE" "{\"dataIp\":\"1.2.3.4\"}"
printf '   HTTP 状态码：'
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "auth: $GRMP_AUTH_TOKEN" \
     -d "{\"dataIp\":\"1.2.3.4\"}" "$BASE"
}

run 3 && {
hdr 3 "接口二：执行无参脚本（全字符串化 / 布尔 / NULL）"
note "看点：datdba=\"10\" 是字符串不是数字；datistemplate=\"f\"；datacl=\"\"（NULL 渲染成空串）"
call "$INVOKE" "{\"dataIp\":\"$IP\",\"id\":\"$ID_HEALTH_DB_INFO\"}"
}

run 4 && {
hdr 4 "接口二：执行真实 skill 脚本（3 个参数，含 DateTime）"
note "看点：param 元素是 {param_name, param_value}，取值一律字符串（整数也写成 \"0\"）"
call "$INVOKE" "{\"dataIp\":\"$IP\",\"id\":\"$ID_SLOWSQL_SLOW_SQL\",\"param\":[{\"param_name\":\"threshold_ms\",\"param_value\":\"0\"},{\"param_name\":\"begin_time\",\"param_value\":\"2020-01-01 00:00:00\"},{\"param_name\":\"limit\",\"param_value\":\"3\"}]}"
}

run 5 && {
hdr 5 "类型校验：注入载荷在连库前被拦住"
note "看点：错误来自类型校验（Integer param value…），不是数据库报的"
call "$INVOKE" "{\"dataIp\":\"$IP\",\"id\":\"$ID_SLOWSQL_SLOW_SQL\",\"param\":[{\"param_name\":\"threshold_ms\",\"param_value\":\"0 OR 1=1\"},{\"param_name\":\"begin_time\",\"param_value\":\"2020-01-01 00:00:00\"},{\"param_name\":\"limit\",\"param_value\":\"3\"}]}"
echo
note "Boolean 只认小写 true/false，传 True 也拒："
call "$INVOKE" "{\"dataIp\":\"$IP\",\"id\":\"$ID_SESSION_ACTIVE_ONLY\",\"param\":[{\"param_name\":\"active_only\",\"param_value\":\"True\"},{\"param_name\":\"limit\",\"param_value\":\"5\"}]}"
}

run 6 && {
hdr 6 "⚠️ String 参数可被注入（这是设计上接受的结果，不是 bug）"
note "脚本：where usename = '{{username}}'   引号由脚本作者写，中间件只做文本替换"
BASE_P="{\"dataIp\":\"$IP\",\"id\":\"$ID_SESSION_BY_USER\",\"param\":[{\"param_name\":\"username\",\"param_value\":\"no_such_user_zz9\"}]}"
# ${Q} 要带花括号：$Q1 会被当成变量名 Q1
INJ_P="{\"dataIp\":\"$IP\",\"id\":\"$ID_SESSION_BY_USER\",\"param\":[{\"param_name\":\"username\",\"param_value\":\"no_such_user_zz9${Q} or ${Q}1${Q}=${Q}1\"}]}"
printf '   正常（传不存在的用户名）：'
brief "$INVOKE" "$BASE_P" 'print(len(d["result"]["data"]), "行")'
printf '   注入 no_such_user_zz9%s or %s1%s=%s1 ：' "$Q" "$Q" "$Q" "$Q"
brief "$INVOKE" "$INJ_P" 'print(len(d["result"]["data"]), "行")'
echo
warn "0 行 → 若干行 = 注入成功。渲染成 where usename = 'no_such_user_zz9' or '1'='1'"
warn "客户中间件若也是 String.replace 实现，客户环境有同一个洞——需向客户确认是否转义"
}

run 7 && {
hdr 7 "标识符位替换真的生效（绑定变量做不到的事）"
note "同一条脚本 order by {{sort_col}}，只换取值，结果顺序应当不同"
for col in backend_start usename; do
  printf '   sort_col=%-14s → usename 顺序：' "$col"
  brief "$INVOKE" "{\"dataIp\":\"$IP\",\"id\":\"$ID_SESSION_TOP_BY\",\"param\":[{\"param_name\":\"sort_col\",\"param_value\":\"$col\"},{\"param_name\":\"limit\",\"param_value\":\"3\"}]}" \
        'print([r["usename"] for r in d["result"]["data"]])'
done
echo
note "若改用绑定变量，ORDER BY \$1 会按常量排序（等于不排序），两次结果会完全相同且不报错"
}

run 8 && {
hdr 8 "失败分支：不产出 result 键"
note "看点：盲目取 resp[\"result\"][\"data\"] 的调用方会当场 KeyError，而不是安静拿到空列表"
printf '   ── 脚本 id 不存在 ──\n'
call "$INVOKE" "{\"dataIp\":\"$IP\",\"id\":\"99999\"}"
printf '\n   ── 缺必填参数 ──\n'
call "$INVOKE" "{\"dataIp\":\"$IP\",\"id\":\"$ID_SLOWSQL_SLOW_SQL\",\"param\":[{\"param_name\":\"limit\",\"param_value\":\"3\"}]}"
printf '\n   ── 接口文档 3.2 那个错误的参数形状 ──\n'
call "$INVOKE" "{\"dataIp\":\"$IP\",\"id\":\"$ID_SESSION_BY_USER\",\"param\":[{\"param_name\":\"username\",\"data_type\":\"String\",\"required\":true,\"description\":\"gaussdb\"}]}"
}

run 9 && {
hdr 9 "鉴权与传输层"
printf '   错误令牌     → HTTP %s  ' \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "auth: wrong" -d "{\"dataIp\":\"$IP\"}" "$BASE")"
curl -s -X POST -H "auth: wrong" -H "Content-Type: application/json" -d "{\"dataIp\":\"$IP\"}" "$BASE"; echo
printf '   Authorization 头（不是 auth）→ '
curl -s -X POST -H "Authorization: Bearer $GRMP_AUTH_TOKEN" -H "Content-Type: application/json" -d "{\"dataIp\":\"$IP\"}" "$BASE"; echo
printf '   GET 方法     → HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE")"
printf '   未知路由     → HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/nope")"
printf '   非法 JSON 体 → '
curl -s -X POST -H "auth: $GRMP_AUTH_TOKEN" -H "Content-Type: application/json" -d '{not json' "$BASE"; echo
echo
note "业务错误一律 HTTP 200 + code!=\"0\"；HTTP 状态码只用于传输层问题（405/404）"
}

run 10 && {
hdr 10 "双路径一致性：同一条 skill 命令，直连 vs 中间件"
note "需要 ~/.gdaa/config.yaml 里有 og(driver:psycopg2) 与 og-grmp(driver:grmp, port:$PORT)"
if python3 -c "
import sys; sys.path.insert(0,'.')
from common.config import find
c = find('og-grmp')
sys.exit(0 if c.port == $PORT else 1)" 2>/dev/null; then
  ( cd skills/gaussdb-slowsql/scripts &&
    python3 slowsql.py -c og      --threshold 0 --limit 3 --begin_time "2020-01-01 00:00:00" --format json > /tmp/direct.json 2>&1
    python3 slowsql.py -c og-grmp --threshold 0 --limit 3 --begin_time "2020-01-01 00:00:00" --format json > /tmp/mw.json 2>&1 )
  if diff -q /tmp/direct.json /tmp/mw.json >/dev/null 2>&1; then
    printf '   直连 vs 中间件：%sIDENTICAL ✓%s\n' "$C_K" "$C_R"
    head -12 /tmp/direct.json
  else
    warn "两条路径输出不同："
    diff /tmp/direct.json /tmp/mw.json | head -20
  fi
else
  warn "跳过：og-grmp 未配置或端口不是 ${PORT}。改用 GRMP_DEMO_PORT=8769 重跑，或改 config.yaml"
fi
}

printf '\n%s完成。服务将在退出时自动停止。%s\n' "$C_H" "$C_R"
