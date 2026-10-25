#!/bin/bash
# 模型层演示(非交互复现 演示手册.md 的 1–5 步):opencode run 驱动,确认环节用预置回答 --continue。
# 前提:scripts/kb/containers.sh 起容器;凭据 kb-pg/kb-graph 已 set;bash install-opencode.sh 已装本分支。
# 输出:~/kb-demo-out/{01-before,02-spec,03-spec-confirm,04-tickets,05-strategy,06-decide,07-after,08-rollback}.txt
set -u
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MODEL="${KB_DEMO_MODEL:-kimi/k3}"
OUT="$HOME/kb-demo-out"; rm -rf "$OUT"; mkdir -p "$OUT"
WORK="$HOME/kb-demo-cwd"; mkdir -p "$WORK"; cd "$WORK"      # 空目录:无 AGENTS.md,只靠 skill 契约
DEMO="$ROOT/skills/gaussdb-kb-import/testdata/demo"
ASK='用 gaussdb-health 检查连接 og(只读,不做任何变更)。然后只针对 INDEX_UNUSED 和 CACHE_LOW 两类发现各给一条处置建议,每条注明依据来源:来自客户知识库就写案例 ID 或条款 ID,没有就写"通用经验"。'

run() {  # $1=文件名 $2=消息 [$3=-c]
  echo "===== $1 $(date +%H:%M:%S) ====="
  perl -e 'alarm shift; exec @ARGV' 1200 opencode run --model "$MODEL" ${3:-} --title "kb-demo-$1" "$2" > "$OUT/$1.txt" 2> "$OUT/$1.err"
  echo "rc=$? 字数=$(wc -c < "$OUT/$1.txt") 引用ID: $(grep -oE '(S[1-4]-[0-9]{8}-[A-Z]+-[^ ,)、。]+|GS-[A-Z]+-[0-9]{3})' "$OUT/$1.txt" | sort -u | head -6 | tr '\n' ' ')"
}

bash "$ROOT/scripts/kb/demo-reset.sh" >/dev/null
run 01-before "$ASK"
run 02-spec "把 $DEMO/运维规范摘录.md 导入知识库。写入之前把条款清单给我确认。"
run 03-spec-confirm "清单确认,全部写入,然后跑 index 和 validate,把 validate 结果告诉我。" -c
run 04-tickets "把 $DEMO/工单导出-2025Q1.csv 导入知识库,原文脱敏。首次导入的策略问题逐题问我。" -c
run 05-strategy "策略回答:全部按默认——案例;一单一条主链;涉及对象 + 引用条款;复发标志两者都要;同义节点合并;全部小节进向量;置信度按此口径;缺省元数据按此缺省。把答案按 key 写进 strategies/tickets.yaml,重跑 propose,然后继续:逐单填写候选并跑 review,把选择列表原样给我。" -c
run 06-decide "全部接受,边也接受,录入人 12345。执行 apply,然后 validate 和 index,最后跑 health 把状态行给我。" -c
run 07-after "$ASK"
bash "$ROOT/scripts/kb/demo-reset.sh" >/dev/null
run 08-rollback "$ASK"
echo "DONE $(date +%H:%M:%S) → $OUT"
