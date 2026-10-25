#!/usr/bin/env bash
# 报告存档端到端:新工号登录 → 会话里跑健康检查 → 经网关 GET /reports/health/latest.json 得到 200 且形状对。
#
# 用一次性工号(默认 u9400),跑完删掉 —— **不要用 u9001–u9003 / u9100 / u9300,那是 user 的测试环境。**
# 前提:网关已滚到含三路分发的版本;gateway-config 的 USER_IMAGE_TAG 指向含 serve-reports 的镜像;
#       中间件 mock 可达(否则第 ③ 步的健康检查跑不出报告,第 ④ 步会超时)。
set -euo pipefail
NS=gaussdb-agent; CTX=${CTX:-orbstack}; U=${U:-u9400}; PORT=${PORT:-18081}
K="kubectl --context $CTX -n $NS"
# 镜像预检:kubelet 会回收没有 Pod 引用的镜像(本地集群一天里发生了四次)。不预检的话,
# 症状是网关等 120 秒后 504「正在启动」,要翻 Pod 事件才看到 ImagePullBackOff。
TAG=$($K get cm gateway-config -o jsonpath='{.data.USER_IMAGE_TAG}')
if ! docker image inspect "gaussdb-agent-runtime:$TAG" >/dev/null 2>&1; then
  echo "镜像 gaussdb-agent-runtime:$TAG 本机没有(多半被 kubelet 回收了)。先 scripts/build-images.sh $TAG 再跑。" >&2
  exit 2
fi
TRUST=$($K get secret gateway-auth -o jsonpath='{.data.TRUST_SECRET}' | base64 -d)
$K create secret generic grmp-token-$U --from-literal=GRMP_AUTH_TOKEN="${GRMP_AUTH_TOKEN:-e2e}" \
   --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
cleanup() {
  kill "${PF:-0}" 2>/dev/null || true
  $K delete deploy,svc runtime-$U --ignore-not-found >/dev/null 2>&1 || true
  $K delete secret grmp-token-$U pod-auth-$U --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT
$K port-forward svc/gateway $PORT:80 >/dev/null 2>&1 & PF=$!
sleep 3
H=(-H "X-Agent-Trust: $TRUST" -H "X-Agent-User: $U")
B="http://127.0.0.1:$PORT"

echo "① 建环境(首次 10–30 秒)"
curl -s -o /dev/null -w "%{http_code}\n" "${H[@]}" "$B/global/health" | grep -qx 200

echo "② 报告端口通(不存在的文件 → 404),记下现有份数"
# NAS 上可能留着上一轮的报告(回收/重建不动 NAS 是设计如此),所以不能断言 latest.json 不存在;
# 用一个肯定不存在的文件证明路由到了 4097,再记下 index 里现有的份数,④ 看它有没有**增加**。
curl -s -o /dev/null -w "%{http_code}\n" "${H[@]}" "$B/reports/health/no-such-file.json" | grep -qx 404
# 报告按实例分目录:总份数从 targets.json 的 count 求和(跑之前不知道实例键)
count_all() { curl -s "${H[@]}" "$B/reports/health/targets.json" | python3 -c 'import json,sys
try: print(sum(int(t.get("count", 0)) for t in json.load(sys.stdin)))
except Exception: print(0)'; }
before=$(count_all)
echo "   现有报告 $before 份"

echo "③ 在会话里跑健康检查"
resp=$(curl -s -m 120 -w "\n%{http_code}" "${H[@]}" -H 'Content-Type: application/json' -d '{"title":"e2e-reports"}' "$B/session")
code=${resp##*$'\n'}; body=${resp%$'\n'*}
if [ "$code" != 200 ]; then
  echo "   POST /session → HTTP $code:$body" >&2
  echo "   网关日志:"; $K logs -l app=agent-gateway --tail=200 --timestamps 2>/dev/null | grep "$U" | tail -5 >&2
  exit 1
fi
SID=$(printf '%s' "$body" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
echo "   会话 $SID"
curl -s -m 900 "${H[@]}" -H 'Content-Type: application/json' \
     -d '{"parts":[{"type":"text","text":"先登录 10.0.0.9 的 postgres 库,然后做一次健康检查,简短回答。"}]}' \
     "$B/session/$SID/message" >/dev/null

echo "④ 报告到了(index 从 $before 份增加)"
ok=0
for _ in $(seq 1 30); do
  now=$(count_all)
  if [ "$now" -gt "$before" ]; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "   60 秒内 targets 份数没有增加;看 $K logs deploy/runtime-$U | grep -i '报告\\|serve-reports'"; exit 1; }
KEY=$(curl -s "${H[@]}" "$B/reports/health/targets.json" | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["key"])')
echo "   实例键 $KEY"
curl -s -o /tmp/e2e-latest.json -w "%{http_code}\n" "${H[@]}" "$B/reports/health/$KEY/latest.json" | grep -qx 200
python3 - <<'EOF'
import json
d = json.load(open("/tmp/e2e-latest.json"))
assert "dims" in d and "overall" in d and "sub_skills" in d, list(d)
print("   overall=%s dims=%d findings=%d" % (d["overall"], len(d["dims"]), len(d["findings"])))
EOF

echo "⑤ index 与越界"
curl -s "${H[@]}" "$B/reports/health/$KEY/index.json" | python3 -c 'import json,sys;i=json.load(sys.stdin);assert len(i)>=1;print("   index 条数",len(i))'
# --path-as-is:curl 默认会在客户端把 /reports/../x 规范化成 /x 再发,那样打到网关的就不是 reports 路由,
# 测的是 curl 不是我们的端口。第一版就是这么假红的。
code=$(curl -s --path-as-is -o /dev/null -w "%{http_code}" "${H[@]}" "$B/reports/../gdaa/config.yaml")
echo "   越界请求 /reports/../gdaa/config.yaml → $code"
[ "$code" = 404 ]

echo "⑥ /dash 经网关到大盘前端:页面 200 且是大盘、whoami 是本人"
curl -s -o /tmp/e2e-dash.html -w "%{http_code}\n" "${H[@]}" "$B/dash/health" | grep -qx 200
grep -q "lib/shell.js" /tmp/e2e-dash.html
curl -s -o /dev/null -w "%{http_code}\n" "${H[@]}" "$B/dash/lib/shell.js" | grep -qx 200
curl -s "${H[@]}" "$B/reports/whoami.json" | python3 -c "import json,sys;assert json.load(sys.stdin)['user_id']=='$U';print('   whoami = $U')"

echo "ALL OK"
