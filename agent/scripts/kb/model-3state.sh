#!/bin/bash
# 模型层三态(用现成的示例知识库,不走导入交互):同一提示词 × 导入前 / 导入后 / 回退。
# 前提同 model-demo.sh;示例库先由 scripts/kb/threestate.sh 灌好(默认 ~/.kb-sample)。
set -u
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
MODEL="${KB_DEMO_MODEL:-kimi/k3}"
KB="${KB_DIR:-$HOME/.kb-sample}"
OUT="$HOME/kb-model-out"; rm -rf "$OUT"; mkdir -p "$OUT"
WORK="$HOME/kb-model-cwd"; mkdir -p "$WORK"; cd "$WORK"
ASK='用 gaussdb-health 检查连接 og(只读,不做任何变更)。然后只针对 INDEX_UNUSED 和 CACHE_LOW 两类发现各给一条处置建议,每条注明依据来源:来自客户知识库就写案例 ID 或条款 ID,没有就写"通用经验"。'
run() {
  echo "===== $1 $(date +%H:%M:%S) ====="
  perl -e 'alarm shift; exec @ARGV' 900 opencode run --model "$MODEL" --title "kb-3state-$1" "$ASK" > "$OUT/$1.txt" 2> "$OUT/$1.err"
  echo "rc=$? 字数=$(wc -c < "$OUT/$1.txt") 引用ID: $(grep -oE '(S[1-4]-[0-9]{8}-[A-Z]+-[^ ,)、。]+|GS-[A-Z]+-[0-9]{3})' "$OUT/$1.txt" | sort -u | head -6 | tr '\n' ' ')"
}
unset GSDB_KB_DIR;        run before
export GSDB_KB_DIR="$KB"; run after
mv "$KB" "$KB.off";       run rollback; mv "$KB.off" "$KB"
echo "DONE $(date +%H:%M:%S) → $OUT"
