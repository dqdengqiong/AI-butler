# AI Butler Frontend

基于 uni-app、Vue 3和 TypeScript的 AI个人管家客户端，目标平台为 H5与微信小程序。

## 快速开始

```bash
mise install
pnpm install --frozen-lockfile
cp .env.example .env.local
pnpm dev:h5
```

默认连接本地验证环境：H5 使用 Mock 微信登录，微信小程序使用 `uni.login` 获取登录码。当前闭环覆盖：

- 首页聚合总览、单计划进度和今日任务
- 计划筛选、任务完成及超时调整入口
- 唯一主聊天、选择卡、状态卡、单计划审批卡和来源详情
- 私有附件哈希校验与上传、提醒偏好、退出与异步账号注销

所有业务事实均来自后端 `/v1` API；Access Token 和 SSE ticket 仅保存在内存，Refresh Token 与设备 ID 通过平台存储适配器保存。

## 契约同步

后端仓库是 API契约唯一来源。两个仓库位于同一工作区时执行：

```bash
pnpm api:sync
```

该命令读取 `../ai-butler-backend/openapi.json`，更新生成类型和契约锁。`pnpm api:check` 验证生成结果无漂移。

规范入口见 [docs/README.md](docs/README.md)。
