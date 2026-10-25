#!/usr/bin/env bash
# 端到端(Web 路径):经 opencode serve 的 HTTP API——也就是浏览器界面用的同一套接口——在 runtime Pod 上
# 建会话、发提问、读回复。用真实模型(DeepSeek,密钥来自本机 0600 文件)与本地 mock 中间件。
# 用法: scripts/k8s/e2e-web.sh [工号]   前提: kubectl --context orbstack apply -k k8s/local 已做;转发器 8781 在跑。
set -u
U=${1:-u1001}; C=${CONTEXT:-orbstack}; NS=gaussdb-agent; PORT=${PORT:-14096}
HERE=$(cd "$(dirname "$0")/../.." && pwd)
K(){ kubectl --context "$C" -n "$NS" "$@"; }
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "[PASS] $1"; }
bad(){ fail=$((fail+1)); echo "[FAIL] $1"; echo "   | $(printf '%s' "${out:-}" | head -8 | tr '\n' ' ' | cut -c1-500)"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
has(){ printf '%s' "$out" | grep -q -- "$1"; }

# 0 模型密钥 → Secret model-api(从本机 opencode.deepseek.jsonc 取,只经 stdin 交给 kubectl,不打印)
python3 - "$HOME/.config/opencode/opencode.deepseek.jsonc" <<'EOF' | K apply -f - >/dev/null
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
key = cfg["provider"]["deepseek"]["options"]["apiKey"]
print("apiVersion: v1\nkind: Secret\nmetadata:\n  name: model-api\n  namespace: gaussdb-agent\ntype: Opaque\nstringData:\n  MODEL_API_KEY: %s" % json.dumps(key))
EOF
python3 "$HERE/scripts/k8s/provision.py" "$U" --auto-role >/dev/null || { echo "provision 失败"; exit 1; }
K rollout restart deploy "runtime-$U" >/dev/null; K rollout status deploy "runtime-$U" --timeout=180s >/dev/null
PW=$(K get secret "pod-auth-$U" -o jsonpath='{.data.OPENCODE_SERVER_PASSWORD}' | base64 -d)
K port-forward "svc/runtime-$U" "$PORT:4096" >/tmp/pf-$U.log 2>&1 & PF=$!
trap 'kill $PF 2>/dev/null' EXIT
sleep 3
api(){ curl -s -m "${3:-600}" -u "opencode:$PW" -H "Content-Type: application/json" "http://127.0.0.1:$PORT$1" ${2:+-d "$2"}; }

# 1 界面与认证
out=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/")
check "1a 不带口令访问界面 → 401"                                  '[ "$out" = "401" ]'
out=$(api /)
check "1b 带口令拿到 Web 界面(HTML)"                                'has "<title>OpenCode</title>"'
out=$(api /global/health)
check "1c /global/health healthy"                                  'has "\"healthy\":true"'

# 2 建会话,让模型登录并诊断(走 gaussdb-login + gaussdb-vacuum,经 mock 中间件)
SID=$(api /session '{"title":"e2e-web"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
check "2a 创建会话"                                                 '[ -n "$SID" ]'
out=$(api "/session/$SID/message" '{"parts":[{"type":"text","text":"先登录 10.0.0.9 的 postgres 库,然后评估一下死元组情况,简短回答。"}]}' 900)
reply=$(printf '%s' "$out" | python3 -c 'import json,sys
try:
    m=json.load(sys.stdin); parts=m.get("parts",[]); print("".join(p.get("text","") for p in parts if p.get("type")=="text"))
except Exception as e: print("PARSE-ERROR", e)')
out=$reply
check "2b 模型登录并给出死元组评估(回复提到会话/死元组/vacuum)"       'has "死元组" || has "vacuum" || has "autovacuum"'
echo "   ↳ $(printf '%s' "$reply" | tr '\n' ' ' | cut -c1-220)"

# 3 同一会话里要求导入工单:runtime 镜像没有导入 skill,应引导找管理员
out=$(api "/session/$SID/message" '{"parts":[{"type":"text","text":"我这里有份工单导出 /tmp/tickets.xlsx,帮我导入知识库。简短回答。"}]}' 600)
reply=$(printf '%s' "$out" | python3 -c 'import json,sys
try:
    m=json.load(sys.stdin); print("".join(p.get("text","") for p in m.get("parts",[]) if p.get("type")=="text"))
except Exception as e: print("PARSE-ERROR", e)')
out=$reply
check "3 导入请求 → 引导联系知识库管理员/导入环境"                    'has "管理员" || has "kb-import" || has "导入环境"'
echo "   ↳ $(printf '%s' "$reply" | tr '\n' ' ' | cut -c1-220)"

# 4 会话落在 NAS 的 opencode.db 里(Web 界面刷新后还在)
out=$(api /session | python3 -c 'import json,sys; s=json.load(sys.stdin); print(len(s), [x.get("title") for x in s][:3])')
check "4 会话列表里有这条会话(持久化在 NAS)"                          'has "e2e-web"'

echo "== 汇总: PASS $pass, FAIL $fail"
echo "浏览器入口: kubectl --context $C -n $NS port-forward svc/runtime-$U $PORT:4096 → http://localhost:$PORT  用户 opencode,口令: kubectl --context $C -n $NS get secret pod-auth-$U -o jsonpath='{.data.OPENCODE_SERVER_PASSWORD}' | base64 -d"
[ $fail -eq 0 ]
