#!/usr/bin/env bash
# spec §12 的六项集群验证(OrbStack Kubernetes,上下文 orbstack)。
# 前提:k8s/local 已 apply;provision.py 已建 u1001 / u1002(普通用户)与 u2001(知识库管理员);
#       转发器 scripts/tcp-forward.py 8781 8779 在跑;~/kf-verify/kf_session_e2e.sh 存在(第 6 项复制进 Pod)。
set -u
C=${CONTEXT:-orbstack}; NS=gaussdb-agent; NAS=${NAS:-/Users/Shared/agent-nas-k8s}
HERE=$(cd "$(dirname "$0")/../.." && pwd)
K(){ kubectl --context "$C" -n "$NS" "$@"; }
S=/opt/agent/config/opencode/skills
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "[PASS] $1"; }
bad(){ fail=$((fail+1)); echo "[FAIL] $1"; echo "   | $(printf '%s' "${out:-}" | head -6 | tr '\n' ' ' | cut -c1-400)"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
has(){ printf '%s' "$out" | grep -q -- "$1"; }
pod(){ K get pod -l "app=gaussdb-agent,role=$1,user=$2" --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}'; }
X(){ K exec "$1" -- bash -c ". /data/env.sh; $2" 2>&1; }
ready(){ for _ in $(seq 1 60); do [ "$(K get deploy "$1" -o jsonpath='{.status.availableReplicas}' 2>/dev/null)" = "1" ] && return 0; sleep 2; done; return 1; }

A=$(pod runtime u1001); B=$(pod runtime u1002); ADM=$(pod kb-import u2001)
echo "Pod: A=$A B=$B ADM=$ADM"

# 1 隔离
out=$(X "$A" "python3 $S/gaussdb-login/scripts/login.py --ip 10.0.0.9 --database postgres")
HA=$(printf '%s' "$out" | grep -oE -- '--session [a-z0-9]{12}' | head -1 | awk '{print $2}')
check "1a A 登录得到句柄"                                          '[ -n "$HA" ]'
out=$(X "$A" "echo A-private > /nas/me/workspace/a.txt; python3 $S/gaussdb-health/scripts/health.py --session $HA | head -3")
check "1b A 带句柄跑 health,报告写着执行人 u1001"                   'has "Health Evidence" && has "执行人：u1001"'
out=$(X "$B" "python3 $S/gaussdb-login/scripts/login.py --status; ls /nas/me/workspace; ls /nas/me/gdaa/sessions 2>&1")
check "1c B 的 Pod 里看不到 A 的会话、文件、句柄"                    'has "本对话未登录" && ! has "a.txt" && ! has "$HA"'
out=$(X "$B" "python3 $S/gaussdb-health/scripts/health.py --session $HA 2>&1; echo rc=\$?")
check "1d B 拿着 A 的句柄也用不了(不同 NAS 子目录)"                  'has "不存在或已过期" && ! has "Health Evidence"'

# 2 无状态:删 Pod,重建后状态都在;.owner 换成新 Pod
OLD=$A
K delete pod "$OLD" --wait=true >/dev/null 2>&1; ready runtime-u1001; A=$(pod runtime u1001)
out=$(X "$A" "cat /nas/me/workspace/a.txt; python3 $S/gaussdb-login/scripts/login.py --status --session $HA | head -5; ls /nas/me/backup; cat /nas/me/.owner")
check "2a 重建后上传文件、登录会话、退出备份都在,.owner 归新 Pod"       'has "A-private" && has "$HA" && has "opencode.db.20" && has "$A" && ! has "$OLD"'
out=$(K logs "$A" 2>&1 | head -8)
check "2b 新 Pod 启动日志:独占标记、自检通过"                          'has "独占标记" && has "完整性检查通过"'

# 3 知识库流转:管理员 Pod 写入 → runtime 立刻可查
out=$(X "$ADM" "printf -- '- id: GS-K8S-001\n  severity: warn\n  check: advisory\n  rule: k8s 流转验证条款 cluster-smoke-token\n' > /nas/kb/rules/k8s-smoke.yaml; python3 $S/gaussdb-kb-import/scripts/kb.py index --kb /nas/kb | tail -1; ls /nas/kb/.lock 2>&1")
check "3a kb-import Pod 写入条款并重建清单,锁已释放"                   'has "已重建" && has "No such file"'
out=$(X "$A" "python3 $S/gaussdb-kb/scripts/kb.py search cluster-smoke-token")
check "3b runtime Pod 下一次 search 就能命中"                          'has "k8s-smoke.yaml" && has "cluster-smoke-token"'
# 验证条款的 ID 不合 GS-<域>-NNN 格式,留下会让之后的 validate 报 error——验完即删并重建清单
X "$ADM" "rm -f /nas/kb/rules/k8s-smoke.yaml; python3 $S/gaussdb-kb-import/scripts/kb.py index --kb /nas/kb >/dev/null 2>&1" >/dev/null

# 4 只读
out=$(X "$A" "touch /nas/kb/probe 2>&1; echo rc=\$?; python3 $S/gaussdb-kb/scripts/kb.py health | grep 只读")
check "4 runtime 对 /nas/kb 写失败,health 标明只读"                   'has "rc=1" && has "知识库只读"'

# 5 自检恢复:优雅停机留备份 → 写坏 db → 起来后从备份恢复
K scale deploy runtime-u1001 --replicas=0 >/dev/null; sleep 12
printf 'garbage%.0s' $(seq 1 2000) > "$NAS/users/u1001/xdg-data/opencode/opencode.db"
K scale deploy runtime-u1001 --replicas=1 >/dev/null; ready runtime-u1001; A=$(pod runtime u1001)
out=$(K logs "$A" 2>&1 | head -8)
check "5 启动自检发现损坏并从备份恢复(日志写明)"                      'has "损坏" && has "已从备份"'

# 6 会话 e2e 在 Pod 内重跑(20 例,含地址变更 S19)
sed -e 's#^export GSDB_HOME=.*#export GSDB_HOME=/nas/me/gdaa#' -e 's#^S=.*#S=/opt/agent/config/opencode/skills#' \
    -e 's#^PY=.*#PY=python3#' -e 's#^set -a; \. "\$HOME/.gdaa/grmp.env"; set +a#true#' -e 's#^export PATH=.*#true#' \
    "$HOME/kf-verify/kf_session_e2e.sh" > /tmp/kf_session_e2e_pod.sh
K cp /tmp/kf_session_e2e_pod.sh "$A:/tmp/kf_session_e2e_pod.sh" >/dev/null
out=$(X "$A" "bash /tmp/kf_session_e2e_pod.sh 2>&1 | tail -4")
check "6 会话 e2e 20/20 在 Pod 内通过"                                'has "PASS 20, FAIL 0"'

# 7 NetworkPolicy 生效:OrbStack 的 CNI 会强制执行——放行的 8781 通,没放行的 8779 不通
out=$(K apply --dry-run=server -f "$HERE/k8s/base/networkpolicy.yaml" 2>&1)
check "7a base 的 NetworkPolicy 清单通过服务端校验"                    'has "unchanged" || has "configured" || has "created"'
out=$(X "$A" 'curl -s -m 5 -o /dev/null -w "allowed=%{http_code} " http://host.docker.internal:8781/; curl -s -m 5 -o /dev/null -w "blocked=%{http_code}" http://host.docker.internal:8779/')
check "7b 出站策略生效:放行端口可达、未放行端口被挡"                   'has "allowed=404" && has "blocked=000"'

echo "== 汇总: PASS $pass, FAIL $fail"
[ $fail -eq 0 ]
