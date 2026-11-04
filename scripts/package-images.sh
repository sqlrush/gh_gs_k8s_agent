#!/usr/bin/env bash
# 离线交付包:把后端**三个**镜像(两种架构)导出成 tar.gz + sha256,连同 k8s 清单与文档打成一个 dist/ 目录。
#   gaussdb-agent-runtime / -kb-import   每用户一个的业务 Pod(固定池化的槽位也用 runtime)
#   gaussdb-agent-gateway                接入网关(全体共用)
#   gaussdb-agent-kbstd                  标准知识库(GaussDB 官方文档集),部署时由 Job kbstd-install 装进 NAS(2026-10-27 起)
# 漏了哪个,行方拿到包也部署不完整 —— 网关起不来、官方文档集装不上,而现场往往没有外网可以补拉。开发时没有标准知识库包可加 --without-kbstd。
# 前端镜像不在本包里:由前端方自建,按 docs/前端对接接口说明.md 接入。
# 另有发版台镜像(跳板机上跑,不进集群)单独一个 tar + 启动脚本 release-console.sh,以及发版台读的 release.yaml。
# 用法: scripts/package-images.sh <TAG> [--arch amd64,arm64] [--without-kbstd]
#   前提: 镜像已构建——本机架构用 <TAG>,另一架构用 <TAG>-<arch>(scripts/build-images.sh <TAG>-<arch> --platform linux/<arch>)。
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
TAG=${1:?tag}; shift || true
ARCHS="amd64,arm64"; KBSTD="kbstd"
while [ $# -gt 0 ]; do case "$1" in --arch) ARCHS=$2; shift 2 ;; --without-kbstd) KBSTD=""; shift ;; *) echo "unknown $1" >&2; exit 2 ;; esac; done
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
  for T in runtime kb-import gateway $KBSTD; do
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
  # 发版台镜像单独一个包:它跑在跳板机上,不进集群、不推行方的镜像仓库。名字不是「<包名>-<架构>」,
  # release/pack.py 与发版台的包识别都不会把它当成要入库的镜像包
  RIMG="gaussdb-agent-release:$TAG$SUFFIX"
  docker image inspect "$RIMG" >/dev/null 2>&1 || { echo "缺镜像 $RIMG(build-images.sh 会一起构建)" >&2; exit 1; }
  ROUT="$DIST/gaussdb-agent-release-${PKG#gaussdb-agent-}-$ARCH.tar.gz"
  docker save "$RIMG" | gzip -6 > "$ROUT"
  (cd "$DIST" && shasum -a 256 "$(basename "$ROUT")" > "$(basename "$ROUT").sha256")
  echo "  → $(basename "$ROUT") $(du -h "$ROUT" | cut -f1)  发版台(release-console.sh 导入)" >> "$DIST/MANIFEST.txt"
done
install -m 0755 "$HERE/scripts/release-console.sh" "$DIST/release-console.sh"
install -m 0755 "$HERE/scripts/rootspec-overlay.sh" "$DIST/rootspec-overlay.sh"   # 行内把根规范模板换成正式规范(版本说明)

# 清单与文档:k8s/ 与 docs/ 整个打进去(docs/ 里只有交付文档)。
# COPYFILE_DISABLE=1:在 macOS 上打包时,bsdtar 会为每个带扩展属性的文件额外塞一个
# `._<原名>` 的资源叉文件,还带着本机的 com.apple.provenance 属性。交付给行方的包里不该有。
# 这个变量在 Linux 的 GNU tar 上是无害的空操作。
COPYFILE_DISABLE=1 tar czf "$DIST/$PKG-k8s.tar.gz" -C "$HERE" \
    --exclude='._*' --exclude='.DS_Store' --exclude='__pycache__' \
    k8s docs
(cd "$DIST" && shasum -a 256 "$PKG-k8s.tar.gz" > "$PKG-k8s.tar.gz.sha256")
# 发版台读的 release.yaml:本版本与历史各版本的变化(k8s/release/changes.yaml)+ 各镜像的平台清单 digest + 白名单全量。
# changes.yaml 第一条不是本版本时这里失败 —— 发版前先把本版本的变化写进去。
(cd "$HERE" && python3 -m release.pack --tag "$TAG" --dist "$DIST" --date "$(date +%F)")
(cd "$DIST" && shasum -a 256 release.yaml > release.yaml.sha256)
cat >> "$DIST/MANIFEST.txt" <<EOF
  → $PKG-k8s.tar.gz  k8s/ 清单模板 + docs/ 交付文档(构建部署手册、前端对接接口说明、发版台运维手册)
  → release.yaml     发版台读的版本说明(镜像 digest、白名单全量、各版本的变化)
  → release-console.sh  发版台一条命令起:整个目录拷进发布包目录后运行 <目录>/release-console.sh(--help 看选项)
  → rootspec-overlay.sh 把镜像里的三份根规范模板换成行内正式规范,另出一个新包目录(--help 看用法;版本说明有步骤)

导入镜像:
  docker:      gunzip -c $PKG-<arch>.tar.gz | docker load
  containerd:  gunzip -c $PKG-<arch>.tar.gz | ctr -n k8s.io images import -
  然后按内网仓库地址重打标签并 push;k8s/templates 里的镜像名由 provision 的 --image-tag 决定。
校验: shasum -a 256 -c *.sha256
EOF
echo "== $DIST"; cat "$DIST/MANIFEST.txt"
