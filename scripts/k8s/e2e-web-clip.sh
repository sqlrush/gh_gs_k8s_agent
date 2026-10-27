#!/usr/bin/env bash
# 端到端(模型级):网页剪藏 —— 需要登录的内网网页经书签 → 剪藏页 → 导入容器 → 模型整理 → 管理员确认 → 入库 → 检索得到。
# 真实模型;本机起一个需要登录的模拟 Wiki(scripts/k8s/mock-wiki.py)与一个知识库管理员的模拟 SSO。
# 前提:测试环境已部署本分支的 kb-import 与网关镜像,agent-config 的 KB_CLIP_ORIGINS 含模拟 Wiki 的源(默认 http://127.0.0.1:18300),
#       网关端口转发在 $GW(默认 18089)。
# 用法: scripts/k8s/e2e-web-clip.sh [管理员工号]
# **收尾会把知识库还原**:开跑前备份知识库里的条目与索引(不含官方文档集、收件目录、标准化目录),结束后照备份还原,
# 并删掉本次新出现的剪藏批次 / 收件目录 / 标准化批次 —— 测试环境的知识库是共用的。
set -u
U=${1:-u9500}; C=${CONTEXT:-orbstack}; NS=gaussdb-agent
GW=${GW:-18089}; SSO=${SSO:-18095}; WIKI_PORT=${WIKI_PORT:-18300}
HERE=$(cd "$(dirname "$0")/../.." && pwd)
NAS=${NAS:-/Users/Shared/agent-nas-k8s}; KB=$NAS/kb
OUT=${OUT:-/tmp/e2e-web-clip}; TRUST=${TRUST:-$HOME/.og5-daily/trust}
WAIT=${WAIT:-1500}
PORTAL=http://127.0.0.1:$SSO; WIKI=http://127.0.0.1:$WIKI_PORT
K(){ kubectl --context "$C" -n "$NS" "$@"; }
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "[PASS] $1"; }
bad(){ fail=$((fail+1)); echo "[FAIL] $1"; echo "   | $(printf '%s' "${out:-}" | head -8 | tr '\n' ' ' | cut -c1-600)"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
has(){ printf '%s' "$out" | grep -q -- "$1"; }
rm -rf "$OUT"; mkdir -p "$OUT"; touch "$OUT/t0"

# ---- 0 备份知识库(条目与索引);记下收件 / 标准化目录里原有的东西 ----
BK=$OUT/kb-backup; mkdir -p "$BK"
EXCL=(--exclude /docsets --exclude /inbox --exclude /init --exclude /index/embed-cache)
rsync -a "${EXCL[@]}" "$KB/" "$BK/" || { echo "备份知识库失败"; exit 1; }
for d in inbox/clips inbox/uploads inbox init; do ls -1 "$KB/$d" 2>/dev/null | sort > "$OUT/before-$(echo $d | tr / _).txt"; done
N_CASES=$(ls "$KB/cases" | wc -l | tr -d ' ')
restore(){
  echo "== 收尾:还原知识库"
  rsync -a --delete "${EXCL[@]}" "$BK/" "$KB/"
  for d in inbox/clips inbox/uploads inbox init; do
    comm -13 "$OUT/before-$(echo $d | tr / _).txt" <(ls -1 "$KB/$d" 2>/dev/null | sort) | while read -r x; do
      [ -n "$x" ] && [ "$x" != clips ] && [ "$x" != uploads ] && rm -rf "${KB:?}/$d/$x" && echo "   删掉 $d/$x"
    done
  done
  kill "$WIKI_PID" "$SSO_PID" 2>/dev/null
}

# ---- 1 模拟 Wiki + 管理员的模拟 SSO ----
python3 "$HERE/scripts/k8s/mock-wiki.py" "$WIKI_PORT" > "$OUT/wiki.log" 2>&1 & WIKI_PID=$!
python3 "$HERE/k8s/tools/sso-sim.py" "$SSO" "$GW" "$U" "$TRUST" kb-admin > "$OUT/sso.log" 2>&1 & SSO_PID=$!
trap restore EXIT
sleep 2
out=$(K get cm agent-config -o jsonpath='{.data.KB_CLIP_ORIGINS}')
check "0 agent-config 的 KB_CLIP_ORIGINS 含模拟 Wiki"                  'has "127.0.0.1:$WIKI_PORT"'

# ---- 2 浏览器:安装页 → 登录 Wiki → 点书签 → 剪藏窗口 → 导入 ----
out=$(node "$HERE/scripts/k8s/e2e-web-clip.mjs" "$PORTAL" "$WIKI" "$OUT" 2>&1); echo "$out" | grep -E '^  [✓✗]'
J=$(printf '%s' "$out" | tail -1)
out=$(printf '%s' "$J" | python3 -c 'import json,sys; r=json.load(sys.stdin); print("\n".join(("ok " if s["ok"] else "NO ")+s["name"] for s in r["steps"]))')
check "1 浏览器侧全部步骤通过(安装页、登录、书签、剪藏窗口、上传、跳到会话)"  '! has "^NO "'
BATCH=$(printf '%s' "$J" | python3 -c 'import json,sys; print(json.load(sys.stdin)["batch"])')
SID=$(printf '%s' "$J" | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"])')
echo "   ↳ 批次 $BATCH,会话 $SID;截图在 $OUT"
[ -n "$BATCH" ] && [ -n "$SID" ] || { echo "== 汇总: PASS $pass, FAIL $((fail+1))(没拿到批次或会话,后面不跑)"; exit 1; }

# ---- 3 落盘核对 ----
B=$KB/inbox/clips/$BATCH
out=$(ls "$B" "$B/images" 2>&1; python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print("user=%s url=%s enc=%s skipped=%d" % (m["user_id"], m["url"], m["encoding"], len(m["skipped"])))' "$B/meta.json" 2>&1)
check "2 批次落盘:快照、meta、同源截图;跨源图片记在跳过清单"             "has '$BATCH.html' && has 'img-001.png' && has 'user=$U' && has 'skipped=1'"

# ---- 4 模型处理第一轮(剪藏页自动发出的「导入剪藏」)----
api(){ curl -s -m "${3:-60}" -H "Cookie: gsa_target=kb-import" -H "Content-Type: application/json" "$PORTAL$1" ${2:+-d "$2"}; }
# 等这一轮结束。模型用提问工具请管理员确认时(选择列表)返回 2,问题存在 $OUT/question.json —— 由脚本代管理员作答
wait_idle(){ local t0; t0=$(date +%s); sleep 15
  while [ $(( $(date +%s) - t0 )) -lt "$WAIT" ]; do
    api /question | python3 -c 'import json,sys
q=[x for x in json.load(sys.stdin) if x.get("sessionID")==sys.argv[1]]
json.dump(q[0], open(sys.argv[2], "w"), ensure_ascii=False) if q else None
sys.exit(0 if q else 1)' "$SID" "$OUT/question.json" 2>/dev/null && return 2
    api /session/status | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if (d.get(sys.argv[1]) or {}).get("type") in ("busy", "retry") else 1)' "$SID" 2>/dev/null || return 0
    sleep 10
  done; return 1; }
# 代管理员选每个问题的第一项(模型把推荐项放第一个,标 Recommended)
answer(){ python3 -c 'import json,sys
q=json.load(open(sys.argv[1]))
print(json.dumps({"answers": [[x["options"][0]["label"]] for x in q["questions"]]}, ensure_ascii=False))' "$OUT/question.json" > "$OUT/answer.json"
  echo "   ↳ 代管理员作答:$(cat "$OUT/answer.json" | cut -c1-200)"
  api "/question/$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$OUT/question.json")/reply" "@$OUT/answer.json" >/dev/null; }
last_reply(){ api "/session/$SID/message" | python3 -c 'import json,sys
ms=[m for m in json.load(sys.stdin) if m.get("info",{}).get("role")=="assistant"]
print("".join(p.get("text","") for p in (ms[-1]["parts"] if ms else []) if p.get("type")=="text"))'; }
tools(){ api "/session/$SID/message" | python3 -c 'import json,sys
for m in json.load(sys.stdin):
    for p in m.get("parts",[]):
        if p.get("type")=="tool": print(json.dumps(p.get("state",{}).get("input",{}),ensure_ascii=False)[:300])'; }
START=$(date +%s); wait_idle; RC=$?; DT=$(( $(date +%s) - START ))
out=$(api "/session/$SID/message" | python3 -c 'import json,sys; ms=json.load(sys.stdin); print(ms[0]["parts"][0].get("text","") if ms else "")')
check "3 会话里第一句是剪藏页发出的导入指令"                              "has '导入剪藏 clips/$BATCH'"
out=$(tools); printf '%s\n' "$out" > "$OUT/tools-1.txt"
check "4 模型跑了 clip ingest(耗时 ${DT}s)"                              "has 'clip ingest'"
out=$(ls "$B" 2>&1; head -12 "$B/page.md" 2>&1)
check "5 page.md 已生成,带出处(source_url / sha256)"                    "has 'page.md' && has 'source_url' && has 'sha256'"
out=$(grep -c '首页\|评论(2)\|赞 12\|技术支持请联系' "$B/page.md")
check "6 page.md 里没有导航 / 评论 / 页脚"                                '[ "$out" = 0 ]'
out=$(last_reply); printf '%s\n' "$out" > "$OUT/reply-1.txt"
check "7 模型给出待确认的清单(写入前交管理员确认)"                       'has "确认" && has "案例"'
echo "   ↳ $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-400)"
out=$(ls "$KB/cases" | wc -l | tr -d ' ')
check "8 确认之前没有写进 cases/(还是 $N_CASES 条)"                        '[ "$out" = "$N_CASES" ]'

# ---- 5 确认入库:模型用提问工具问的就选推荐项;没问(停下等回复)就发确认消息 ----
START=$(date +%s)
if [ "$RC" = 2 ]; then
  answer
else
  api "/session/$SID/prompt_async" '{"parts":[{"type":"text","text":"确认,按你给的清单全部入库;入库后把原始网页 attach 到条目上作为证据、重建索引,最后告诉我条目 ID。"}]}' >/dev/null
fi
for i in 1 2 3 4 5; do wait_idle; RC=$?; [ "$RC" = 2 ] && answer || break; done
DT=$(( $(date +%s) - START ))
out=$(last_reply); printf '%s\n' "$out" > "$OUT/reply-2.txt"
echo "   ↳ $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-400)"
NEW=$(comm -13 <(ls -1 "$BK/cases" | sort) <(ls -1 "$KB/cases" | sort))
out="$NEW"
check "9 入库了新案例(耗时 ${DT}s)"                                       '[ -n "$NEW" ]'
# 案例的 source 指向 sources/ 里的标准化快照;原网页地址在那份快照里(剪藏批次处理完会被清掉,不能指回 page.md)
out=$(for f in $NEW; do cat "$KB/cases/$f"; s=$(awk '/^source:/{sub(/^source: */, ""); sub(/#.*/, ""); print; exit}' "$KB/cases/$f"); cat "$KB/$s" 2>/dev/null; done)
check "10 案例筛出了现象 / 根因 / 处置(现场、判断、处置三段都有),出处顺着快照追得到原网页"  "has '127.0.0.1:$WIKI_PORT/display/OPS/lock-incident' && has '## 现场' && has '## 判断' && has '## 处置' && (has 'idle in transaction' || has '未提交') && has 'pg_terminate_backend'"
out=$(ls "$KB/evidence" 2>/dev/null | grep -- "$BATCH"; for f in $NEW; do grep -A3 '^evidence:' "$KB/cases/$f"; done)
check "11 原始网页作为 L3 证据挂在条目上(evidence/ 里有快照,条目登记了它)"   "[ \$(printf '%s' \"\$out\" | grep -c -- '$BATCH') -ge 2 ]"
out=$(find "$KB/index/store.sqlite" -newer "$OUT/t0" 2>/dev/null)
check "12 索引重建过"                                                     '[ -n "$out" ]'

# ---- 6 检索得到 ----
api "/session/$SID/prompt_async" '{"parts":[{"type":"text","text":"用知识库查询(gaussdb-kb 的 kb.py query)查一下:批量作业锁等待超时、根会话 idle in transaction,有没有先例?给出条目 ID 与出处。"}]}' >/dev/null
for i in 1 2 3; do wait_idle; [ $? = 2 ] && answer || break; done
out=$(last_reply); printf '%s\n' "$out" > "$OUT/reply-3.txt"
ID=$(for f in $NEW; do awk '/^id:/{sub(/^id: */, ""); print; exit}' "$KB/cases/$f"; done | head -1)
check "13 检索命中刚入库的案例($ID)"                                        "[ -n '$ID' ] && has '$ID'"

echo "== 汇总: PASS $pass, FAIL $fail(截图、模型回复与工具调用在 $OUT)"
[ $fail -eq 0 ]
