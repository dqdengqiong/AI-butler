# AI个人管家接口设计文档 V1.0

## 1. 文档目标

本文基于以下输入定义 AI 个人管家 MVP 的客户端—服务端接口契约：

- 《AI个人管家·可点击原型 V10（单计划调整）》
- [《AI个人管家数据库设计文档 V2.4》](./AI个人管家数据库设计.md)
- [《AI个人管家系统详细设计文档 V2.4》](./AI个人管家系统详细设计.md)
- [《AI个人管家聊天系统设计文档 V2.4》](./AI个人管家聊天系统设计.md)

本文是目标接口设计，不代表当前接口已经实现。当前仓库 OpenAPI 仅包含健康检查；进入开发后应先按本文维护后端 OpenAPI，再生成前端客户端。

MVP 重点覆盖：

1. 微信登录、当前用户和规划画像
2. 首页总览、计划列表和任务打卡
3. 多会话、专业 Agent 目录、Agent Run 和 SSE 事件流
4. 首计划/组合计划创建、单计划调整和用户审批
5. 文件上传、聊天附件、资料来源和审核结果
6. 提醒偏好、退出登录和账号删除

## 2. 核心设计结论

### 2.1 业务边界

- 所有会话底层绑定 `BUTLER`。客户端在路径中提交公开会话 ID、创建时可提交公开 `specialist_code`，但不传内部 `user_agent_id`、segment 或 thread ID。
- 每个用户最多一个 `CURRENT` 会话；新建自动归档当前会话，向历史会话首次发送时原子恢复。查看历史本身不改变状态。
- MVP 只正式开放公考领域，包括公务员备考、行测专项和申论专项。雅思、求职和健康只作为“即将开放”展示，不得调用正式创建接口。
- 创建计划和调整计划都必须由 Agent 生成不可变 `plan_revision` 草案，并通过结构化审批接口确认。
- 一次新建请求可以整组确认多个同领域计划；一次调整必须且只能影响一个已存在计划。
- 文本“确认”“可以”“按这个来”不能产生计划发布副作用。
- 任务只能从已批准 revision 物化；客户端不能直接创建正式任务。
- SSE 断开不取消 Agent Run；只有显式取消接口才能取消。

### 2.2 原型到正式接口的修正

| 原型行为 | 正式契约 |
| --- | --- |
| 前端用标题、按钮文字或临时 key 判断计划 | 所有写操作使用服务端 UUID 和版本号 |
| 点击计划卡后前端直接改变本地计划 | 调用审批接口，等待同一 run 恢复并收到完成事件 |
| 附件选择后立即显示“已添加” | 先完成私有上传、安全扫描，再把 `file_id` 放入消息请求 |
| 任务复选框可自由勾选/取消 | MVP 只支持提交执行结果；取消已完成状态暂不开放 |
| “清除全部数据”仅修改前端状态 | 当前数据库只支持账号删除流程；MVP 应改为“注销账号” |
| 手机号和游客登录可直接进入 | 均为原型演示；MVP 默认只实现微信登录 |
| 来源卡直接打开固定弹窗 | 卡片携带 `claim_id`、`citation_id` 和 `document_id`，服务端校验后返回来源详情/短期地址 |

## 3. 通用约定

### 3.1 基础协议

| 项目 | 约定 |
| --- | --- |
| Base URL | `/v1` |
| 传输协议 | HTTPS |
| 普通媒体类型 | `application/json; charset=utf-8` |
| 实时媒体类型 | `text/event-stream` |
| 时间点 | ISO 8601 UTC，例如 `2026-08-09T08:00:00Z` |
| 用户日期 | `YYYY-MM-DD`，按用户 IANA 时区解释 |
| 主键 | UUID 字符串 |
| 状态值 | 大写英文，与数据库一致 |
| Schema 版本 | 请求、卡片和 SSE payload 使用 `schema_version` |

所有需要登录的接口使用：

```http
Authorization: Bearer <access_token>
X-Request-ID: <uuid-or-ulid>
```

`user_id` 必须从服务端认证上下文获得，禁止由请求体、查询参数或模型输出提供。

### 3.2 幂等

创建或产生副作用的请求必须携带稳定幂等标识：

| 操作 | 位置 | 幂等键 |
| --- | --- | --- |
| 登录码验证 | Header | `Idempotency-Key` |
| 发送消息 | Body | `client_message_id` |
| 申请上传 | Header | `Idempotency-Key` |
| 完成上传 | Header | `Idempotency-Key` |
| 任务执行 | Body | `client_execution_id` |
| 审批决定 | Body | `approval_id + expected_approval_version` |
| Run 重试 | Body | `run_id + expected_attempt` |

同一幂等键、相同规范化请求返回首次结果；相同键、不同请求返回 `409 IDEMPOTENCY_KEY_REUSED`。

### 3.3 分页

列表接口统一使用游标分页：

```http
GET /v1/tasks?limit=20&cursor=<opaque_cursor>
```

```json
{
  "items": [],
  "next_cursor": null,
  "has_more": false
}
```

- `limit` 默认 20，最大 100。
- 游标为服务端不透明字符串，客户端不得解析或构造。
- 聊天历史游标包含 `(created_at, id)`，首屏返回最新消息，向上翻页获取更早消息。

### 3.4 错误响应

```json
{
  "error": {
    "code": "PLAN_REVISION_CONFLICT",
    "message": "计划版本已更新，请刷新后重试",
    "request_id": "uuid",
    "retryable": false,
    "details": {}
  }
}
```

客户端只能按稳定 `code` 分支，不解析 `message`。生产响应不得包含堆栈、SQL、对象存储 key、Prompt、模型思维链或内部工具参数。

### 3.5 条件更新

用户画像、学习时间等可编辑资源使用整数 `version` 乐观锁。更新请求携带 `expected_version`；版本不匹配返回 `409 RESOURCE_VERSION_CONFLICT`。

## 4. 页面与接口映射

