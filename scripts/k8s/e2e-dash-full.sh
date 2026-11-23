#!/usr/bin/env bash
# 四个大盘的全链路验收(本地集群):
#   ① 一次性工号登录(NAS 目录先清空,结果不受上一轮影响)
#   ② 会话里让模型在两个实例上跑五个技能:健康检查、Top SQL(两个维度)、SQL 调优、WDR、知识库
#   ③ 经网关逐个核对存档:每个技能、每个实例都有,targets.json 带可读实例名
#   ④ 经「模拟 SSO」用真浏览器验四个大盘:区块、坏值、控制台、失败请求、切实例、页签、手机宽度、深挖
#
# 用一次性工号(默认 u9400)。**不要用 u9001–u9003 / u9100 / u9300,那是 user 的测试环境。**
# KEEP=1:跑完不删工号与 port-forward,模拟 SSO 留在 $SSO 端口,给人接着手测。
# 前提:gateway / frontend 已滚到待测镜像;gateway-config 的 USER_IMAGE_TAG 指向待测用户镜像;中间件 mock 可达。
set -euo pipefail
NS=gaussdb-agent; CTX=${CTX:-orbstack}; U=${U:-u9400}; PORT=${PORT:-18089}; SSO=${SSO:-18090}
IP1=${IP1:-10.0.0.9}; IP2=${IP2:-10.0.0.12}; DB=${DB:-postgres}
NAS_ROOT=${NAS_ROOT:-/Users/Shared/agent-nas-k8s}
HERE=$(cd "$(dirname "$0")/../.." && pwd)
K="kubectl --context $CTX -n $NS"
WORK=$(mktemp -d /tmp/e2e-dash-full.XXXXXX)
U2=${U2:-u9401}; SSO2=${SSO2:-18091}
for x in "$U" "$U2"; do case "$x" in u9001|u9002|u9003|u9100|u9300) echo "拒绝:$x 是 user 的测试工号" >&2; exit 2 ;; esac; done

TAG=$($K get cm gateway-config -o jsonpath='{.data.USER_IMAGE_TAG}')
docker image inspect "gaussdb-agent-runtime:$TAG" >/dev/null 2>&1 \
  || { echo "镜像 gaussdb-agent-runtime:${TAG} 本机没有(多半被 kubelet 回收了)。先 scripts/build-images.sh ${TAG}。" >&2; exit 2; }
TRUST=$($K get secret gateway-auth -o jsonpath='{.data.TRUST_SECRET}' | base64 -d)
printf '%s' "$TRUST" > "$WORK/trust"; chmod 600 "$WORK/trust"

echo "⓪ 清掉 $U 的旧环境与 NAS 目录"
$K delete deploy,svc runtime-$U --ignore-not-found --wait=true >/dev/null 2>&1 || true
$K delete secret pod-auth-$U --ignore-not-found >/dev/null 2>&1 || true
# 等旧容器真正退出再清 NAS:它退出前要做最终回写,目录先没了它会以 Error 退出(2026-09-23 撞过)
for _ in $(seq 1 60); do $K get pod -o name 2>/dev/null | grep -q "runtime-$U-" || break; sleep 2; done
rm -rf "${NAS_ROOT:?}/users/$U"
$K create secret generic grmp-token-$U --from-literal=GRMP_AUTH_TOKEN="${GRMP_AUTH_TOKEN:-e2e}" \
   --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null

