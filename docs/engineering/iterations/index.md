# 迭代记录索引

> 目标：把分散的迭代记录/补丁说明收拢为可阅读的工程轨迹。

## 里程碑记录
- `docs/engineering/iterations/first.md`
- `docs/engineering/iterations/second.md`
- `docs/engineering/iterations/third.md`
- `docs/engineering/iterations/fourth.md`
- `docs/engineering/iterations/rerun.md`

## 专题补充
- `docs/engineering/branch_fix.md`（分支/Ref 兼容）
- `docs/engineering/build_remote_vs_local.md`（本地 vs 远程构建）
- `docs/engineering/FRONTEND_UPDATE.md`（前端迭代）
- `docs/engineering/improvement_plan.md`（改进计划）
- `docs/engineering/integration_tests.md`（集成测试清单）
- `docs/engineering/research-deployment-platform-feasibility-study.md`（可行性研究）
- `docs/engineering/tests.md`（测试体系说明）

## 补丁归档
补丁文件集中在：`docs/engineering/patches/`
- `branch_fix.patch`
- `build_remote_vs_local.patch`
- `frontend_improvement.patch`
- `improvement.patch`
- `manual_fix.patch`
- `phase2.patch`
- `phase3.patch`
- `phase4.patch`

应用/回滚示例：
```bash
git apply docs/engineering/patches/phase4.patch
# 回滚（已应用时）
git apply -R docs/engineering/patches/phase4.patch
```