| 页面/交互 | 接口 |
| --- | --- |
| 微信一键登录 | `POST /v1/auth/wechat/login` |
| 启动后恢复登录 | `POST /v1/auth/refresh`、`GET /v1/me` |
| 首页总览 | `GET /v1/dashboard` |
| 计划页 | `GET /v1/plans`、`GET /v1/tasks` |
| 任务勾选 | `POST /v1/tasks/{task_id}/executions` |
| 打开聊天 | `GET /v1/conversations`、`GET /v1/conversations/{id}/messages` |
| 发送文字/语音转写/附件 | `POST /v1/conversations/{id}/messages` |
| Agent 进度与流式回复 | `POST /v1/agent-runs/{id}/stream-ticket`、`GET /v1/agent-runs/{id}/events` |
| 选择卡提交 | `POST /v1/conversations/{id}/messages`；等待输入时恢复同一 run |
| 确认创建/确认调整 | `POST /v1/approvals/{approval_id}/decisions` |
| 继续修改 | 同上，`action=EDIT` 且携带 `feedback` |
| 查看计划历史 | `GET /v1/plans/{id}/revisions` |
| 查看引用来源 | `GET /v1/citations/{id}` |
| 上传文件/图片/扫描件 | `POST /v1/files/upload-intents`、直传对象存储、`POST /v1/files/{id}/complete` |
| 选择资料库文件 | `GET /v1/files?purpose=STUDY_MATERIAL&status=READY` |
| 设置 | `GET/PATCH /v1/me/preferences` |
| 退出登录 | `POST /v1/auth/logout` |
| 注销账号 | `DELETE /v1/me` |

## 5. 接口总览

### 5.1 认证与用户

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/auth/wechat/login` | 微信登录码换取令牌 |
| `POST` | `/v1/auth/refresh` | 轮换刷新令牌 |
| `POST` | `/v1/auth/logout` | 撤销当前刷新会话 |
| `GET` | `/v1/me` | 获取当前用户 |
| `PATCH` | `/v1/me` | 更新昵称、头像、语言和时区 |
| `GET`/`PUT` | `/v1/me/profile` | 获取/整体更新规划画像 |
| `GET`/`PUT` | `/v1/me/availability` | 获取/整体更新学习时间 |
| `GET`/`PATCH` | `/v1/me/preferences` | 获取/更新提醒偏好 |
| `DELETE` | `/v1/me` | 发起账号删除 |

### 5.2 首页、计划和任务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/dashboard` | 首页聚合数据 |
| `GET` | `/v1/goals` | 查询当前用户目标 |
| `GET` | `/v1/plans` | 查询计划摘要 |
| `GET` | `/v1/plans/{id}` | 查询计划和当前版本 |
| `GET` | `/v1/plans/{id}/revisions` | 查询版本历史 |
| `GET` | `/v1/plans/{id}/revisions/{revision_id}` | 查询指定完整版本 |
| `GET` | `/v1/tasks` | 按日期、计划和状态查询任务 |
| `GET` | `/v1/tasks/{id}` | 查询任务详情 |
| `POST` | `/v1/tasks/{id}/executions` | 提交完成、部分完成或跳过记录 |

MVP 不提供 `POST /goals`、`POST /plans`、`PATCH /plans` 或 `POST /tasks`。这些业务写入均由审批后的 Agent 工作流完成。

### 5.3 聊天、运行和审批

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/agent-definitions` | 获取专业 Agent 快捷入口目录 |
| `GET/POST` | `/v1/conversations` | 分页查询或幂等创建会话 |
| `GET` | `/v1/conversations/{id}` | 获取会话详情 |
| `GET/POST` | `/v1/conversations/{id}/messages` | 分页查询消息、发送或恢复等待输入的 run |
| `GET` | `/v1/agent-runs/{id}` | 查询运行状态和补偿信息 |
| `POST` | `/v1/agent-runs/{id}/stream-ticket` | 签发短期 SSE 票据 |
| `GET` | `/v1/agent-runs/{id}/events` | SSE 事件流 |
| `POST` | `/v1/agent-runs/{id}/cancel` | 幂等取消 |
| `POST` | `/v1/agent-runs/{id}/retry` | 从同一 checkpoint 重试 |
| `POST` | `/v1/approvals/{id}/decisions` | 批准、编辑或拒绝 |

### 5.4 文件与来源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/files/upload-intents` | 创建私有上传意图 |
| `POST` | `/v1/files/{id}/complete` | 通知服务端校验上传结果 |
| `GET` | `/v1/files/{id}` | 查询上传和扫描状态 |
| `GET` | `/v1/files` | 查询可复用资料库文件 |
| `GET` | `/v1/files/{id}/download-url` | 获取私有文件短期下载地址 |
| `DELETE` | `/v1/files/{id}` | 删除文件和关联向量 |
| `GET` | `/v1/citations/{id}` | 查询不可变引用快照和安全访问方式 |
| `GET` | `/v1/knowledge-documents/{id}/access-url` | 获取原文地址或短期签名地址 |

## 6. 认证与用户接口

### 6.1 微信登录

```http
POST /v1/auth/wechat/login
Idempotency-Key: 0190...
```

```json
{
  "schema_version": "1.0",
  "login_code": "one-time-wechat-code",
  "provider": "WECHAT_MINIAPP",
  "device_id": "installation-id",
  "consent": {
    "terms_version": "2026-08-01",
    "privacy_version": "2026-08-01",
    "accepted_at": "2026-08-09T08:00:00Z"
  }
}
```

`200 OK`：

```json
{
  "access_token": "opaque-or-jwt",
  "token_type": "Bearer",
  "expires_in": 1800,
  "refresh_token": "rotating-refresh-token",
  "refresh_expires_in": 2592000,
  "user": {
    "id": "uuid",
    "nickname": "微信用户",
    "avatar_url": null,
    "locale": "zh-CN",
    "timezone": "Asia/Shanghai",
    "status": "ACTIVE",
    "is_new_user": true
  }
}
```

