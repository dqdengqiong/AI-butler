# API 与数据库规范

## HTTP API

- 公共业务接口使用 `/v1` 前缀；健康检查保持 `/health/live` 与 `/health/ready`。
- 请求与响应必须有显式 Schema；状态值使用大写英文并与数据库保持一致。
- 统一错误包含稳定 `code`、可展示 `message`、`request_id` 和可选 `details`，不得泄露堆栈。
- 创建或副作用请求 MUST 支持幂等键；冲突使用可区分的业务错误而非静默覆盖。
- OpenAPI 是前端契约唯一来源，生成代码禁止手工修改。

## 兼容性

- `/v1` 内 MAY 增加可选字段，不得删除字段、改变类型或重解释既有语义。
- 破坏性变更必须创建新 API版本，并提供迁移窗口。
- SSE事件使用 `run_id + sequence` 去重，支持 `Last-Event-ID` 续传；连接断开不取消 run。

## 数据库

- 表和列使用 `snake_case`，状态使用 `varchar + CHECK`；核心关联不得只放在 JSON中。
- 每个用户数据查询 MUST 使用服务端上下文中的 `user_id`；跨用户读写拒绝是自动化测试门禁。
- Qdrant共用 collection时，私有数据 payload中的 `tenant_id` 等于用户 UUID，公共数据使用 `public`。这里的字段仅表示用户级过滤，不代表企业多租户。
- 应用角色不得拥有迁移权限；迁移、应用、测试和备份角色分离。

