# API 与 SSE 规范

## API契约

- 后端 `openapi.json` 是唯一来源，生成的客户端和类型禁止手工修改。
- `pnpm api:sync` 必须写入 API标签、后端 commit SHA、schema摘要和生成器版本。
- API错误统一映射为可判定的客户端错误类型，禁止只依赖展示文案判断分支。

## SSE

- H5可使用适配器包装 EventSource；微信小程序使用分块 HTTP，但两者输出同一事件模型。
- 字节流必须使用流式 UTF-8解码，保留未完成字符和未完成 SSE帧。
- 事件按空行分帧，支持多行 `data`、`id` 和 heartbeat。
- 使用 `run_id + sequence` 去重，持久化最后确认 sequence用于续传。
- 页面离开或网络断开只关闭连接，不调用取消 run；重连前先查询状态补偿。
- 流票据过期时重新申请，禁止把票据写入日志、埋点或错误上报。