服务端使用微信稳定主体创建或查询 `user_identities`，并幂等创建 `BUTLER user_agent`、初始 `CURRENT` 会话和 ACTIVE segment。不得把微信 session key 返回客户端或写普通日志。

手机号验证码登录和游客登录不属于 MVP。若后续启用，应新增独立验证码挑战表与限流/风控设计，不能接受原型中的“任意 4–6 位验证码”。

### 6.2 刷新与退出

```http
POST /v1/auth/refresh
```

```json
{
  "schema_version": "1.0",
  "refresh_token": "current-refresh-token",
  "device_id": "installation-id"
}
```

刷新令牌每次使用后轮换，数据库只保存 HMAC。旧令牌再次使用应撤销令牌族并返回 `401 REFRESH_TOKEN_REUSED`。

```http
POST /v1/auth/logout
```

```json
{"schema_version":"1.0","refresh_token":"current-refresh-token"}
```

成功返回 `204 No Content`，只撤销当前会话；其他设备会话不受影响。

### 6.3 当前用户

```http
GET /v1/me
```

```json
{
  "id": "uuid",
  "nickname": "张三",
  "avatar_file_id": null,
  "avatar_url": null,
  "locale": "zh-CN",
  "timezone": "Asia/Shanghai",
  "status": "ACTIVE",
  "created_at": "2026-08-09T08:00:00Z"
}
```

```http
PATCH /v1/me
```

```json
{
  "nickname": "张三",
  "avatar_file_id": "uuid-or-null",
  "locale": "zh-CN",
  "timezone": "Asia/Shanghai"
}
```

`avatar_file_id` 必须属于当前用户，且用途为 `AVATAR`、状态为 `VERIFIED + CLEAN`。

### 6.4 规划画像

```http
PUT /v1/me/profile
```

```json
{
  "schema_version": "1.0",
  "expected_version": 2,
  "education_level": "本科",
  "major": "计算机科学",
  "region_code": "CN-11",
  "current_level": "BASIC",
  "existing_material_file_ids": ["uuid"]
}
```

返回更新后的 `profile_version`。`existing_material_file_ids` 只接受当前用户可用的 `STUDY_MATERIAL` 文件；数据库 `existing_materials` 保存结构化摘要和文件 ID，不保存文件正文。

### 6.5 学习时间

```http
PUT /v1/me/availability
```

```json
{
  "schema_version": "1.0",
  "expected_version": 3,
  "windows": [
    {
      "day_of_week": null,
      "start_time": null,
      "end_time": null,
      "available_minutes": 60,
      "effective_from": "2026-08-09",
      "effective_to": null
    },
    {
      "day_of_week": 6,
      "start_time": "09:00:00",
      "end_time": "11:00:00",
      "available_minutes": 120,
      "effective_from": "2026-08-09",
      "effective_to": null
    }
  ]
}
```

服务端整体替换当前生效配置，并在单事务中校验时间窗不重叠。具体星期配置优先于默认每日配置。

### 6.6 偏好设置

```http
GET /v1/me/preferences
```

```json
{
  "version": 1,
  "task_reminder": {
    "enabled": true,
    "channels": ["IN_APP", "WECHAT"],
    "advance_minutes": 15,
    "quiet_hours": {"start":"22:00:00","end":"07:00:00"}
  },
  "plan_change_confirmation_required": true,
  "read_only_policies": ["plan_change_confirmation_required"]
}
```

```http
PATCH /v1/me/preferences
```

```json
{
  "expected_version": 1,
  "task_reminder": {
    "enabled": false,
    "channels": ["IN_APP"],
    "advance_minutes": 15
  }
}
```

原型中的“计划变更需确认”和“审核完成时显示来源入口”在 MVP 中是安全/产品策略，不允许关闭。当前数据库只需将可编辑提醒设置保存到 `user_profiles.notification_preferences`。

### 6.7 账号删除

```http
DELETE /v1/me
Idempotency-Key: 0190...
```

```json
{
  "schema_version": "1.0",
  "confirmation": "DELETE_MY_ACCOUNT"
}
```

返回 `202 Accepted`：

```json
{
  "status": "DELETING",
  "accepted_at": "2026-08-09T08:00:00Z"
}
```

服务端立即将用户置为不可登录并撤销会话，随后停止 run/通知，清理 Checkpointer、Store、Qdrant、对象存储和 PostgreSQL 用户数据。若需要保留账号但清除业务数据，必须先补充独立删除作业模型、范围和恢复策略，不能复用账号删除接口。

## 7. 首页、计划与任务接口

### 7.1 首页聚合

```http
GET /v1/dashboard?date=2026-08-09
```

`date` 默认为用户时区下的今天。

```json
{
  "date": "2026-08-09",
  "timezone": "Asia/Shanghai",
  "experience_state": "ACTIVE",
  "butler": {
    "status": "ONLINE",
    "active_specialist_count": 1,
    "summary": "今天有 2 项任务待完成"
  },
  "plan_summary": {
    "total": 1,
    "active": 1,
    "completed": 0
  },
  "task_summary": {
    "today_total": 3,
    "today_done": 1,
    "week_total": 8,
    "week_done": 3,
    "overloaded_minutes": 0
  },
  "plans": [
    {
      "id": "uuid",
      "title": "公务员备考",
      "status": "ACTIVE",
      "goal_type": "CIVIL_SERVICE_EXAM",
      "current_revision_id": "uuid",
      "weekly_minutes": 480,
      "progress": {"completed":3,"total":8,"percent":38}
    }
  ],
  "today_tasks": [
    {
      "id": "uuid",
      "plan_id": "uuid",
      "plan_title": "公务员备考",
      "title": "完成行测基础摸底",
      "scheduled_date": "2026-08-09",
      "due_at": null,
      "expected_minutes": 40,
      "priority": 3,
      "status": "TODO"
    }
  ]
}
```

