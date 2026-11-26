# e2b/ —— Fuyao 沙箱模板材料

与离线交付包里的 `e2b/` 目录**逐字节相同**，随本仓库标签 `agent-v1.5.3` 发布。用法见《[Fuyao 沙箱部署手册](../docs/Fuyao沙箱部署手册.md)》§4「注册沙箱模板」。

| 文件 | 作用 |
|---|---|
| `scripts/build-e2b-template.py` | 模板注册脚本：把两个沙箱模板镜像（OCI 包）注册成 E2B 模板 |
| `scripts/oci-serve.py` | 临时镜像仓库：注册期间在构建主机上临时提供 OCI 包，注册完即停 |
| `docker/Dockerfile.e2b` | 沙箱模板镜像的构建文件（自己构建时用，见手册附录「制作容器镜像与鲲鹏沙箱镜像」） |

不用离线包时：`git clone -b agent-v1.5.3 --depth 1 https://github.com/sqlrush/gh_gs_k8s_agent.git`，把本目录整个拷到模板构建主机，再按手册 §2.3 从 GHCR 导出两个模板镜像的 OCI 包。
这三个文件与仓库里的 `scripts/build-e2b-template.py`、`scripts/oci-serve.py`、`docker/Dockerfile.e2b` 内容相同。
