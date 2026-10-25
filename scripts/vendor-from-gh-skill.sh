#!/usr/bin/env bash
# 把 gh_skill 在某个标签下的技能代码复制进本仓库 agent/,作为 Pod 适配改动的起点。
#
# 只用于首次导入(agent/ 不存在或为空)。之后 gh_skill 的通用修复用 git 三方合并 / cherry-pick
# 同步进来,不要再整目录覆盖——agent/ 里已经有 Pod 专属的改动。
#
# 用法: scripts/vendor-from-gh-skill.sh <gh_skill 工作目录> <标签> [--force]
set -euo pipefail

SRC=${1:?用法: $0 <gh_skill 目录> <标签> [--force]}
TAG=${2:?用法: $0 <gh_skill 目录> <标签> [--force]}
FORCE=${3:-}
HERE=$(cd "$(dirname "$0")/.." && pwd)
DEST=$HERE/agent

if [ -d "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ] && [ "$FORCE" != "--force" ]; then
  echo "agent/ 已存在且非空;首次导入之后请用 git 合并同步,不要整目录覆盖(确需覆盖加 --force)" >&2
  exit 2
fi

SHA=$(git -C "$SRC" rev-parse "$TAG^{commit}")
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 从标签导出,不碰 gh_skill 的工作树。docs/ demo/ prd/ 不导:交付文档与客户材料不进公开仓库。
git -C "$SRC" archive --format=tar "$TAG" \
  common skills scripts tools tests grmp_middleware \
  AGENTS.md install-opencode.sh deploy.sh requirements.txt pytest.ini LICENSE NOTICE \
  | tar -x -C "$TMP"

mkdir -p "$DEST"
cp -R "$TMP"/. "$DEST"/
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +
# 这条测试依赖 docs/delivery(客户白名单交付物,gh_skill 里也是 gitignore 的),本仓库没有这个目录
rm -f "$DEST/tests/test_delivery_drift_units.py"

printf 'gh_skill %s %s\nvendored %s\n' "$TAG" "$SHA" "$(date +%F)" > "$DEST/UPSTREAM"
echo "agent/ <- gh_skill $TAG ($SHA)"