`experience_state` 取值：

- `EMPTY`：没有 ACTIVE 计划，显示创建首计划入口。
- `ACTIVE`：至少一个 ACTIVE 计划。
- `ONBOARDING`：存在等待输入/等待审批的首计划 run。

进度百分比是响应派生值，不写入数据库。默认口径为当前 revision 已到期任务中 `DONE / 非 CANCELLED`；客户端不得自行计算另一套口径。

### 7.2 计划列表

```http
GET /v1/plans?status=ACTIVE&limit=20&cursor=...
```

支持：

- `status`: `DRAFT|ACTIVE|COMPLETED|CANCELLED`
- `goal_type`: MVP 为 `CIVIL_SERVICE_EXAM`
- `updated_after`: ISO 8601 时间点

列表项包含 `id`、`goal_id`、`title`、`status`、`current_revision_id`、当前版本日期范围、每周分钟数、任务统计和 `updated_at`。

### 7.3 计划详情和版本历史

```http
GET /v1/plans/{plan_id}
```

```json
{
  "id": "uuid",
  "goal": {
    "id": "uuid",
    "title": "准备 2027 国考",
    "goal_type": "CIVIL_SERVICE_EXAM",
    "target_date": "2026-11-29",
    "status": "ACTIVE"
  },
  "title": "公务员备考",
  "status": "ACTIVE",
  "current_revision": {
    "id": "uuid",
    "revision": 2,
    "status": "APPROVED",
    "objective_summary": "完成基础摸底并进入模块训练",
    "start_date": "2026-08-09",
    "end_date": "2026-11-28",
    "weekly_minutes": 360,
    "change_reason": "工作繁忙，减少本周任务",
    "approved_at": "2026-08-09T08:00:00Z",
    "stages": [],
    "task_templates": []
  },
  "progress": {"completed":4,"total":10,"percent":40},
  "updated_at": "2026-08-09T08:00:00Z"
}
```

```http
GET /v1/plans/{plan_id}/revisions?limit=20&cursor=...
```

默认只返回摘要；指定版本详情接口返回 stages 和 task templates。旧版本为 `SUPERSEDED`，不得原地编辑或重新标记为当前版本。若未来支持“恢复旧版本”，也必须复制为新 revision 并重新审批。

### 7.4 任务查询

```http
GET /v1/tasks?date_from=2026-08-09&date_to=2026-08-15&status=TODO,DOING&plan_id=uuid&limit=50
```

约束：

- `date_to - date_from` 最大 93 天。
- 多个状态用逗号分隔，允许 `TODO|DOING|DONE|SKIPPED|CANCELLED`。
- `scope=today|week` 可作为日期范围快捷参数，但不能与显式日期冲突。
- 所有计划和任务查询必须带认证用户过滤；跨用户 ID 对外统一返回 `404`。

### 7.5 任务执行

```http
POST /v1/tasks/{task_id}/executions
```

```json
{
  "schema_version": "1.0",
  "client_execution_id": "0190...",
  "result": "COMPLETED",
  "duration_minutes": 38,
  "feedback": "资料分析部分较弱",
  "outcome_data": {"correct":16,"total":20},
  "occurred_at": "2026-08-09T08:00:00Z"
}
```

`201 Created`：

```json
{
  "execution": {
    "id": "uuid",
    "task_id": "uuid",
    "result": "COMPLETED",
    "duration_minutes": 38,
    "occurred_at": "2026-08-09T08:00:00Z"
  },
  "task": {
    "id": "uuid",
    "status": "DONE",
    "completed_at": "2026-08-09T08:00:00Z"
  }
}
```

规则：

- `PARTIAL` 可提交多次，不自动把任务置为 `DONE`。
- 首次 `COMPLETED` 原子更新任务为 `DONE`。
- 相同 `client_execution_id` 返回首次结果，不重复累计时长。
- 已完成任务再次提交 `COMPLETED` 返回当前完成状态，不增加统计。
- `SKIPPED` 将任务置为 `SKIPPED`；是否顺延由后续 Agent 调整流程决定。
- 当前数据库没有“撤销完成”的可靠审计模型，因此 UI 不应允许直接取消已完成勾选。

## 8. 聊天与 Agent Run 接口

> 0.1 预发布契约重置：本节多会话接口已直接替换 `/v1/chat`，不提供旧客户端兼容层。

### 8.1 Agent 快捷入口目录

`GET /v1/agent-definitions` 返回 `code`、名称、说明、图标、`AVAILABLE | COMING_SOON`、欢迎语和推荐问题。`BUTLER` 为隐藏能力；未开放 code 创建会话时返回 `409 AGENT_NOT_AVAILABLE`。

### 8.2 会话列表与创建

```http
GET /v1/conversations?limit=30&cursor=<opaque_cursor>
```

列表按 `CURRENT` 优先、最近消息倒序返回 `id`、`title`、`status`、`specialist`、`last_message`、`last_message_at` 和 `active_run`，不暴露 User Agent ID。

```http
POST /v1/conversations
```

```json
{"schema_version":"1.0","client_conversation_id":"uuid","specialist_code":"CIVIL_SERVICE_EXAM"}
```

`specialist_code` 为空创建普通会话。服务端锁用户行、检查全局 run、归档当前会话并创建新会话；专业欢迎语作为静态 Assistant 消息持久化但不创建 run。`(user_id, client_conversation_id)` 保证创建幂等。

`GET /v1/conversations/{id}` 读取详情。会话归属从认证用户解析，跨用户访问统一返回 404。

### 8.3 消息历史

```http
GET /v1/conversations/{id}/messages?limit=30&cursor=<opaque_cursor>
```

```json
{
  "items": [
    {
      "id": "uuid",
      "role": "ASSISTANT",
      "status": "COMPLETED",
      "content": "我先确认你每天可投入的时间。",
      "cards": [],
      "attachments": [],
      "agent_run_id": "uuid",
      "created_at": "2026-08-09T08:00:00Z"
    }
  ],
  "next_cursor": null,
  "has_more": false
}
```

