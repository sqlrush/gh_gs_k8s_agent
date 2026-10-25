#!/bin/bash
# 知识库测试容器(幂等):kbneo4j(:7474,7687)。
# 2026-10-23 起向量库是知识库目录里的 index/store.sqlite(SQLite + sqlite-vec),原 kbpg / og7 两个向量库容器不再需要。
# 口令来自 ~/.kb-test.env(0600),首次运行自动生成,绝不打印。
set -u
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
ENV_FILE="$HOME/.kb-test.env"
if [ ! -f "$ENV_FILE" ]; then
  umask 077
  printf 'export KB_NEO4J_PW=%s\n' "$(openssl rand -hex 12)" > "$ENV_FILE"
  echo "已生成 $ENV_FILE(0600)。之后用 python3 -m common.credential_cli set kb-graph 把同样的口令存进凭据库。"
fi
source "$ENV_FILE"
have_ctr() { docker ps -a --format '{{.Names}}' | grep -qx "$1"; }

start_neo4j() {
  docker image inspect neo4j:5-community >/dev/null 2>&1 || docker pull -q neo4j:5-community >/dev/null
  have_ctr kbneo4j || docker run -d --name kbneo4j -e NEO4J_AUTH="neo4j/$KB_NEO4J_PW" -p 7474:7474 -p 7687:7687 neo4j:5-community >/dev/null
  docker start kbneo4j >/dev/null 2>&1 || true
  for _ in $(seq 1 90); do curl -sf http://127.0.0.1:7474/ >/dev/null 2>&1 && { echo "kbneo4j ready"; return; }; sleep 2; done
  echo "kbneo4j NOT ready"
}
start_neo4j
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E "kbneo4j"
echo
echo "live 测试环境变量:"
echo '  source ~/.kb-test.env; export KB_TEST_NEO4J="http://127.0.0.1:7474|neo4j|$KB_NEO4J_PW"'
