# AI Agent 工作约束

AI 编程助手修改前端仓库前必须读取本文件、`docs/README.md` 及 `docs/standards/code-comments.md`。

## 强制约束

- MUST 使用后端 OpenAPI生成类型和客户端，禁止手写复制后端 Schema。
- MUST 保持 H5与微信小程序可移植边界，不在共享模块直接使用仅浏览器可用 API。
- MUST 将 SSE按字节流增量解码，处理半帧、UTF-8跨块和重复 sequence。
- MUST 运行 `pnpm ci:check` 或与修改相称的检查，并报告未验证平台。
- MUST 为导出能力和复杂业务、状态、SSE、安全及多端适配逻辑补充与实现同步的详细注释。
- MUST NOT 输出、提交或发送令牌、流票据、用户原文和私有附件。
- MUST NOT 虚构接口、吞掉错误、绕过鉴权或修改生成目录。
- MUST NOT 未经授权提交、推送、发布、删除数据或调用真实通知渠道。

跨仓库接口变更先更新后端 OpenAPI，再执行前端 `pnpm api:sync`。
