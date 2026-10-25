#!/usr/bin/env bash
# 离线交付包:把后端**三个**镜像(两种架构)导出成 tar.gz + sha256,连同 k8s 清单与文档打成一个 dist/ 目录。
#   gaussdb-agent-runtime / -kb-import   每用户一个的业务 Pod(固定池化的槽位也用 runtime)
#   gaussdb-agent-gateway                接入网关(全体共用)
# 网关漏了的话,客户拿到包也部署不起来 —— 而现场往往没有外网可以补拉。
# 前端镜像不在本包里:由前端方自建,按 docs/前端对接接口说明.md 接入。
# 用法: scripts/package-images.sh <TAG> [--arch amd64,arm64]
#   前提: 镜像已构建——本机架构用 <TAG>,另一架构用 <TAG>-<arch>(scripts/build-images.sh <TAG>-<arch> --platform linux/<arch>)。
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
TAG=${1:?tag}; shift || true
ARCHS="amd64,arm64"
while [ $# -gt 0 ]; do case "$1" in --arch) ARCHS=$2; shift 2 ;; *) echo "unknown $1" >&2; exit 2 ;; esac; done
HOST_ARCH=$(docker info --format '{{.Architecture}}' 2>/dev/null | sed 's/aarch64/arm64/; s/x86_64/amd64/')
# 包名:标签本身以 agent- 开头时不再重复(gaussdb-agent-v0.4-…),否则 gaussdb-agent-<TAG>
case "$TAG" in agent-*) PKG="gaussdb-$TAG" ;; *) PKG="gaussdb-agent-$TAG" ;; esac
DIST="$HERE/dist/$PKG"
rm -rf "$DIST"; mkdir -p "$DIST"
COMMIT=$(git -C "$HERE" rev-parse --short HEAD)
{
  echo "gaussdb-agent 离线交付包  标签 $TAG  仓库提交 $COMMIT  打包 $(date +%F)"
  echo
} > "$DIST/MANIFEST.txt"

for ARCH in ${ARCHS//,/ }; do
  SUFFIX=""; [ "$ARCH" != "$HOST_ARCH" ] && SUFFIX="-$ARCH"
  IMAGES=""
  for T in runtime kb-import gateway; do
    IMG="gaussdb-agent-$T:$TAG$SUFFIX"
    docker image inspect "$IMG" >/dev/null 2>&1 || { echo "缺镜像 $IMG(先 build-images.sh)" >&2; exit 1; }
    IMAGES="$IMAGES $IMG"
    printf '%-10s %-40s id=%s size=%sMB\n' "$ARCH" "$IMG" "$(docker image inspect "$IMG" --format '{{.Id}}' | cut -c8-19)" \
      "$(docker image inspect "$IMG" --format '{{.Size}}' | awk '{printf "%d", $1/1048576}')" >> "$DIST/MANIFEST.txt"
  done
  OUT="$DIST/$PKG-$ARCH.tar.gz"
  # shellcheck disable=SC2086
  docker save $IMAGES | gzip -6 > "$OUT"
  (cd "$DIST" && shasum -a 256 "$(basename "$OUT")" > "$(basename "$OUT").sha256")
  echo "  → $(basename "$OUT") $(du -h "$OUT" | cut -f1)" >> "$DIST/MANIFEST.txt"
done

# 清单与文档:k8s/ 与 docs/ 整个打进去(docs/ 里只有交付文档)。
# COPYFILE_DISABLE=1:在 macOS 上打包时,bsdtar 会为每个带扩展属性的文件额外塞一个
# `._<原名>` 的资源叉文件,还带着本机的 com.apple.provenance 属性。交付给客户的包里不该有。
# 这个变量在 Linux 的 GNU tar 上是无害的空操作。
COPYFILE_DISABLE=1 tar czf "$DIST/$PKG-k8s.tar.gz" -C "$HERE" \
    --exclude='._*' --exclude='.DS_Store' --exclude='__pycache__' \
    k8s docs
(cd "$DIST" && shasum -a 256 "$PKG-k8s.tar.gz" > "$PKG-k8s.tar.gz.sha256")
cat >> "$DIST/MANIFEST.txt" <<EOF
  → $PKG-k8s.tar.gz  k8s/ 清单模板 + docs/ 交付文档(构建部署手册、前端对接接口说明)

导入镜像:
  docker:      gunzip -c $PKG-<arch>.tar.gz | docker load
  containerd:  gunzip -c $PKG-<arch>.tar.gz | ctr -n k8s.io images import -
  然后按内网仓库地址重打标签并 push;k8s/templates 里的镜像名由 provision 的 --image-tag 决定。
校验: shasum -a 256 -c *.sha256
EOF
echo "== $DIST"; cat "$DIST/MANIFEST.txt"