只返回 `USER`、`ASSISTANT`、`SYSTEM_EVENT` 可展示消息，不返回 checkpoint、隐藏系统消息、思维链、内部摘要或专业 Agent 原始输出。

### 8.4 发送消息

```http
POST /v1/conversations/{id}/messages
```

```json
{
  "schema_version": "1.0",
  "client_message_id": "0190...",
  "content": "我准备参加 2027 年国考，每天可以学习 2 小时",
  "attachments": [
    {"file_id":"uuid","position":0}
  ],
  "selection": null
}
```

`content` 和 `attachments` 至少一个非空。建议限制：文本 20,000 字符、附件最多 9 个、总大小按用途策略限制。

若目标为 `ARCHIVED`，服务端在同一事务恢复目标并归档此前 `CURRENT`。新建、恢复或发送前若用户已有其他非终态 run，返回 `409 GLOBAL_RUN_IN_PROGRESS` 并附当前 run 和会话 ID。

`selection` 仅用于回答当前 `SelectionCard`，示例：

```json
{
  "card_id": "uuid",
  "action_id": "submit-plan-target",
  "selected_option_ids": ["plan-uuid"]
}
```

`202 Accepted`：

```json
{
  "schema_version": "1.0",
  "conversation_id": "uuid",
  "user_message": {"id":"uuid","status":"COMPLETED"},
  "assistant_message": {"id":"uuid","status":"PENDING"},
  "run": {
    "id": "uuid",
    "status": "QUEUED",
    "execution_mode": "START",
    "attempt": 0
  },
  "stream": {
    "events_url": "/v1/agent-runs/uuid/events",
    "ticket": "short-lived-signed-ticket",
    "expires_at": "2026-08-09T08:10:00Z",
    "last_sequence": 1
  }
}
```

`execution_mode`：

- `START`：没有活动 run，创建新 run。
- `INPUT_RESUME`：当前 run 为 `AWAITING_INPUT`，将本消息作为结构化输入恢复同一 run。

活动 run 分支：

| 当前状态 | 行为 |
| --- | --- |
| 无活动 run | 创建消息、Assistant 占位和新 run |
| `AWAITING_INPUT` | 恢复同一 run |
| `AWAITING_APPROVAL` | `409 APPROVAL_REQUIRED` |
| `QUEUED/RUNNING/CANCEL_REQUESTED` | `409 CONVERSATION_BUSY` |
| `FAILED_RETRYABLE` | `409 RUN_RETRY_REQUIRED` |

附件必须属于当前用户、用途为 `CHAT_ATTACHMENT`，且为 `VERIFIED + CLEAN`。客户端只能传 `file_id`，不能传对象 key 或永久公开 URL。

### 8.5 Run 查询

```http
GET /v1/agent-runs/{run_id}
```

```json
{
  "schema_version": "2.0",
  "run_id": "uuid",
  "status": "AWAITING_APPROVAL",
  "attempt": 1,
  "last_sequence": 42,
  "response_message": {"id":"uuid","status":"COMPLETED"},
  "data": {},
  "citations": [],
  "warnings": [],
  "next_action": {
    "type": "REVIEW_PLAN",
    "approval_id": "uuid",
    "approval_version": 1
  },
  "error": null,
  "created_at": "2026-08-09T08:00:00Z",
  "updated_at": "2026-08-09T08:00:20Z"
}
```

Run 状态：`QUEUED`、`RUNNING`、`AWAITING_INPUT`、`AWAITING_APPROVAL`、`SUCCEEDED`、`FAILED_RETRYABLE`、`FAILED_FINAL`、`CANCEL_REQUESTED`、`CANCELLED`。

### 8.5 取消与重试

```http
POST /v1/agent-runs/{run_id}/cancel
Idempotency-Key: 0190...
```

无请求体，返回当前 run 状态。终态 run 重复取消不回退状态。

```http
POST /v1/agent-runs/{run_id}/retry
```

```json
{"schema_version":"1.0","expected_attempt":1}
```

只允许匹配的 `FAILED_RETRYABLE` run 原子增加 attempt，并从同一 checkpoint 重试；不得创建新 run。

## 9. 结构化卡片与审批

### 9.1 卡片信封

卡片保存在 `messages.structured_content.cards`：

```json
{
  "schema_version": "1.0",
  "card_id": "uuid",
  "card_type": "SelectionCard",
  "entity_refs": {},
  "payload": {},
  "actions": []
}
```

| 类型 | 必需实体 | 用途 | 动作 |
| --- | --- | --- | --- |
| `SelectionCard` | 稳定 option/entity ID | 缺失信息或选择唯一目标计划 | `SUBMIT_SELECTION` |
| `PlanCard` | `approval_id`、版本和 revision 项 | 组合创建或单计划调整草案 | `APPROVE`、`EDIT`、`REJECT` |
| `StatusCard` | `run_id` | 展示预定义进度 | 可选 `CANCEL_RUN` |
| `SourceCard` | 有序 `citation_ids` 和来源摘要 | 展示回答引用来源 | `OPEN_SOURCE` |

客户端不得根据标题、顺序、按钮文案或临时 key 推导业务 ID。未知 `schema_version` 或 `card_type` 时只显示文本，不展示写操作按钮。

### 9.2 单计划调整卡

```json
{
  "schema_version": "1.0",
  "card_id": "uuid",
  "card_type": "PlanCard",
  "entity_refs": {
    "approval_id": "uuid",
    "approval_version": 1,
    "items": [
      {
        "plan_id": "uuid",
        "plan_revision_id": "new-revision-uuid",
        "expected_current_revision_id": "current-revision-uuid"
      }
    ]
  },
  "payload": {
    "mode": "SINGLE_PLAN_ADJUST",
    "title": "公务员备考 · 调整后",
    "plans": [],
    "unchanged_plan_ids": ["other-plan-uuid"],
    "warnings": []
  },
  "actions": [
    {"action_id":"approve","action_type":"APPROVE","label":"确认调整"},
    {"action_id":"edit","action_type":"EDIT","label":"继续说明"},
    {"action_id":"reject","action_type":"REJECT","label":"放弃调整"}
  ]
}
```

