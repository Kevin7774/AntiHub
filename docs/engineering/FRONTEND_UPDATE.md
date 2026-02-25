# Frontend Update Report

## 修复与改进
- 顶部导航去重：仅保留“控制台 / 模板库 / 订阅”三入口，主按钮按页面上下文切换
- “控制台”按钮使用前端路由跳转；“API 文档”基于 `VITE_API_BASE` 打开，失败可复制链接
- Playwright `webServer` 自动拉起 preview，`npm run e2e` 一键 build + 冒烟测试，CI 下不复用已存在端口
- E2E 冒烟全部改为 data-testid + 完整 mock，不依赖中文文案或后端状态
- 空状态与说明书侧栏优化，阅读与扫描更稳定

## 变更文件清单
- `frontend/src/App.tsx`
- `frontend/src/styles.css`
- `frontend/e2e/smoke.spec.ts`
- `frontend/playwright.config.ts`
- `frontend/package.json`
- `frontend/vite.config.ts`
- `TESTING.md`

## 如何验证（自测清单）
- [ ] `cd frontend && npm ci`
- [ ] `npm run test`
- [ ] `npx playwright install --with-deps`
- [ ] `npm run e2e`
- [ ] 顶部仅一个主按钮（控制台=新建案例 / 创建页=返回控制台 / 详情页=刷新状态）
- [ ] API 文档能打开，失败时 toast 提示并可复制链接
