#!/bin/bash
# 检索层三态对照(不经模型):示例语料 → 容器 → eval → health(og)三态。结果对照见 scripts/kb-e2e.md。
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PY:-/usr/bin/python3}"
KBPY="$ROOT/skills/gaussdb-kb-import/scripts/kb.py"     # 导入 skill:validate / setup / index / eval
KBQ="$ROOT/skills/gaussdb-kb/scripts/kb.py"             # 查询 skill:query / health / search / cite-check
KB="${KB_DIR:-$HOME/.kb-sample}"
CONN="${KB_DEMO_CONN:-og}"
source "$HOME/.kb-test.env"
cd "$ROOT"
step() { echo; echo "===== $* ====="; }

step "生成语料"; $PY skills/gaussdb-kb-import/testdata/build_sample.py
step "validate(样例目录)"; $PY "$KBPY" validate --kb skills/gaussdb-kb-import/testdata/sample-kb 2>&1 | tail -3
rm -rf "$KB"; cp -R skills/gaussdb-kb-import/testdata/sample-kb "$KB"
cat > "$KB/kb.yaml" <<'YAML'
store:
  pg: {host: 127.0.0.1, port: 5440, database: kb, user: kb, credential: kb-pg}
  graph: {url: http://127.0.0.1:7474, user: neo4j, credential: kb-graph}
embeddings: {source: none}
YAML
step "setup"; $PY "$KBPY" setup --kb "$KB" | head -4
step "index --rebuild"; $PY "$KBPY" index --kb "$KB" --rebuild 2>&1 | grep -v '^提示'
step "eval"; $PY "$KBPY" eval --kb "$KB"; echo "rc=$?"

run_health() { perl -e "alarm shift; exec @ARGV" 240 "$PY" skills/gaussdb-health/scripts/health.py -c "$CONN" 2>/dev/null; }
kb_section() { sed -n '/^## 客户知识库参照/,/^## Overview\|^## Deterministic/p' | grep -E '^## |^> |^### 对|^- \*\*|^- 贵行规范:无'; }
step "态一:导入前(无 GSDB_KB_DIR,安装根下无 kb/)"; unset GSDB_KB_DIR; run_health | kb_section | head -3
step "态二:导入后"; export GSDB_KB_DIR="$KB"; run_health > /tmp/kb-health-after.md; kb_section < /tmp/kb-health-after.md
step "态三:回退"; mv "$KB" "$KB.off"; run_health | kb_section | head -3; mv "$KB.off" "$KB"
