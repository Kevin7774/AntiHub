# Remote vs Local 构建诊断报告

## 1) 现象与结论
- 现象：本地路径仓库可构建，GitHub URL 仓库即便有 Dockerfile 也可能失败。
- 结论：根因主要集中在 **Dockerfile 路径不一致**、**基础镜像拉取失败**、**网络/DNS 问题**、**LFS/Submodule 未初始化** 等环节。已通过 preflight 与错误码体系实现可诊断、可配置、可回归。

## 2) 根因分类与诊断要点
1) Dockerfile 路径不一致
- 远程仓库 Dockerfile 不在根目录或 context 不一致。
- 现改造：支持 `dockerfile_path/context_path`，并在 clone 后向下搜索 2 层自动定位。

2) Multi-stage Dockerfile 误判（已修复）
- 现象：将 stage 名当作外部镜像拉取（如 `app-base`），触发 `DOCKER_BASE_IMAGE_PULL_FAILED`。
- 修复：解析 FROM flags/AS，区分 stages 与 external images；preflight 仅拉取外部镜像并打印列表。

3) 基础镜像拉取失败（网络/registry）
- 典型表现：拉取超时、EOF、DNS 解析失败。
- 现改造：preflight 阶段解析 Dockerfile 的 FROM 列表，逐一 `docker pull`（3 次重试+退避）。
- 失败即中止，返回 `DOCKER_BASE_IMAGE_PULL_FAILED`。

4) 构建网络/DNS 失败
- 典型表现：`Temporary failure in name resolution`、`dial tcp`、`i/o timeout`。
- 现改造：识别并映射 `DOCKER_BUILD_NETWORK_FAILED`，可通过 `docker_build_network=host` 规避网络解析问题。

5) Submodule / LFS 未初始化
- 远程仓库包含 `.gitmodules` 或 LFS 指针文件，但未拉取子模块或 LFS 内容。
- 现改造：可通过 `enable_submodules/enable_lfs` 开关启用（默认关闭）。

## 3) 新增错误码与排障步骤
- `DOCKERFILE_NOT_FOUND`
  - 排障：
    - 检查目录：`find . -maxdepth 3 -name Dockerfile`
    - 指定参数：`dockerfile_path` / `context_path`
- `DOCKER_BASE_IMAGE_PULL_FAILED`
  - 排障：
    - 手动拉取：`docker pull <image>`
    - 检查镜像源/代理/DNS
- `DOCKER_BUILD_NETWORK_FAILED`
  - 排障：
    - 切换网络：`docker_build_network=host`
    - 检查 DNS/代理
- `BUILDKIT_REQUIRED`
  - 排障：
    - 启用：`DOCKER_BUILDKIT=1` 或 daemon 配置 `features.buildkit=true`
- `GIT_REF_NOT_FOUND`
  - 排障：
    - `git ls-remote --symref <url> HEAD`
    - ref 留空或设为 `auto`

## 4) 关键增强点
- Clone 后 preflight：Dockerfile 解析 + 基础镜像拉取 + 自动定位。
- BuildKit 检测：出现 BuildKit 特性时 fail-fast（`BUILDKIT_REQUIRED`）。
- Build 过程可配置：`docker_build_network`、`docker_no_cache`、`docker_build_args`。
- 结构化日志：`[clone]/[preflight]/[pull]/[build]/[run]`。
- 可回归测试脚本：`scripts/integration_tests.py`。

## 5) 回归测试摘要
- 结果文件：`.devlogs/integration_test_results.json`
- 用例 1：ref=auto clone 成功（失败原因为 `DOCKERFILE_NOT_FOUND`）
- 用例 2：ref=master fallback 成功
- 用例 3：Dockerfile 子目录指定 `dockerfile_path/context_path` -> RUNNING
- 用例 4：基础镜像不存在 -> preflight 直接 `DOCKER_BASE_IMAGE_PULL_FAILED`

## 6) 建议
- 对仓库 README 标注 Dockerfile 路径与启动说明。
- 配置镜像源与 DNS（尤其在 WSL/企业网络下）。
- 为含子模块/大文件的仓库开启 `enable_submodules/enable_lfs`。

## 7) 变更应用与回滚
- Patch：`build_remote_vs_local.patch`
- 应用：`patch -p0 < build_remote_vs_local.patch`
- 回滚：`patch -R -p0 < build_remote_vs_local.patch`