`SINGLE_PLAN_ADJUST` 必须且只能包含一个审批项。服务端再次校验目标计划属于当前用户且 `expected_current_revision_id` 仍为当前版本。

### 9.3 组合创建卡

组合创建使用 `payload.mode=BUNDLE_CREATE`，每个新计划各有一个 `plan_revision_id`，`expected_current_revision_id=null`。全部 revision 整组批准或整组失败，不允许部分成功。

### 9.4 提交审批

```http
POST /v1/approvals/{approval_id}/decisions
```

批准：

```json
{
  "schema_version": "1.0",
  "action": "APPROVE",
  "expected_approval_version": 1,
  "feedback": null
}
```

继续修改：

```json
{
  "schema_version": "1.0",
  "action": "EDIT",
  "expected_approval_version": 1,
  "feedback": "把周四的申论任务移到周六"
}
```

拒绝：

```json
{
  "schema_version": "1.0",
  "action": "REJECT",
  "expected_approval_version": 1,
  "feedback": "暂时不创建"
}
```

`202 Accepted`：

```json
{
  "approval": {
    "id": "uuid",
    "status": "APPROVED",
    "approval_version": 2,
    "decided_at": "2026-08-09T08:00:00Z"
  },
  "run": {
    "id": "uuid",
    "status": "QUEUED",
    "execution_mode": "APPROVAL_RESUME"
  },
  "assistant_message": {"id":"uuid","status":"PENDING"},
  "stream": {"events_url":"/v1/agent-runs/uuid/events","last_sequence":43}
}
```

审批事务必须：锁定审批和全部相关计划、校验审批版本及当前 revision、保存唯一决定、创建 `SYSTEM_EVENT` 和新 Assistant 占位消息、把同一 run 设置为 `APPROVAL_RESUME`。真正发布 revision 和物化任务由恢复后的幂等节点完成。

`EDIT` 是本次审批的终态 `EDITED`。Worker 使用反馈生成新的 revision 和新的审批记录；客户端不能继续使用旧 `approval_id`。

## 10. SSE 事件流

### 10.1 流票据

```http
POST /v1/agent-runs/{run_id}/stream-ticket
```

```json
{
  "ticket": "signed-ticket",
  "expires_at": "2026-08-09T08:10:00Z",
  "events_url": "/v1/agent-runs/uuid/events"
}
```

票据默认 10 分钟有效，只绑定当前 `user_id + run_id + purpose=chat_stream`，不包含可逆个人信息。已经建立的合法连接不因票据中途过期被强制断开。

### 10.2 建立和续传

```http
GET /v1/agent-runs/{run_id}/events?ticket=<ticket>&after=41
Accept: text/event-stream
Last-Event-ID: 41
```

`after` 和 `Last-Event-ID` 同时存在时必须相同。响应头：

```text
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

事件帧：

```text
id: 42
event: message.delta
data: {"schema_version":"1.0","run_id":"uuid","sequence":42,"created_at":"2026-08-09T08:00:00Z","payload":{"message_id":"uuid","delta":"下一步建议","attempt":1}}

