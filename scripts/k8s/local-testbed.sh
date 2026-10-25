#!/usr/bin/env bash
# Mac 本地测试床:把容器版跑起来,供人用浏览器和 opencode CLI 手工验证。
#
#   scripts/k8s/local-testbed.sh [--tag agent-v0.7-oc1.18.27]   搭建并打印接入方式
#   scripts/k8s/local-testbed.sh --down                          拆掉(NAS 数据保留)
#   scripts/k8s/local-testbed.sh --info                          只重新打印接入方式
#
# 搭出来的东西:
#   - 开了签名校验的 GRMP mock(8782)+ 转发 8781,Pod 经宿主网关访问它
#   - 两个用户:普通用户(只有 runtime)与知识库管理员(runtime + kb-import)
#   - 两个 port-forward,浏览器和 CLI 都从本机 127.0.0.1 接入
# 与客户环境的差别:中间件是 mock、模型是 DeepSeek、NAS 是 hostPath、网关的活由这个脚本代劳。
set -uo pipefail
HERE=$(cd "$(dirname "$0")/../.." && pwd)
C=${CONTEXT:-orbstack}; NS=gaussdb-agent
TAG=${TAG:-agent-v0.7-oc1.18.27}
USER_NORMAL=${USER_NORMAL:-u9001}; PORT_NORMAL=${PORT_NORMAL:-14096}
USER_ADMIN=${USER_ADMIN:-u9002};   PORT_ADMIN=${PORT_ADMIN:-14098}
NAS=${NAS:-/Users/Shared/agent-nas-k8s}
PY=${PY:-$HOME/p2venv/bin/python3}
KEYFILE=$HOME/.config/opencode/opencode.deepseek.jsonc
LOGDIR=$HOME/kf-verify; mkdir -p "$LOGDIR"
K(){ kubectl --context "$C" -n "$NS" "$@"; }
# /data/state/workspace 的 base64url —— opencode Web 界面的项目路由(状态本地化后工作目录在本地盘)
PROJ=L2RhdGEvc3RhdGUvd29ya3NwYWNl

info() {
  local pw_n pw_a
  pw_n=$(K get secret "pod-auth-$USER_NORMAL" -o jsonpath='{.data.OPENCODE_SERVER_PASSWORD}' 2>/dev/null | base64 -d)
  pw_a=$(K get secret "pod-auth-$USER_ADMIN"  -o jsonpath='{.data.OPENCODE_SERVER_PASSWORD}' 2>/dev/null | base64 -d)
  cat <<TXT

╔══════════════════════════════════════════════════════════════════════════╗
║  接入方式(用户名一律 opencode)                                           ║
╚══════════════════════════════════════════════════════════════════════════╝

【普通用户 ${USER_NORMAL}】18 个诊断 skill + 知识库只读

  浏览器   http://127.0.0.1:$PORT_NORMAL/$PROJ
  口令     $pw_n
  CLI      OPENCODE_SERVER_PASSWORD='$pw_n' \\
             opencode attach http://127.0.0.1:$PORT_NORMAL --dir /data/state/workspace

【知识库管理员 ${USER_ADMIN}】另有导入与入库前标准化

  浏览器   http://127.0.0.1:$PORT_ADMIN/$PROJ
  口令     $pw_a
  CLI      OPENCODE_SERVER_PASSWORD='$pw_a' \\
             opencode attach http://127.0.0.1:$PORT_ADMIN --dir /data/state/workspace

  注:浏览器别落在 / ——那页的项目列表是浏览器本地记的,新浏览器为空。上面给的是项目路由。
     管理员的收件目录(放待导入材料):$NAS/kb/inbox/uploads/

【可以试的】
  普通用户  "登录 10.0.0.9 的 postgres 库,评估一下死元组情况"
            "查一下知识库里有没有 autovacuum 相关的案例"
            "帮我导入一份工单"            ← 应答复「本环境不含导入功能,请联系管理员」
  管理员    "把收件目录里的材料标准化"     ← 走 kb-init:scan → 看图/读文本 → render
            "把标准化好的导入知识库"       ← 走 kb-import:ingest → propose → 选择列表 → apply

【看日志】
  Pod       kubectl --context $C -n $NS logs -f deploy/runtime-$USER_NORMAL
  中间件    tail -f $LOGDIR/mock8782.log
  拆环境    scripts/k8s/local-testbed.sh --down
TXT
}

down() {
  echo "== 拆环境(NAS 数据保留在 $NAS)"
  pkill -f "port-forward svc/runtime-$USER_NORMAL" 2>/dev/null
  pkill -f "port-forward svc/kb-import-$USER_ADMIN" 2>/dev/null
  pkill -f "grmp_mock --port 8782" 2>/dev/null
  pkill -f "tcp-forward.py 8781" 2>/dev/null
  for u in "$USER_NORMAL" "$USER_ADMIN"; do "$PY" "$HERE/scripts/k8s/provision.py" "$u" --delete 2>&1 | tail -1; done
  echo "已拆。镜像与 NAS 数据还在,再跑一次本脚本即可恢复。"
}