cleanup() {
  if [ "${KEEP:-0}" = 1 ]; then
    echo; echo "KEEP=1:环境留着。浏览器打开 http://127.0.0.1:$SSO/dash/health (工号 $U)"
    echo "      结束后:kill $PF $SP; $K delete deploy,svc runtime-$U; $K delete secret grmp-token-$U pod-auth-$U"
    return
  fi
  kill "${PF:-0}" "${SP:-0}" "${SP2:-0}" 2>/dev/null || true
  $K delete deploy,svc runtime-$U runtime-$U2 --ignore-not-found >/dev/null 2>&1 || true
  $K delete secret grmp-token-$U pod-auth-$U grmp-token-$U2 pod-auth-$U2 --ignore-not-found >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
for p in "$PORT" "$SSO" "$SSO2"; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then echo "端口 $p 已被占用(上一轮没清干净?):" >&2; lsof -nP -iTCP:"$p" -sTCP:LISTEN >&2; exit 2; fi
done
$K port-forward svc/gateway $PORT:80 >/dev/null 2>&1 & PF=$!
python3 "$HERE/k8s/tools/sso-sim.py" "$SSO" "$PORT" "$U" "$WORK/trust" >/dev/null 2>&1 & SP=$!
sleep 3
B="http://127.0.0.1:$SSO"          # 之后的请求都经模拟 SSO,和浏览器走同一条路

echo "① 登录(首次建环境 10–30 秒)"
code=000
for _ in $(seq 1 36); do
  code=$(curl -s -m 30 -o "$WORK/login.txt" -w "%{http_code}" "$B/global/health" || true)
  [ "$code" = 200 ] && break; sleep 5
done
[ "$code" = 200 ] || { echo "   3 分钟内没登录上:HTTP $code $(head -c 300 "$WORK/login.txt")" >&2; exit 1; }
echo "   已就绪"

echo "①b 被闲置回收后再登录:拉起的容器不能马上又被判成闲置(2026-09-24 现场)"
$K annotate deploy runtime-$U "gaussdb-agent/last-activity=$(( $(date +%s) - 99999 ))" --overwrite >/dev/null
$K scale deploy runtime-$U --replicas=0 >/dev/null
for _ in $(seq 1 60); do [ -z "$($K get pod -l user=$U -o name 2>/dev/null)" ] && break; sleep 2; done
t0=$(date +%s)
code=$(curl -s -m 150 -o /dev/null -w "%{http_code}" "$B/global/health" || true)
act=$($K get deploy runtime-$U -o jsonpath='{.metadata.annotations.gaussdb-agent/last-activity}')
rep=$($K get deploy runtime-$U -o jsonpath='{.spec.replicas}')
if [ "$code" = 200 ] && [ "$rep" = 1 ] && [ -n "$act" ] && [ "$act" -ge $(( t0 - 5 )) ]; then
  echo "   ✓ 重连 200,副本 1,活动时间已刷新"
else
  echo "   ✗ 重连后 HTTP ${code}、副本 ${rep}、活动时间「${act}」(应 ≥ ${t0})—— 回收器下一轮会把它当成闲置" >&2; exit 1
fi

session() { curl -s -m 60 -H 'Content-Type: application/json' -d "{\"title\":\"$1\"}" "$B/session" \
            | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])'; }
say() {   # say <会话> <话>:同步等模型说完,打印它最后一段话的开头
  local body; body=$(python3 -c 'import json,sys;print(json.dumps({"parts":[{"type":"text","text":sys.argv[1]}]}))' "$2")
  curl -s -m 900 -H 'Content-Type: application/json' -d "$body" "$B/session/$1/message" \
    | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); t=[p.get("text","") for p in d.get("parts",[]) if p.get("type")=="text"]
    print("   模型:", (t[-1] if t else "(无文本)").replace("\n"," ")[:150])
except Exception as e: print("   模型回复解析失败:", e)'
}
NOASK="不要向我提问,遇到选择按默认做。每步完成后一句话汇报。"

echo "② 实例一 $IP1/$DB:健康检查、Top SQL、SQL 调优、WDR"
S1=$(session "e2e-dash-1")
say "$S1" "登录 $IP1 的 $DB 库,然后做一次健康检查。$NOASK"
say "$S1" "取 Top SQL:按总耗时取一次,再按平均耗时取一次。$NOASK"
say "$S1" "对按总耗时排第一的那条 SQL 做一次 SQL 调优(用它的 sql_id)。如果它按策略被跳过,再对榜上第一条访问业务表(不是系统视图、不是监控 SQL)的语句做一次调优。$NOASK"
say "$S1" "列出 WDR 快照,对最近的一个窗口做 WDR 分析,并生成原生 WDR 报告。快照不够就先手动打两个快照再分析。$NOASK"
echo "   实例二 $IP2/$DB:健康检查、Top SQL"
S2=$(session "e2e-dash-2")
say "$S2" "登录 $IP2 的 $DB 库,做一次健康检查,再按总耗时取一次 Top SQL。$NOASK"
echo "   知识库"
S3=$(session "e2e-dash-3")
say "$S3" "知识库里有没有 CPU 相关的案例?再查一下知识库的健康状态。$NOASK"

echo "③ 经网关核对存档"
fetch() { curl -s -o "$WORK/$2" -w "%{http_code}" "$B/reports/$1"; }
fail=0
check() { if eval "$2"; then echo "   ✓ $1"; else echo "   ✗ $1"; fail=1; fi; }
for s in health topsql wdr sqltune; do fetch "$s/targets.json" "$s-targets.json" >/dev/null || true; done
python3 - "$WORK" "$IP1" "$IP2" "$DB" <<'EOF' || fail=1
import json, pathlib, sys
w, ip1, ip2, db = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
def targets(s):
    try: return json.loads((w / f"{s}-targets.json").read_text())
    except Exception: return []
ok = True
def need(cond, msg):
    global ok
    print("   %s %s" % ("✓" if cond else "✗", msg)); ok = ok and cond
