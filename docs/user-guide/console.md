# 控制台使用

控制台提供创建 case、查看状态与日志、执行管理动作的一站式体验。

## 启动前端
```bash
cd frontend
npm ci
npm run dev
```

默认地址：`http://127.0.0.1:5173`

## 核心功能
- **创建 Case**：填写 `repo_url` + 可选 `ref`，可添加 env（仅保存 key）
- **详情页**：状态、阶段、运行信息（URL/端口/容器）
- **日志面板**：实时日志 + 历史回放、按 stream 过滤
- **管理动作**：Stop / Restart / Retry / Archive

## 常见操作
- 复制访问地址：详情页 Runtime 卡片
- 错误码定位：详情页 Error 卡片（可跳转错误码手册）
- Retry 带覆盖 env：详情页动作按钮

## 安全注意
- env value 不持久化，前端只展示 key
- 建议使用最小权限的临时凭证
