#!/usr/bin/env bash
# Mac 本地验证:让 Pod 打到的中间件 mock 开着签名校验(客户 2026-09-18 加固)。
#   1. 测试密钥对放 ~/kf-verify/.grmp-sm2/{private.hex,public.hex}(0600;没有就生成)
#   2. 起 grmp_mock 8782(--appkey gaussdb-agent --sm2-public-key),把 8781 的转发指向它(Pod 仍打 host.docker.internal:8781)
#   3. 用私钥创建 / 更新 Secret grmp-sm2(经 stdin,不打印)
# 之后 kubectl apply -k k8s/local(local 的 agent-config 带 GRMP_APPKEY)+ provision 即可;e2e-web.sh / verify-cluster.sh 照常。
set -euo pipefail
HERE=$(cd "$(dirname "$0")/../.." && pwd)
C=${CONTEXT:-orbstack}; NS=gaussdb-agent; PORT=8782; FWD=8781
PY=${PY:-$HOME/p2venv/bin/python3}; G=${GSDB_HOME_MOCK:-$HOME/kf-verify/gdaa}
KEYDIR=$HOME/kf-verify/.grmp-sm2; mkdir -p "$KEYDIR"; chmod 700 "$KEYDIR"
if [ ! -s "$KEYDIR/private.hex" ]; then
  (cd "$HERE/agent" && "$PY" -c 'from common.grmp import sm2; d=sm2.generate_private_key(); x,y=sm2.public_key(d); print("%064x" % d); print("%064x%064x" % (x,y))') \
    | { read -r priv; read -r pub; umask 077; printf '%s\n' "$priv" > "$KEYDIR/private.hex"; printf '%s\n' "$pub" > "$KEYDIR/public.hex"; }
  echo "生成测试密钥对 → $KEYDIR"
fi
set -a; . "$HOME/.gdaa/grmp.env"; set +a
pkill -f "grmp_mock --port $PORT" 2>/dev/null || true; pkill -f "tcp-forward.py $FWD" 2>/dev/null || true; sleep 1
# 背景进程一律从 /dev/null 读、输出进日志、disown:即使本脚本被 `| tail` 之类管道调用,
# 也绝不让子进程握住调用方的管道 fd 导致对端读不到 EOF。
(cd "$HERE/agent" && GSDB_HOME=$G nohup "$PY" -m grmp_middleware.grmp_mock --port $PORT --db "$G/grmp/script_config.db" --instances "$G/grmp/instances.yaml" \
    --appkey gaussdb-agent --sm2-public-key "$(cat "$KEYDIR/public.hex")" </dev/null > "$HOME/kf-verify/mock$PORT.log" 2>&1 &)
nohup "$PY" "$HERE/scripts/tcp-forward.py" $FWD $PORT </dev/null > "$HOME/kf-verify/tcp-forward-$FWD.log" 2>&1 &
disown 2>/dev/null || true
sleep 2; grep -m1 "签名校验" "$HOME/kf-verify/mock$PORT.log"
kubectl --context "$C" -n "$NS" create secret generic grmp-sm2 --from-file=GRMP_SM2_PRIVATE_KEY="$KEYDIR/private.hex" --dry-run=client -o yaml \
  | kubectl --context "$C" apply -f - >/dev/null && echo "Secret grmp-sm2 已就绪"
echo "mock $PORT(签名校验)← 转发 $FWD;Pod 打 host.docker.internal:$FWD 即经签名校验"