case "${1:-}" in
  --down) down; exit 0 ;;
  --info) info; exit 0 ;;
  --tag)  TAG=${2:?}; ;;
esac

echo "== 0 前置检查"
command -v kubectl >/dev/null || { echo "没有 kubectl"; exit 1; }
kubectl --context "$C" get ns >/dev/null 2>&1 || { echo "连不上 $C 集群(OrbStack Kubernetes 开了吗)"; exit 1; }
docker image inspect "gaussdb-agent-runtime:$TAG" >/dev/null 2>&1 || { echo "本地没有镜像 gaussdb-agent-runtime:$TAG —— 先 scripts/build-images.sh $TAG"; exit 1; }
[ -f "$KEYFILE" ] || { echo "没有模型密钥文件 $KEYFILE"; exit 1; }
[ -f "$HOME/.gdaa/grmp.env" ] || { echo "没有 ~/.gdaa/grmp.env(mock 的 auth 令牌)"; exit 1; }
echo "   镜像 $TAG · 集群 $C · NAS $NAS"

echo "== 1 NAS 目录与知识库"
mkdir -p "$NAS/users/$USER_NORMAL" "$NAS/users/$USER_ADMIN" "$NAS/admins/$USER_ADMIN" "$NAS/kb/inbox/uploads"
if [ ! -d "$NAS/kb/cases" ]; then
  [ -d "$HOME/kb-verify/modes/kb" ] && cp -R "$HOME/kb-verify/modes/kb/." "$NAS/kb/" && echo "   知识库已放入(来自 ~/kb-verify/modes/kb)"
fi
chmod -R a+rwX "$NAS" 2>/dev/null
echo "   知识库:$(ls "$NAS/kb/cases" 2>/dev/null | wc -l | tr -d ' ') 个案例,$(ls "$NAS/kb/rules" 2>/dev/null | wc -l | tr -d ' ') 个条款文件"

echo "== 2 开了签名校验的中间件 mock(8782)+ 转发(8781)+ Secret grmp-sm2"
bash "$HERE/scripts/k8s/local-grmp-signing.sh" > "$LOGDIR/testbed-signing.out" 2>&1 \
  && tail -2 "$LOGDIR/testbed-signing.out" | sed 's/^/   /' || { echo "   起 mock 失败,看 $LOGDIR/testbed-signing.out"; exit 1; }

echo "== 3 清单与模型密钥"
kubectl --context "$C" apply -k "$HERE/k8s/local" >/dev/null && echo "   k8s/local 已应用"
"$PY" - "$KEYFILE" <<'PY' | K apply -f - >/dev/null && echo "   Secret model-api 已更新(密钥不打印)"
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
key = cfg["provider"]["deepseek"]["options"]["apiKey"]
print("apiVersion: v1\nkind: Secret\nmetadata:\n  name: model-api\n  namespace: gaussdb-agent\ntype: Opaque\nstringData:\n  MODEL_API_KEY: %s" % json.dumps(key))
PY

echo "== 4 供给两个用户的 Pod(网关在客户环境干的事)"
"$PY" "$HERE/scripts/k8s/provision.py" "$USER_NORMAL" --image-tag "$TAG" 2>&1 | sed 's/^/   /'
"$PY" "$HERE/scripts/k8s/provision.py" "$USER_ADMIN" --kb-admin --image-tag "$TAG" 2>&1 | sed 's/^/   /'

echo "== 5 端口转发"
pkill -f "port-forward svc/runtime-$USER_NORMAL" 2>/dev/null; pkill -f "port-forward svc/kb-import-$USER_ADMIN" 2>/dev/null
sleep 1
nohup kubectl --context "$C" -n "$NS" port-forward "svc/runtime-$USER_NORMAL" "$PORT_NORMAL:4096" </dev/null > "$LOGDIR/pf-$USER_NORMAL.log" 2>&1 &
nohup kubectl --context "$C" -n "$NS" port-forward "svc/kb-import-$USER_ADMIN" "$PORT_ADMIN:4096" </dev/null > "$LOGDIR/pf-$USER_ADMIN.log" 2>&1 &
disown 2>/dev/null || true
sleep 3

echo "== 6 自检"
ok=0
for spec in "$USER_NORMAL:$PORT_NORMAL" "$USER_ADMIN:$PORT_ADMIN"; do
  u=${spec%%:*}; p=${spec##*:}
  pw=$(K get secret "pod-auth-$u" -o jsonpath='{.data.OPENCODE_SERVER_PASSWORD}' | base64 -d)
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 5 "http://127.0.0.1:$p/")
  health=$(curl -s -m 5 -u "opencode:$pw" "http://127.0.0.1:$p/global/health")
  printf "   %-8s 不带口令=%s(应 401) · health=%s\n" "$u" "$code" "$(printf '%s' "$health" | cut -c1-32)"
  [ "$code" = "401" ] && printf '%s' "$health" | grep -q '"healthy":true' && ok=$((ok+1))
done
[ "$ok" = "2" ] && echo "   两个 Pod 都就绪" || { echo "   ⚠ 有 Pod 没起来,看 kubectl -n $NS get pod"; }

info
