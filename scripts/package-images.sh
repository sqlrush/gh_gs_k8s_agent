#!/usr/bin/env bash
# 离线交付包:把**五个**镜像(两种架构)导出成 tar.gz + sha256,连同 k8s 清单与文档打成一个 dist/ 目录。
# 另有发版台镜像(跳板机上跑,不进集群)单独一个 tar + 启动脚本 release-console.sh。
#   gaussdb-agent-runtime / -kb-import   每用户一个的业务 Pod
#   gaussdb-agent-gateway                接入网关(全体共用)
#   gaussdb-agent-frontend               四个大盘的静态页面(全体共用)
#   gaussdb-agent-kbstd                  标准知识库(官方文档集),部署时由 Job kbstd-install 装进 NAS(2026-10-27 起)
# 漏了哪个,行方拿到包也部署不完整 —— 网关起不来、大盘 502、官方文档集装不上,而现场往往没有外网可以补拉。
# 开发时没有标准知识库包可加 --without-kbstd,正式发版不许用。
# --e2b:另出 Fuyao 沙箱模式的两种模板镜像(docker/Dockerfile.e2b 构建,压成一层的 OCI 包,每种架构一个
#   <包名>-e2b-<架构>.oci.tar),连同 e2b/ 目录(模板构建脚本、临时仓库、Dockerfile.e2b)。平台的模板构建主机
#   不能上网时,把 OCI 包拷过去执行 e2b/scripts/build-e2b-template.py --tag <TAG> --from-oci <OCI 包>。要有 kbstd。
# (原第 5 个 openGauss 向量库镜像 2026-10-23 下线:知识库索引改为 NAS 上的 index/store.sqlite。)
# 用法: scripts/package-images.sh <TAG> [--arch amd64,arm64] [--without-kbstd] [--e2b]
#   前提: 镜像已构建——本机架构用 <TAG>,另一架构用 <TAG>-<arch>(scripts/build-images.sh <TAG>-<arch> --platform linux/<arch>)。
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
TAG=${1:?tag}; shift || true
ARCHS="amd64,arm64"; KBSTD="kbstd"; E2B=""
while [ $# -gt 0 ]; do case "$1" in --arch) ARCHS=$2; shift 2 ;; --without-kbstd) KBSTD=""; shift ;; --e2b) E2B=1; shift ;; *) echo "unknown $1" >&2; exit 2 ;; esac; done
[ -z "$E2B" ] || [ -n "$KBSTD" ] || { echo "--e2b 要用 kbstd(模板里内置官方文档集),不能和 --without-kbstd 一起用" >&2; exit 2; }
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
  for T in runtime kb-import gateway frontend $KBSTD; do
    IMG="gaussdb-agent-$T:$TAG$SUFFIX"
    docker image inspect "$IMG" >/dev/null 2>&1 || { echo "缺镜像 $IMG(先 build-images.sh;标准知识库 kbstd 要加 --kbstd <放包的目录>)" >&2; exit 1; }
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
  if [ -n "$E2B" ]; then
    # Fuyao 模板镜像:层都已压缩,OCI 包不再 gzip。名字不是「<包名>-<架构>.tar.gz」,发版台不会把它当成要入库的镜像包
    for K in runtime:runtime-e2b kb-import:kbimport-e2b; do
      docker buildx build -q --load --platform "linux/$ARCH" -f "$HERE/docker/Dockerfile.e2b" --target "${K#*:}" \
        --build-arg "RUNTIME_IMAGE=gaussdb-agent-runtime:$TAG$SUFFIX" \
        --build-arg "KBIMPORT_IMAGE=gaussdb-agent-kb-import:$TAG$SUFFIX" \
        --build-arg "KBSTD_IMAGE=gaussdb-agent-kbstd:$TAG$SUFFIX" \
        -t "gaussdb-agent-${K%%:*}-e2b:$TAG$SUFFIX" "$HERE" >/dev/null
    done
    EOUT="$DIST/$PKG-e2b-$ARCH.oci.tar"
    docker save --platform "linux/$ARCH" -o "$EOUT" "gaussdb-agent-runtime-e2b:$TAG$SUFFIX" "gaussdb-agent-kb-import-e2b:$TAG$SUFFIX"
    (cd "$DIST" && shasum -a 256 "$(basename "$EOUT")" > "$(basename "$EOUT").sha256")
    echo "  → $(basename "$EOUT") $(du -h "$EOUT" | cut -f1)  Fuyao 模板镜像(e2b/scripts/build-e2b-template.py --from-oci)" >> "$DIST/MANIFEST.txt"
  fi
done
if [ -n "$E2B" ]; then
  mkdir -p "$DIST/e2b/scripts" "$DIST/e2b/docker"          # 照仓库的相对路径放:脚本按自己的位置找 oci-serve.py
  install -m 0755 "$HERE/scripts/build-e2b-template.py" "$HERE/scripts/oci-serve.py" "$DIST/e2b/scripts/"
  install -m 0644 "$HERE/docker/Dockerfile.e2b" "$DIST/e2b/docker/"
fi
install -m 0755 "$HERE/scripts/release-console.sh" "$DIST/release-console.sh"
install -m 0755 "$HERE/scripts/rootspec-overlay.sh" "$DIST/rootspec-overlay.sh"   # 行内把根规范模板换成正式规范(版本说明)

# 清单与文档
# **文档清单不要再写死。** 写死的后果是:加了新文档却忘了加进这一行,行方拿到的包里
# 没有最重要的那两份,而打包脚本一声不响地成功了。改成整个 docs/ 目录打进去,
# 只排除内部用的:
#   plans/     我们的实施计划
#   security/  扫描报告
#   specs/     设计 spec —— 里面有备选方案、退路、残余风险、未定项与决策过程。
#              交付文档不写我们的讨论过程,这一份从头到尾都是讨论过程。
#   prototypes/ 大盘设计稿(示例数字、方案对比)
#   acceptance/ + 智能体验收方案.md  验收题库的方案与网页版(跑题要连测试环境与模拟组件;在线页面在联合仓库)
#   env-contract.md / k8s-deploy.md / delivery-容器化交付手册.md  早期文档,版本号停在 v0.4.1 / v0.7,
#              还指向 specs/。v1.0.4 的包里带着它们,验 v1.1.0 包时才发现。
# 仓库结构说明的「内部件(不随包发出)」表与这里必须一致,有守卫。
# COPYFILE_DISABLE=1:在 macOS 上打包时,bsdtar 会为每个带扩展属性的文件额外塞一个
# `._<原名>` 的资源叉文件(行方解开后 docs/ 里会多出一堆 ._参数手册.md)。
# 那些文件还带着本机的 com.apple.provenance 属性。交付给行方的包里不该有。
# 这个变量在 Linux 的 GNU tar 上是无害的空操作。
COPYFILE_DISABLE=1 tar czf "$DIST/$PKG-k8s.tar.gz" -C "$HERE" \
    --exclude='docs/plans' --exclude='docs/security' --exclude='docs/specs' --exclude='docs/prototypes' \
    --exclude='docs/env-contract.md' --exclude='docs/k8s-deploy.md' --exclude='docs/delivery-容器化交付手册.md' \
    --exclude='docs/acceptance' --exclude='docs/智能体验收方案.md' \
    --exclude='._*' --exclude='.DS_Store' \
    k8s docs
(cd "$DIST" && shasum -a 256 "$PKG-k8s.tar.gz" > "$PKG-k8s.tar.gz.sha256")
# 发版台读的 release.yaml:本版本与历史各版本的变化(k8s/release/changes.yaml)+ 各镜像的平台清单 digest + 白名单全量。
# changes.yaml 第一条不是本版本时这里失败 —— 发版前先把本版本的变化写进去。
(cd "$HERE" && python3 -m release.pack --tag "$TAG" --dist "$DIST" --date "$(date +%F)")
(cd "$DIST" && shasum -a 256 release.yaml > release.yaml.sha256)
cat >> "$DIST/MANIFEST.txt" <<EOF
  → $PKG-k8s.tar.gz  k8s/ 清单模板 + docs/ 全部交付文档(清单见 docs/仓库结构说明.md「交付给行方的」一节)
  → release.yaml     发版台读的版本说明(镜像 digest、白名单全量、各版本的变化);用发版台时把整个目录拷进发布包目录即可
  → release-console.sh  发版台一条命令起:整个目录拷进发布包目录后运行 <目录>/release-console.sh(--help 看选项)
  → rootspec-overlay.sh 把镜像里的三份根规范模板换成行内正式规范,另出一个新包目录(--help 看用法;版本说明有步骤)
$( [ -n "$E2B" ] && echo "  → e2b/               Fuyao 模板:在平台的模板构建主机上 python3 e2b/scripts/build-e2b-template.py --tag $TAG --arch <架构> --from-oci $PKG-e2b-<架构>.oci.tar" )

导入镜像:
  docker:      gunzip -c $PKG-<arch>.tar.gz | docker load
  containerd:  gunzip -c $PKG-<arch>.tar.gz | ctr -n k8s.io images import -
  然后按内网仓库地址重打标签并 push;k8s/templates 里的镜像名由 provision 的 --image-tag 决定。
校验: shasum -a 256 -c *.sha256
EOF
echo "== $DIST"; cat "$DIST/MANIFEST.txt"
