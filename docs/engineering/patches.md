# 补丁说明（apply / rollback）

> 说明：补丁文件已集中到 `./patches/`。

## patch 列表

### branch_fix.patch
- 说明：分支自动识别与 fallback（main/master/auto）
- Apply：`patch -p0 < ./patches/branch_fix.patch`
- Rollback：`patch -p0 -R < ./patches/branch_fix.patch`

### build_remote_vs_local.patch
- 说明：远程/本地构建诊断与 Dockerfile 发现增强
- Apply：`patch -p0 < ./patches/build_remote_vs_local.patch`
- Rollback：`patch -p0 -R < ./patches/build_remote_vs_local.patch`

### frontend_improvement.patch
- 说明：前端控制台体验优化与 E2E 冒烟增强
- Apply：`patch -p0 < ./patches/frontend_improvement.patch`
- Rollback：`patch -p0 -R < ./patches/frontend_improvement.patch`

### manual_fix.patch
- 说明：说明书生成差异化与信号增强
- Apply：`patch -p0 < ./patches/manual_fix.patch`
- Rollback：`patch -p0 -R < ./patches/manual_fix.patch`

### phase2.patch
- 说明：Phase 2 前后端对齐
- Apply：`patch -p0 < ./patches/phase2.patch`
- Rollback：`patch -p0 -R < ./patches/phase2.patch`

### phase3.patch
- 说明：Phase 3 管理动作与日志导出
- Apply：`patch -p0 < ./patches/phase3.patch`
- Rollback：`patch -p0 -R < ./patches/phase3.patch`

### phase4.patch
- 说明：Phase 4 智能展厅 + 配置升级
- Apply：`patch -p0 < ./patches/phase4.patch`
- Rollback：`patch -p0 -R < ./patches/phase4.patch`

### improvement.patch
- 说明：改进计划相关的脚本化补丁包
- Apply：参见 `./patches/improvement.patch` 文件头部的嵌入式命令
- Rollback：使用补丁生成的 `.patch_backup_*` 目录恢复