```

### 10.3 事件类型

| 事件 | 持久化 | 说明 |
| --- | --- | --- |
| `run.accepted` | 是 | 消息事务完成并进入队列 |
| `run.status` | 是 | 状态变化 |
| `progress` | 是 | 预定义用户可见进度 |
| `message.start` | 是 | Assistant 开始输出 |
| `message.delta` | 是 | 合并后的文本增量 |
| `message.reset` | 是 | 重试时清空旧 attempt 增量 |
| `message.completed` | 是 | 最终文本和卡片已落库 |
| `interrupt` | 是 | 等待输入或审批 |
| `run.completed` | 是 | run 成功结束 |
| `run.cancelled` | 是 | run 已取消 |
| `error` | 是 | 安全错误信息和可重试标识 |
| `heartbeat` | 否 | 每 15 秒保活，无 ID |

`progress.payload.code` 只允许：

- `SEARCHING_WEB`：正在检索网络
- `RETRIEVING_PRIVATE`：正在检索我的资料
- `ORGANIZING_CITATIONS`：正在整理引用
- `GENERATING_ANSWER`：正在生成回答

客户端按 `run_id + sequence` 去重。断线后按 1、2、5、10、30 秒上限指数退避并携带最后完整序列；断线期间 run 继续。事件过期返回 `410 STREAM_CURSOR_EXPIRED`，客户端重新查询消息和 run 状态。

## 11. 文件与附件接口

### 11.1 创建上传意图

```http
POST /v1/files/upload-intents
Idempotency-Key: 0190...
```

```json
{
  "schema_version": "1.0",
  "purpose": "CHAT_ATTACHMENT",
  "filename": "国考大纲.pdf",
  "declared_mime_type": "application/pdf",
  "size_bytes": 2480123,
  "sha256": "64-char-lowercase-hex"
}
```

`201 Created`：

```json
{
  "file": {
    "id": "uuid",
    "purpose": "CHAT_ATTACHMENT",
    "original_filename": "国考大纲.pdf",
    "upload_status": "PENDING",
    "scan_status": "PENDING"
  },
  "upload": {
    "method": "PUT",
    "url": "short-lived-private-upload-url",
    "headers": {"Content-Type":"application/pdf"},
    "expires_at": "2026-08-09T08:10:00Z"
  }
}
```

服务端生成不可猜测的对象 key。响应不得返回可长期复用的公开 URL。

### 11.2 完成上传和查询状态

客户端直传成功后调用：

```http
POST /v1/files/{file_id}/complete
Idempotency-Key: 0190...
```

```json
{"schema_version":"1.0","sha256":"64-char-lowercase-hex"}
```

服务端校验对象大小、真实 MIME 和哈希，进入安全扫描。返回 `202 Accepted`。客户端轮询：

```http
GET /v1/files/{file_id}
```

只有 `upload_status=VERIFIED` 且 `scan_status=CLEAN` 时，文件状态为客户端语义的 `READY`，才可发送聊天消息。

### 11.3 文件库和下载

```http
GET /v1/files?purpose=STUDY_MATERIAL&status=READY&limit=20
```

仅返回当前用户私有文件的元数据。公共知识库不是用户文件列表，应通过 SourceCard 暴露引用来源。

```http
GET /v1/files/{file_id}/download-url
```

```json
{
  "url": "short-lived-signed-url",
  "expires_at": "2026-08-09T08:05:00Z",
  "filename": "国考大纲.pdf"
}
```

### 11.4 删除文件

```http
DELETE /v1/files/{file_id}
```

返回 `202 Accepted`。服务端立即把文件标记为不可下载，再异步删除知识向量和对象文件。若文件已被未发送的客户端草稿引用，客户端自行移除；若已被历史消息引用，历史消息保留文件名和“已删除”状态。

## 12. Citation 与来源接口

### 12.1 查询引用快照

```http
GET /v1/citations/{citation_id}
```

```json
{
  "id": "uuid",
  "source_type": "WEB",
  "title": "国家公务员考试公共科目大纲",
  "source_organization": "国家公务员局",
  "domain": "scs.gov.cn",
  "published_at": "2026-10-14T00:00:00Z",
  "retrieved_at": "2026-10-15T09:30:00Z",
  "evidence_excerpt": "短证据片段",
  "access": {
    "type": "EXTERNAL_URL",
    "url": "https://example.gov.cn/document",
    "expires_at": null
  }
}
```

`source_type` 为 `WEB | PRIVATE_FILE | KNOWLEDGE`。网页地址必须通过安全 URL 校验；私有文件每次查询都重新校验所有权并返回短期签名地址。已删除或无权访问的实体统一返回 `404`；历史引用快照仍可展示，但 `access.type` 为 `UNAVAILABLE`。

## 13. 状态机和关键流程

### 13.1 首计划/组合计划创建

```text
POST chat message
  → run QUEUED/RUNNING
  → 信息不足：AWAITING_INPUT + SelectionCard
  → 用户补充信息，INPUT_RESUME 同一 run
  → 检索、审核、规划
  → 写 DRAFT/IN_REVIEW/PENDING_APPROVAL revisions
  → AWAITING_APPROVAL + PlanCard
  → APPROVE
  → 同一 run APPROVAL_RESUME
  → 原子发布全部 revisions
  → 幂等物化未来 7 天 tasks/notifications
  → run SUCCEEDED
```

### 13.2 单计划调整

```text
用户提出调整
  → 未唯一命中计划：SelectionCard
  → 选定一个 plan_id
  → 只读取目标计划完整版本；其他计划仅提供负荷摘要
  → 生成目标计划的新完整 revision
  → PlanCard(mode=SINGLE_PLAN_ADJUST)
  → APPROVE/EDIT/REJECT