h = targets("health")
labels = {t.get("label") for t in h}
need(f"{ip1} / {db}" in labels and f"{ip2} / {db}" in labels, f"健康检查两个实例都有,且是可读名:{sorted(labels)}")
need(all(t.get("login", "").startswith("登录 ") for t in h), "每个实例都带登录说法(深挖首句用)")
t = targets("topsql"); need(any(x.get("label") == f"{ip1} / {db}" for x in t), f"Top SQL 实例一有存档:{[x.get('label') for x in t]}")
need(any(x.get("label") == f"{ip2} / {db}" for x in t), "Top SQL 实例二有存档")
need(bool(targets("sqltune")), f"SQL 调优有存档:{[x.get('label') for x in targets('sqltune')]}")
need(bool(targets("wdr")), f"WDR 有存档:{[x.get('label') for x in targets('wdr')]}")
for s in ("health", "topsql", "wdr"):
    for x in targets(s):
        (w / f"{s}-{x['key']}.key").write_text(x["key"])
sys.exit(0 if ok else 1)
EOF
K1=$(python3 -c 'import json,sys;print([t["key"] for t in json.load(open(sys.argv[1])) if t.get("label","").startswith(sys.argv[2]+" ")][0])' "$WORK/topsql-targets.json" "$IP1" 2>/dev/null || echo "")
if [ -n "$K1" ]; then
  fetch "topsql/$K1/index.json" ts-index.json >/dev/null
  check "Top SQL 实例一有两个维度(time 与 avg)" 'python3 -c "import json,sys;f=[e[\"file\"] for e in json.load(open(\"$WORK/ts-index.json\"))];sys.exit(0 if any(\".time.\" in x for x in f) and any(\".avg.\" in x for x in f) else 1)"'
  fetch "wdr/$K1/latest.json" wdr-latest.json >/dev/null || true
  check "WDR 最近一份带原生报告" 'python3 -c "import json,sys;d=json.load(open(\"$WORK/wdr-latest.json\"));n=d.get(\"native\") or {};print(\"     native:\",{k:n.get(k) for k in (\"generated\",\"bytes\",\"note\")});sys.exit(0 if n.get(\"generated\") else 1)"'
fi
for kf in "$WORK"/health-*.key; do
  [ -e "$kf" ] || continue; k=$(cat "$kf")
  fetch "health/$k/latest.json" "h-$k.json" >/dev/null
  check "健康检查存档($k)里没有中间件地址与接口路径(红线第 5 条)" '! grep -Eq "https?://|/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+" "$WORK/h-$k.json"'
done
check "知识库健康存档 200" '[ "$(fetch kb/health.json kb-health.json)" = 200 ]'
check "知识库目录(案例/条款/关系图页签)200,且读到了案例与条款、没有收件目录" '[ "$(fetch _kb/catalog.json kb-cat.json)" = 200 ] && python3 -c "import json,sys;d=json.load(open(\"$WORK/kb-cat.json\"));n=(len(d[\"cases\"]),sum(len(g[\"rules\"]) for g in d[\"groups\"]),len(d[\"edges\"]));print(\"     案例/条款/边:\",n);sys.exit(0 if d.get(\"attached\") and n[0] and n[1] and \"inbox/\" not in json.dumps(d) else 1)"'
check "知识库检索日志有本人的查询" '[ "$(fetch kb/queries.jsonl kb-q.jsonl)" = 200 ] && [ -s "$WORK/kb-q.jsonl" ]'

echo "④ 真浏览器:四个大盘 + 深挖"
BASE="$B" OUT="${OUT:-/tmp/dash-shots}" DIG=1 node "$HERE/scripts/k8s/dash-browser-check.mjs" || fail=1

echo "⑤ 新工号 $U2:什么都没跑过,四个大盘应是空状态 + 引导"
$K delete deploy,svc runtime-$U2 --ignore-not-found --wait=true >/dev/null 2>&1 || true
rm -rf "${NAS_ROOT:?}/users/$U2"
$K create secret generic grmp-token-$U2 --from-literal=GRMP_AUTH_TOKEN="${GRMP_AUTH_TOKEN:-e2e}" \
   --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
python3 "$HERE/k8s/tools/sso-sim.py" "$SSO2" "$PORT" "$U2" "$WORK/trust" >/dev/null 2>&1 & SP2=$!
sleep 2; B2="http://127.0.0.1:$SSO2"
for _ in $(seq 1 36); do [ "$(curl -s -m 30 -o /dev/null -w "%{http_code}" "$B2/global/health" || true)" = 200 ] && break; sleep 5; done
BASE="$B2" OUT="${OUT:-/tmp/dash-shots}" EMPTY=1 node "$HERE/scripts/k8s/dash-browser-check.mjs" || fail=1

[ "$fail" = 0 ] && echo "ALL OK" || { echo "有检查没过(见上面的 ✗)" >&2; exit 1; }
