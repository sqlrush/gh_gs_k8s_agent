#!/usr/bin/env bash
# 端到端(模型级):在 kb-import Pod 里,让模型用 gaussdb-kb-init 把收件目录里的杂格式材料(docx + 照片)
# 标准化成六要素 md。真实模型(DeepSeek,密钥来自本机 0600 文件),不需要中间件。
# 用法: scripts/k8s/e2e-kb-init.sh [管理员工号] [材料目录]
#   材料目录默认 ~/gh_skill/prd/案例/word(5 份 docx)+ ~/kf-verify/cases-jpg/1/IMG_2357.jpg(1 张照片)
set -u
U=${1:-u2001}; C=${CONTEXT:-orbstack}; NS=gaussdb-agent; PORT=${PORT:-14098}
HERE=$(cd "$(dirname "$0")/../.." && pwd)
NAS=${NAS:-/Users/Shared/agent-nas-k8s}
K(){ kubectl --context "$C" -n "$NS" "$@"; }
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "[PASS] $1"; }
bad(){ fail=$((fail+1)); echo "[FAIL] $1"; echo "   | $(printf '%s' "${out:-}" | head -8 | tr '\n' ' ' | cut -c1-500)"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
has(){ printf '%s' "$out" | grep -q -- "$1"; }

# 0 材料进收件目录(网关在客户环境干的事,这里手工放)
# 4 份 docx(案例 1–4)+ 案例 5 的一张前言表照片:照片与 docx 不是同一案例。第一次跑时放的是案例 1 的照片,
# 模型看出它是案例 1 docx 的第一页、且 docx 开头注明正是从这些照片整理来的,于是停下来问「重复的怎么处理」——
# 行为正确(没有自作主张去重、也没编),但自动验收没人回答。材料不重复,验的才是「从照片抽六要素」本身。
UP=$NAS/kb/inbox/uploads/kbinit-e2e; rm -rf "$UP"; mkdir -p "$UP"
for n in 1 2 3 4; do cp "$HOME"/gh_skill/prd/案例/word/案例$n-*.docx "$UP"/ 2>/dev/null; done
cp "$HOME"/kf-verify/cases-jpg/5/IMG_2380.jpg "$UP"/ 2>/dev/null
chmod -R a+rwX "$UP"
N_DOCX=$(ls "$UP"/*.docx 2>/dev/null | wc -l | tr -d ' '); N_IMG=$(ls "$UP"/*.jpg 2>/dev/null | wc -l | tr -d ' ')
[ "$N_DOCX" -gt 0 ] || { echo "没有材料:$UP"; exit 1; }
rm -rf "$NAS/kb/init/kbinit-e2e"

# 1 模型密钥 → Secret;管理员 Pod
python3 - "$HOME/.config/opencode/opencode.deepseek.jsonc" <<'EOF' | K apply -f - >/dev/null
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
key = cfg["provider"]["deepseek"]["options"]["apiKey"]
print("apiVersion: v1\nkind: Secret\nmetadata:\n  name: model-api\n  namespace: gaussdb-agent\ntype: Opaque\nstringData:\n  MODEL_API_KEY: %s" % json.dumps(key))
EOF
python3 "$HERE/scripts/k8s/provision.py" "$U" --kb-admin >/dev/null || { echo "provision 失败"; exit 1; }
K rollout restart deploy "kb-import-$U" >/dev/null; K rollout status deploy "kb-import-$U" --timeout=180s >/dev/null
PW=$(K get secret "pod-auth-$U" -o jsonpath='{.data.OPENCODE_SERVER_PASSWORD}' | base64 -d)
K port-forward "svc/kb-import-$U" "$PORT:4096" >/tmp/pf-ki-$U.log 2>&1 & PF=$!
trap 'kill $PF 2>/dev/null' EXIT
sleep 3
api(){ curl -s -m "${3:-1200}" -u "opencode:$PW" -H "Content-Type: application/json" "http://127.0.0.1:$PORT$1" ${2:+-d "$2"}; }
reply(){ python3 -c 'import json,sys
try:
    m=json.load(sys.stdin); print("".join(p.get("text","") for p in m.get("parts",[]) if p.get("type")=="text"))
except Exception as e: print("PARSE-ERROR", e)'; }

out=$(api /global/health)
check "1 kb-import Pod 健康"                                       'has "\"healthy\":true"'
out=$(K exec "deploy/kb-import-$U" -- sh -c 'ls /opt/agent/config/opencode/skills | tr "\n" " "')
check "2 Pod 里有 gaussdb-kb-init(且没有诊断 skill)"                'has "gaussdb-kb-init" && ! has "gaussdb-health"'

# 3 让模型标准化:scan → 看图/读文本写草稿 → render
SID=$(api /session '{"title":"e2e-kb-init"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
START=$(date +%s)
out=$(api "/session/$SID/message" "{\"parts\":[{\"type\":\"text\",\"text\":\"收件目录 /nas/kb/inbox/uploads/kbinit-e2e 里有 $N_DOCX 份 word 问题分析报告和 $N_IMG 张照片,请用入库前标准化 skill 把它们整理成统一格式:scan 之后逐个读工作单写草稿(照片要用 read 工具看图),然后 render、validate。做到 render 通过为止,先不要 ingest 入库。这是自动验收,没有人回答提问:遇到要判断的地方按 SKILL.md 的保守规则处理(照片读不清或原文没写的段写「原文未写明」,conclusion 写推测),不要使用提问工具。最后简短汇报每一单的时间和系统。\"}]}" 1500)
DT=$(( $(date +%s) - START ))
reply=$(printf '%s' "$out" | reply); out=$reply
check "3 模型完成标准化并汇报(耗时 ${DT}s)"                          'has "现场" || has "render" || has "统一格式" || has "std"'
echo "   ↳ $(printf '%s' "$reply" | tr '\n' ' ' | cut -c1-300)"

# 4 落盘核对:批次目录、统一格式文件、校验、照片那一单的标记
B=$NAS/kb/init/kbinit-e2e
out=$(ls "$B/std" 2>/dev/null; echo "n=$(ls "$B/std"/*.md 2>/dev/null | wc -l | tr -d ' ')")
check "4a std/ 里有 $((N_DOCX + N_IMG)) 份统一格式文件"                 "has \"n=$((N_DOCX + N_IMG))\""
out=$(K exec "deploy/kb-import-$U" -- sh -c '. /data/env.sh; python3 /opt/agent/config/opencode/skills/gaussdb-kb-init/scripts/kb_init.py validate --batch kbinit-e2e --kb /nas/kb 2>&1; echo rc=$?')
check "4b validate 全部通过"                                        'has "0 份有问题" && has "rc=0"'
out=$(grep -l 'extracted_by: "model"' "$B"/std/*.md 2>/dev/null | wc -l | tr -d " ")
check "4c 照片那一单标了 extracted_by: model"                        '[ "$out" -ge 1 ]'
out=$(grep -h "^system:\|^occurred_at:" "$B"/std/*.md 2>/dev/null | sort | uniq -c | sort -rn | head -8)
check "4d 六要素里的时间 / 系统都不为空"                              '! has "system: \"\"" && ! has "occurred_at: \"\""'
echo "   ↳ $(printf '%s' "$out" | tr '\n' ';' | cut -c1-300)"
out=$(grep -c "原文未写明\|原文未提及\|未提及" "$B"/std/*.md 2>/dev/null | awk -F: '{s+=$2} END{print "unknown-marks="s}')
echo "   ↳ 显式「原文未写明」的段落数:$out(允许,但要看得见)"
out=$(K exec "deploy/kb-import-$U" -- sh -c 'ls /nas/kb/cases | wc -l')
check "4e 没有绕过闸门直接写 cases/(数量不变)"                       '[ "$(printf "%s" "$out" | tr -d " ")" = "'$(ls "$NAS/kb/cases" | wc -l | tr -d ' ')'" ]'

echo "== 汇总: PASS $pass, FAIL $fail"
[ $fail -eq 0 ]