```

批准事务必须比较 `expected_current_revision_id`。批准后旧版本变为 `SUPERSEDED`，新版本变为 `APPROVED` 并成为 `plans.current_revision_id`；其他计划不得产生新 revision 或任务变更。

### 13.3 任务与通知

- 任务按 `(plan_revision_id, task_key)` 幂等创建。
- 新 revision 批准后，对旧 revision 尚未执行的未来任务应按确定性规则取消，再物化新 revision 未来 7 天任务。
- 任务和提醒在同一后续事务中创建；通知使用业务幂等键。
- 外部通知发送失败不能回滚已批准计划或已创建任务。

## 14. 数据库映射

| API 资源 | 主表 | 说明 |
| --- | --- | --- |
| 用户 | `users` | 展示资料、时区和账号状态 |
| 登录身份 | `user_identities` | 微信稳定主体 |
| 刷新会话 | `auth_sessions` | 只保存刷新令牌 HMAC |
| 画像/偏好 | `user_profiles` | 画像、提醒 JSON、乐观锁 |
| 可用时间 | `study_availability` | 默认与每周时间窗 |
| 主聊天 | `conversations` | 每用户一个 BUTLER 会话 |
| 消息 | `messages` | 用户可见消息和卡片 |
| 附件 | `message_attachments`、`stored_files` | 稳定顺序和文件归属 |
| Run/SSE | `agent_runs`、`agent_run_events` | 状态、恢复摘要和展示事件 |
| 审批 | `approval_decisions`、`approval_decision_items` | 一次审批关联一个或多个 revision |
| 目标/计划 | `goals`、`plans`、`plan_revisions` | 逻辑实体与不可变版本 |
| 阶段/模板 | `plan_stages`、`plan_task_templates` | 批准前完整计划结构 |
| 任务/执行 | `tasks`、`task_executions` | 正式任务和幂等打卡 |
| 来源 | `knowledge_documents`、`knowledge_chunks` | 权威元数据和向量映射 |
| 内部事实/引用 | `claims`、`citations` | SourceCard 依据；客户端不直接消费 Claim |
| 通知 | `notification_jobs` | 提醒状态、重试和渠道幂等 |

聚合响应字段如首页进度、今日统计和超负荷分钟数均为查询派生值，不新增重复事实字段。

## 15. 错误码

### 15.1 通用

| HTTP | code | 含义 |
| --- | --- | --- |
| `400` | `VALIDATION_ERROR` | 请求格式或字段不合法 |
| `401` | `UNAUTHENTICATED` | 未登录或访问令牌失效 |
| `401` | `REFRESH_TOKEN_REUSED` | 已轮换刷新令牌被再次使用 |
| `403` | `ACCOUNT_UNAVAILABLE` | 账号暂停或正在删除 |
| `404` | `RESOURCE_NOT_FOUND` | 资源不存在或不属于当前用户 |
| `409` | `IDEMPOTENCY_KEY_REUSED` | 幂等键被用于不同请求 |
| `409` | `RESOURCE_VERSION_CONFLICT` | 资源版本已变化 |
| `413` | `PAYLOAD_TOO_LARGE` | 文本、文件或附件数量超限 |
| `415` | `UNSUPPORTED_MEDIA_TYPE` | 不支持的文件类型 |
| `429` | `RATE_LIMITED` | 频率受限 |
| `500` | `INTERNAL_ERROR` | 不可展示的服务端错误 |
| `503` | `SERVICE_UNAVAILABLE` | 依赖暂不可用 |

### 15.2 聊天和计划

| HTTP | code | 含义 |
| --- | --- | --- |
| `400` | `INVALID_STREAM_CURSOR` | SSE 游标不一致或格式错误 |
| `401/403` | `STREAM_TICKET_INVALID` | 流票据无效或不属于当前 run |
| `409` | `CONVERSATION_BUSY` | 会话已有执行中 run |
| `409` | `APPROVAL_REQUIRED` | 当前必须提交结构化审批 |
| `409` | `RUN_RETRY_REQUIRED` | 当前 run 等待重试或取消 |
| `409` | `RUN_ATTEMPT_CONFLICT` | 重试 attempt 已变化 |
| `409` | `APPROVAL_VERSION_CONFLICT` | 审批已变化、过期或终结 |
| `409` | `PLAN_REVISION_CONFLICT` | 当前计划版本已变化 |
| `409` | `ATTACHMENT_NOT_READY` | 附件未验证、未通过扫描或用途不符 |
| `410` | `STREAM_CURSOR_EXPIRED` | SSE 所需事件已清理 |
| `422` | `UNSUPPORTED_GOAL_TYPE` | MVP 未开放该目标类型 |
| `422` | `AMBIGUOUS_TARGET_PLAN` | 调整请求没有唯一目标计划 |
| `422` | `INSUFFICIENT_EVIDENCE` | 无可靠资料支撑事实性结论 |

## 16. 安全、隐私和限流

- 每个用户数据查询都必须直接带 `user_id`，或通过带用户条件的父表关联。
- 跨用户读写统一返回 `404`，避免枚举资源。
- 原始聊天、手机号、文件正文、Prompt 和流式文本不得写普通日志。
- 上传文件必须做服务端 MIME 检测、大小/哈希校验、病毒扫描和私有存储。
- 外部 URL 只允许 HTTP(S)，防止 `file:`、内网地址、重定向绕过和 SSRF。
- SSE 票据和上传/下载签名 URL 必须从代理访问日志中脱敏。
- 建议默认限流：登录 10 次/设备/小时；发送消息 30 次/用户/分钟；上传意图 20 次/用户/小时；SSE 同一 run 最多 3 个并发连接。
- API 请求不能在 HTTP 协程中同步等待模型完成；长任务全部进入 Worker。

## 17. 待补充或需产品确认项

### 17.1 当前设计已作出的默认决定

1. “清除全部数据”改为“注销账号”；不提供保留账号的数据清空。
2. MVP 只实现微信登录；手机号和游客入口移除或明确标注演示。
3. 任务完成后不能直接取消勾选；如需支持，应新增任务状态变更审计或 reversal 模型。
4. 语音在客户端完成转写后作为普通文本发送；不保存原始音频。若需上传语音，需扩展文件 purpose、MIME 和转写作业。
5. “计划变更需确认”是只读策略；仅在 RAG 实际返回引用时展示 SourceCard。

### 17.2 数据库设计缺口

- 登录接口要求记录用户接受的用户协议和隐私政策版本，但当前数据库没有 consent 审计表或字段。上线前必须补充不可抵赖的同意记录。
- `DELETE /v1/me` 需要可重试地推进跨 PostgreSQL、LangGraph、Qdrant 和对象存储的删除步骤，当前数据库没有账号删除作业表。实现前应增加删除作业或明确复用通用作业设施。
- 学习时间整体更新需要集合级版本；当前 `study_availability` 只有行级时间字段，没有集合版本。建议在 `user_profiles` 增加 `availability_version`，或新增用户配置版本表。
- Citation 必须保存标题、机构、域名、发布时间、检索时间和排序快照，避免来源更新或私有文件删除后改变历史消息编号。
- 上传完成和安全扫描需要后台作业状态；当前只有文件状态字段，验证阶段可由现有 Worker 扫描，规模扩大后应增加通用作业表。
- 新 revision 批准后，旧 revision 的未来未执行任务如何取消、迁移或保留需要确定性规则；当前数据库能表达 `CANCELLED`，但详细设计尚未固定转换口径。

## 18. 验收场景

1. 新用户微信登录后，重复请求不会创建多个用户、BUTLER 实例或主聊天。
2. 空首页返回 `experience_state=EMPTY`，创建首计划只能通过聊天和 PlanCard 审批完成。
3. 信息不足时同一 run 进入 `AWAITING_INPUT`，补充信息后恢复原 run。
4. 文本“确认”不会批准计划；只有审批接口会发布 revision。
5. 组合新建任一 revision 冲突时全部回滚。
6. 单计划调整只生成一个 revision，其他计划 ID 的当前版本和任务不变。
7. 重复消息、重复审批、重复任务完成均不会产生重复副作用。
8. SSE 断线重连后按 sequence 补齐，文本不重复，run 不被取消。
9. 未扫描、跨用户或用途不符的附件不能绑定消息。
10. SourceCard 只展示 `SUPPORTED + PASS` 的结论，且不能访问其他用户私有来源。
11. 跨用户的计划、任务、run、审批、文件和来源请求全部拒绝。
12. 账号删除开始后用户不可继续登录，外围存储失败可重试且不会恢复可见性。
