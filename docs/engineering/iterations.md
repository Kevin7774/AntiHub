# 迭代时间线

> 说明：以下为工程迭代的主要里程碑与对应材料入口，便于验收与追溯。

## Phase 1：链路打通（2026-01-20）
- 目标：创建 case → clone/build/run → 日志回放 → 状态查询
- 证据：`./iterations/first.md`

## Phase 2：前后端对齐
- 目标：控制台与 API 契约对齐、WS 回放稳定
- 证据：`./iterations/second.md`
- 相关补丁：`./patches/phase2.patch`

## Phase 3：可管理展示
- 目标：Dashboard 管理动作、日志导出、归档等
- 证据：`./iterations/third.md`
- 相关补丁：`./patches/phase3.patch`

## Phase 4：智能展厅与配置升级
- 目标：说明书生成、配置体系、错误码字典、健康检查
- 证据：`./iterations/fourth.md`
- 相关补丁：`./patches/phase4.patch`

## 分支自动识别修复
- 目标：ref=auto、main/master fallback、错误码完善
- 证据：`./branch_fix.md`
- 相关补丁：`./patches/branch_fix.patch`

## Remote vs Local 构建诊断
- 目标：提升 Dockerfile 发现与预检诊断能力
- 证据：`./build_remote_vs_local.md`
- 相关补丁：`./patches/build_remote_vs_local.patch`

## Manual 差异化与 E2E 复测
- 目标：说明书“证据驱动”与 E2E 复测
- 证据：`./iterations/rerun.md`
- 相关补丁：`./patches/manual_fix.patch`

## 前端体验优化
- 目标：控制台导航、E2E 冒烟与交互优化
- 证据：`./FRONTEND_UPDATE.md`
- 相关补丁：`./patches/frontend_improvement.patch`

## 改进计划与集成测试
- 改进计划：`./improvement_plan.md`
- 集成测试：`./integration_tests.md`
- 结果文件：`./integration_test_results.json`
