# AI个人管家数据库设计文档 V2.5

## 1. 目标与存储边界

本文定义 10 人以内产品验证阶段的数据库基线，并保证可演进到 100–1,000 用户。

| 存储 | 保存内容 | 不保存内容 |
| --- | --- | --- |
| PostgreSQL | 用户、多会话、分段、消息、摘要、记忆作业/屏障、目标、计划、任务、引用元数据、审批和审计事实 | embedding 向量、对象文件正文 |
| LangGraph Checkpointer | 某个分段 `thread_id` 的图状态、节点进度、中断和恢复点 | 用户可见业务事实的唯一副本 |
| LangGraph PostgreSQL Store | 当前有效的跨归档分段用户语义记忆及 pgvector embedding | 原始聊天、业务事实、记忆准入和遗忘规则 |
| Qdrant | 知识分块 embedding 和检索 payload | 文档权威元数据和访问控制真相 |
| 对象存储 | 用户文件和知识原文件 | 可直接公开访问的永久 URL |

PostgreSQL 是业务事实唯一来源；外围存储失败时通过状态字段和补偿重试实现最终一致。

## 2. 全局规范

### 2.1 命名与类型

- 表名、字段名使用小写 `snake_case`，表名使用复数。
- 主键统一为 `uuid`，默认由应用或 `gen_random_uuid()` 生成。
- 时间点统一为 `timestamptz`，日期使用 `date`，日内时间使用 `time`。
- 金额使用 `numeric(12,4)`，时长统一保存整数分钟。
- 灵活配置和模型结构化结果使用 `jsonb`；核心关联和状态不得只存在 JSON 中。
- 状态字段使用 `varchar` + `CHECK`，避免 PostgreSQL Enum 带来的迁移限制。
- 所有可变业务表包含 `created_at`、`updated_at`；不可变事件表至少包含 `created_at`。
- 所有文本字段明确长度；用户或模型长文本使用 `text` 并在 API 层限制大小。

### 2.2 通用规则

- `updated_at` 由统一数据库触发器或仓储层更新，禁止各业务模块自行约定。
- 用户身份来自服务端认证上下文，不从请求体读取。
- 用户拥有的数据必须存在直接 `user_id`，或存在单一、可索引的用户归属路径。
- 软删除字段统一为 `deleted_at timestamptz`；普通查询必须排除已删除记录。
- 外键默认 `ON DELETE RESTRICT`；明确属于用户且无独立审计价值的数据可 `CASCADE`。
- 生产备份和日志不得依赖 Row Level Security 过滤；备份使用专用角色。

### 2.3 状态值书写

数据库状态保存大写英文，例如 `ACTIVE`、`PENDING_APPROVAL`。API 和 Agent Schema 使用相同值，避免转换映射。

## 3. 用户与认证

### 3.1 `users`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `nickname` | `varchar(64)` | 是 | 展示昵称 |
| `avatar_file_id` | `uuid` | 是 | 后续添加到 `stored_files.id` 的外键 |
| `phone_ciphertext` | `text` | 是 | 加密手机号，不写日志 |
| `phone_hash` | `char(64)` | 是 | 规范化手机号的 HMAC，用于唯一查询 |
| `email_ciphertext` | `text` | 是 | 加密邮箱 |
| `email_hash` | `char(64)` | 是 | 规范化邮箱的 HMAC |
| `locale` | `varchar(16)` | 否 | 默认 `zh-CN` |
| `timezone` | `varchar(64)` | 否 | 默认 `Asia/Shanghai`，必须是 IANA 时区 |
| `status` | `varchar(16)` | 否 | `ACTIVE`、`SUSPENDED`、`DELETING`、`DELETED` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |
| `deleted_at` | `timestamptz` | 是 | 软删除时间 |

约束与索引：

- `phone_hash`、`email_hash` 分别建立非空唯一索引。
- `status = 'DELETED'` 时 `deleted_at` 必须非空。
- `status` 建立普通索引，用于删除补偿和后台治理。

### 3.2 `user_identities`

用于微信及未来其他登录方式，不把供应商身份字段放入 `users`。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `provider` | `varchar(32)` | 否 | `WECHAT_MINIAPP`、`WECHAT_H5` 等 |
| `provider_subject` | `varchar(255)` | 否 | openid 或其他稳定主体标识 |
| `union_subject` | `varchar(255)` | 是 | 可选 unionid |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `last_login_at` | `timestamptz` | 是 | 最近登录时间 |

约束与索引：

- 唯一约束：`(provider, provider_subject)`。
- 索引：`user_id`。

### 3.3 `auth_sessions`

保存可撤销的刷新会话；数据库只保存刷新令牌哈希，不保存明文令牌。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `refresh_token_hash` | `char(64)` | 否 | 带服务端密钥的 HMAC |
| `device_id` | `varchar(128)` | 是 | 客户端安装或浏览器设备标识 |
| `status` | `varchar(16)` | 否 | `ACTIVE`、`REVOKED`、`EXPIRED` |
| `expires_at` | `timestamptz` | 否 | 过期时间 |
| `last_used_at` | `timestamptz` | 是 | 最近刷新时间 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `revoked_at` | `timestamptz` | 是 | 撤销时间 |

约束与索引：

- `refresh_token_hash` 全局唯一。
- `expires_at > created_at`；`REVOKED` 要求 `revoked_at` 非空。
- 索引：`(user_id, status, expires_at)`。

### 3.3.1 `phone_verification_challenges`

保存登录前的一次性短信挑战。只持久化手机号 HMAC 和验证码 HMAC，不保存明文；状态为
`PENDING`、`SENT`、`FAILED`、`CONSUMED`、`LOCKED` 或 `EXPIRED`。挑战绑定
`device_id`，默认五分钟过期、最多校验五次，并按手机号和设备的创建时间索引执行限流。

### 3.4 `user_profiles`

保存 Profile 节点收集、且用户可查看和修改的规划信息。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `user_id` | `uuid` | 否 | 主键、外键 → `users.id`，删除级联 |
| `education_level` | `varchar(32)` | 是 | 学历 |
| `major` | `varchar(128)` | 是 | 专业 |
| `region_code` | `varchar(32)` | 是 | 标准地区编码 |
| `current_level` | `varchar(32)` | 是 | `BEGINNER`、`BASIC`、`INTERMEDIATE`、`ADVANCED` |
| `existing_materials` | `jsonb` | 否 | 默认 `[]`，仅保存结构化摘要和文件 ID |
| `notification_preferences` | `jsonb` | 否 | 默认 `{}`，渠道、静默时间等 |
| `profile_version` | `integer` | 否 | 默认 1，乐观锁，必须大于 0 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

目标考试和目标日期属于 `goals`，不得在画像中重复维护。

### 3.5 `study_availability`

一行表示默认每日时长，或每周某一天的一段可学习时间。`day_of_week` 为空的记录是默认每日配置；具体星期配置优先于默认配置。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `day_of_week` | `smallint` | 是 | 1–7，1 表示周一；空表示默认每日配置 |
| `start_time` | `time` | 是 | 可选本地开始时间 |
| `end_time` | `time` | 是 | 与 `start_time` 同时为空或同时存在 |
| `available_minutes` | `smallint` | 否 | 1–1,440 |
| `effective_from` | `date` | 否 | 默认当前日期 |
| `effective_to` | `date` | 是 | 不得早于 `effective_from` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- `day_of_week` 为空或在 1–7 之间。
- `start_time`、`end_time` 必须同时为空或同时非空；非空时 `end_time > start_time`。
- `effective_to` 为空或不早于 `effective_from`。
- 索引：`(user_id, day_of_week, effective_from)`。
- 业务层阻止同一生效期内重复的默认配置和相互重叠的具体时间窗。

## 4. Agent 定义与用户实例

### 4.1 `agent_definitions`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `code` | `varchar(64)` | 否 | 稳定代码，例如 `CIVIL_SERVICE_EXAM` |
| `version` | `integer` | 否 | 定义版本，必须大于 0 |
| `name` | `varchar(128)` | 否 | 展示名称 |
| `description` | `text` | 是 | 描述 |
| `graph_name` | `varchar(128)` | 否 | LangGraph 图名称 |
| `status` | `varchar(16)` | 否 | `DRAFT`、`ACTIVE`、`RETIRED` |
| `catalog_status` | `varchar(16)` | 否 | `AVAILABLE`、`COMING_SOON`、`HIDDEN` |
| `display_order` | `smallint` | 否 | 快捷入口排序 |
| `catalog_metadata` | `jsonb` | 否 | 图标、欢迎语与推荐问题 |
| `default_config` | `jsonb` | 否 | 默认 `{}` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

唯一约束：`(code, version)`。同一个 `code` 只允许一个 `ACTIVE` 版本，使用部分唯一索引实现。

### 4.2 `user_agents`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `agent_definition_id` | `uuid` | 否 | 外键 → `agent_definitions.id`，删除限制 |
| `status` | `varchar(16)` | 否 | `INIT`、`ACTIVE`、`PAUSED`、`COMPLETED` |
| `config` | `jsonb` | 否 | 默认 `{}`，只允许受支持配置 |
| `activated_at` | `timestamptz` | 是 | 首次激活时间 |
| `completed_at` | `timestamptz` | 是 | 完成时间 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(user_id, agent_definition_id)`。
- 索引：`(user_id, status)`。
- `COMPLETED` 状态要求 `completed_at` 非空。

系统为每个用户幂等创建一个 `BUTLER` User Agent。所有用户可见会话都绑定该实例；专业会话另以 `specialist_user_agent_id` 固定路由。`AVAILABLE` 目录项必须对应 `ACTIVE` 定义；`COMING_SOON` 不创建 User Agent 且不可执行。MVP 中 `BUTLER` 为 `HIDDEN`、考公为 `AVAILABLE`、雅思与求职为 `COMING_SOON`。

## 5. 会话、消息与运行

### 5.1 `conversations`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `user_agent_id` | `uuid` | 否 | 外键 → `user_agents.id`，删除限制 |
| `client_conversation_id` | `uuid` | 否 | 客户端创建幂等键 |
| `title` | `varchar(200)` | 否 | 安全截断后的用户可见标题 |
| `status` | `varchar(16)` | 否 | `CURRENT`、`ARCHIVED` |
| `specialist_user_agent_id` | `uuid` | 是 | 专业会话固定绑定，普通会话为空 |
| `archived_at` | `timestamptz` | 是 | 产品会话归档时间 |
| `archive_reason` | `varchar(32)` | 是 | `TOPIC_SWITCH`、`SPECIALIST_SWITCH`、`HISTORY_RESUME`、`WORKFLOW_EXIT` |
| `deleted_at` | `timestamptz` | 是 | 用户删除历史会话或系统丢弃空会话的软删除时间 |
| `active_segment_id` | `uuid` | 是 | 当前 ACTIVE 分段，循环外键后添加 |
| `latest_handoff_summary_id` | `uuid` | 是 | 最新累计交接摘要，循环外键后添加 |
| `context_version` | `integer` | 否 | 默认 1，每次线程轮换递增 |
| `last_message_at` | `timestamptz` | 是 | 主聊天最近消息时间 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(user_id, client_conversation_id)`；部分唯一索引在 `status = 'CURRENT' AND deleted_at IS NULL` 范围内保证每个用户最多一个可见当前会话，时间线索引仅覆盖 `deleted_at IS NULL` 的可见会话。
- 产品会话归档不删除消息、摘要或 checkpoint，也不改变内部 segment 状态。
- 用户删除仅允许 `ARCHIVED` 会话；系统切换会话时可软删除不存在 `USER` 消息的空 `CURRENT` 会话。普通列表、详情、消息与发送查询必须排除 `deleted_at IS NOT NULL`，历史列表还必须排除没有 `USER` 消息的异常 `ARCHIVED` 数据。
- 业务层从认证用户解析归属；客户端只能指定公开 `specialist_code`，服务端解析并固定 `specialist_user_agent_id`。`client_conversation_id` 由服务端自动场景创建时生成，客户端不再调用手动创建 API。

### 5.2 `conversation_segments`

主聊天的内部上下文分段，前端不可见。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `conversation_id` | `uuid` | 否 | 外键 → `conversations.id`，删除级联 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `sequence` | `integer` | 否 | 主聊天内从 1 连续递增 |
| `thread_id` | `varchar(128)` | 否 | 本分段不可变的 LangGraph thread ID |
| `status` | `varchar(16)` | 否 | `ACTIVE`、`ARCHIVING`、`ARCHIVED` |
| `start_message_id` | `uuid` | 是 | 本段首条消息，循环外键后添加 |
| `end_message_id` | `uuid` | 是 | 本段末条消息，循环外键后添加 |
| `estimated_context_tokens` | `integer` | 否 | 默认 0，未做新压缩时的预计输入 Token |
| `final_summary_id` | `uuid` | 是 | 最终分段摘要，循环外键后添加 |
| `archive_reason` | `varchar(32)` | 是 | 验证版为 `TOKEN_HARD_LIMIT` 或运维修复原因 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `archived_at` | `timestamptz` | 是 | 完成归档时间 |

约束与索引：

- `thread_id` 全局唯一；唯一约束 `(conversation_id, sequence)`。
- 部分唯一索引：`conversation_id`，仅 `status = 'ACTIVE'` 时生效。
- `sequence > 0`、`estimated_context_tokens >= 0`；`ARCHIVED` 要求 `end_message_id`、`final_summary_id`、`archived_at` 非空。
- 有非终态 run 的 segment 不能进入 `ARCHIVING/ARCHIVED`。
- 添加 `conversations.active_segment_id` 外键；它必须指向同一 conversation 的 ACTIVE segment。

### 5.3 `conversation_summaries`

不可变、可重建的派生上下文，不是业务事实。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `conversation_id` | `uuid` | 否 | 外键 → `conversations.id`，删除级联 |
| `segment_id` | `uuid` | 是 | `SEGMENT_FINAL` 时指向来源分段 |
| `summary_type` | `varchar(24)` | 否 | `INCREMENTAL`、`SEGMENT_FINAL`、`CUMULATIVE_HANDOFF` |
| `version` | `integer` | 否 | 主聊天和类型内递增 |
| `summary_data` | `jsonb` | 否 | `ConversationSummaryV1`，含来源消息/分段和 `memory_refs` |
| `source_from_message_id` | `uuid` | 是 | 覆盖起点 |
| `source_through_message_id` | `uuid` | 是 | 覆盖终点 |
| `source_hash` | `char(64)` | 否 | 规范化来源及上一摘要的 SHA-256 |
| `prompt_version` | `varchar(32)` | 否 | 摘要 Prompt 版本 |
| `token_count` | `integer` | 否 | 摘要 Token 数 |
| `status` | `varchar(16)` | 否 | `GENERATING`、`PUBLISHED`、`FAILED`、`SUPERSEDED` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

- 唯一约束：`(conversation_id, summary_type, version)`、`source_hash`。
- `version > 0`、`token_count >= 0`；仅 `PUBLISHED` 摘要可进入模型上下文。
- `memory_refs` 只保存稳定记忆键，不复制记忆值；来源消息必须属于同一 conversation。
- 添加 `conversation_segments.final_summary_id` 和 `conversations.latest_handoff_summary_id` 外键并校验归属。

### 5.4 `messages`

只保存用户可见消息，不保存隐藏思维链。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `conversation_id` | `uuid` | 否 | 外键 → `conversations.id`，删除级联 |
| `segment_id` | `uuid` | 否 | 外键 → `conversation_segments.id`，删除限制 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联，便于隔离查询 |
| `role` | `varchar(16)` | 否 | `USER`、`ASSISTANT`、`SYSTEM_EVENT` |
| `status` | `varchar(16)` | 否 | `PENDING`、`STREAMING`、`COMPLETED`、`FAILED`、`CANCELLED` |
| `client_message_id` | `varchar(64)` | 是 | 用户消息客户端幂等 ID |
| `client_request_hash` | `char(64)` | 是 | 规范化 content 和有序附件 ID 的 SHA-256 |
| `agent_run_id` | `uuid` | 是 | 处理该消息的 run；同一 run 可关联多轮消息，循环外键后添加 |
| `content` | `text` | 否 | API 层限制长度 |
| `structured_content` | `jsonb` | 否 | 默认 `{}`，卡片、引用等展示数据 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()`，流式完成或失败时更新 |

约束与索引：

- `USER` 消息要求 `client_message_id`、`client_request_hash` 非空且 `status = 'COMPLETED'`，其他角色的两个客户端字段必须为空。
- `ASSISTANT` 消息初始以空 `content` 和 `PENDING` 创建，可迁移为 `STREAMING`，最终必须进入 `COMPLETED`、`FAILED` 或 `CANCELLED`。
- 只有显式重试 `FAILED_RETRYABLE` run 时，当前 `FAILED` Assistant 消息可以回到 `PENDING`；必须增加 attempt，并在已有部分输出时写 `message.reset`。
- `SYSTEM_EVENT` 必须为 `COMPLETED`。
- 部分唯一索引：`(user_id, client_message_id)`，仅非空时生效，确保自动路由重试不会重复创建会话或消息。
- 业务层验证 segment 属于同一 conversation；归档不改变消息的 segment 或展示顺序。
- 索引：`(conversation_id, created_at, id)`、`(segment_id, created_at, id)`、`(user_id, created_at DESC)`、`agent_run_id`。

### 5.5 `message_attachments`

消息与私有文件的稳定关联，附件 ID 不只保存在 `structured_content` 中。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `message_id` | `uuid` | 否 | 外键 → `messages.id`，删除级联 |
| `stored_file_id` | `uuid` | 否 | 外键 → `stored_files.id`，在文件表创建后添加，删除限制 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `position` | `smallint` | 否 | 从 0 开始的显示顺序 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(message_id, stored_file_id)`、`(message_id, position)`。
- `position >= 0`。
- 业务事务必须验证 message、file 的 `user_id` 相同，且文件为 `purpose=CHAT_ATTACHMENT`、`upload_status=VERIFIED`、`scan_status=CLEAN`。
- 验证阶段只允许 `USER` 消息附带文件；Assistant 引用文件时在结构化内容中引用已有、已授权的 file ID，不复制关联。
- 索引：`(user_id, created_at DESC)`、`stored_file_id`。

### 5.6 `agent_runs`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键，也是 API `run_id` |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `conversation_id` | `uuid` | 否 | 外键 → `conversations.id`，删除级联 |
| `segment_id` | `uuid` | 否 | 外键 → `conversation_segments.id`，删除限制；决定本 run 的 thread_id |
| `selected_user_agent_id` | `uuid` | 是 | 本次运行实际选择并固定的专业 Agent |
| `trigger_message_id` | `uuid` | 否 | 外键 → `messages.id`，删除级联 |
| `status` | `varchar(24)` | 否 | `QUEUED`、`RUNNING`、`AWAITING_INPUT`、`AWAITING_APPROVAL`、`SUCCEEDED`、`FAILED_RETRYABLE`、`FAILED_FINAL`、`CANCEL_REQUESTED`、`CANCELLED` |
| `pending_action` | `varchar(24)` | 否 | `NONE`、`START`、`INPUT_RESUME`、`APPROVAL_RESUME`、`RETRY`；默认 `START` |
| `pending_action_key` | `varchar(160)` | 是 | 当前启动/恢复命令的稳定幂等键 |
| `last_applied_action_key` | `varchar(160)` | 是 | 已进入 LangGraph checkpoint 的最近命令键 |
| `pending_message_id` | `uuid` | 是 | 本次启动/恢复使用的 User 消息，循环外键后添加 |
| `pending_response_message_id` | `uuid` | 是 | 本次输出对应的 Assistant 占位消息，循环外键后添加 |
| `graph_version` | `varchar(32)` | 否 | 工作流版本 |
| `prompt_bundle_version` | `varchar(32)` | 否 | Prompt 组合版本 |
| `capability_registry_version` | `varchar(32)` | 否 | 创建 run 时固定的内部能力注册表版本 |
| `capability_registry_fingerprint` | `char(64)` | 否 | 注册表 canonical JSON 的 SHA-256；恢复时必须匹配 |
| `model_provider` | `varchar(64)` | 是 | 模型供应商 |
| `model_name` | `varchar(128)` | 是 | 模型名称 |
| `last_node` | `varchar(64)` | 是 | 最近节点 |
| `attempt_count` | `smallint` | 否 | 默认 0；首次执行变为 1，技术重试时递增 |
| `input_summary` | `text` | 是 | 脱敏摘要，不默认保存完整输入 |
| `output_data` | `jsonb` | 是 | 已验证的最终结构化输出 |
| `warning_data` | `jsonb` | 否 | 默认 `[]` |
| `error_code` | `varchar(64)` | 是 | 稳定错误码 |
| `error_detail` | `jsonb` | 是 | 脱敏错误信息 |
| `input_tokens` | `integer` | 否 | 默认 0 |
| `output_tokens` | `integer` | 否 | 默认 0 |
| `trace_id` | `varchar(128)` | 否 | 创建 run 时生成的全链路 ID |
| `worker_id` | `varchar(128)` | 是 | 当前领取该 run 的 Worker 实例 |
| `lease_expires_at` | `timestamptz` | 是 | Worker 租约到期时间 |
| `heartbeat_at` | `timestamptz` | 是 | Worker 最近续租时间 |
| `last_event_sequence` | `integer` | 否 | 默认 0，run 内事件序列分配器 |
| `cancel_requested_at` | `timestamptz` | 是 | 首次请求取消的时间 |
| `started_at` | `timestamptz` | 是 | 开始时间 |
| `completed_at` | `timestamptz` | 是 | 终态时间 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- `trigger_message_id` 唯一，保证作为初始触发消息的 User 消息最多创建一个 run；后续输入恢复消息关联同一 run，但不写入该字段。
- `trace_id` 唯一并在 run 生命周期内不可变；所有 span 必须复用该值。
- Token、尝试次数和 `last_event_sequence` 不得为负。
- 终态 `SUCCEEDED`、`FAILED_FINAL`、`CANCELLED` 要求 `completed_at` 非空。
- `CANCEL_REQUESTED` 要求 `cancel_requested_at` 非空。
- 部分唯一索引：`conversation_id`，仅状态为 `QUEUED`、`RUNNING`、`AWAITING_INPUT`、`AWAITING_APPROVAL`、`FAILED_RETRYABLE`、`CANCEL_REQUESTED` 时生效，保证一个会话最多一个活动 run。
- `user_id` 部分唯一索引只覆盖 `QUEUED`、`RUNNING`、`CANCEL_REQUESTED`，保证用户全局最多一个真正执行中的 run；等待输入、待审批和待重试可跨会话并存。
- Worker 领取索引：`(status, lease_expires_at, created_at)`；查询仍需使用 `FOR UPDATE SKIP LOCKED`。
- 查询索引：`(user_id, created_at DESC)`、`(conversation_id, created_at DESC)`、`(segment_id, created_at DESC)`、`(status, updated_at)`、`trace_id`。
- 添加 `messages.agent_run_id` → `agent_runs.id` 外键，删除时设空。
- 添加 `agent_runs.pending_message_id`、`pending_response_message_id` → `messages.id` 外键，删除时设空。
- `pending_action != 'NONE'` 时 `pending_action_key` 必须非空。示例：`START:{trigger_message_id}`、`INPUT:{message_id}`、`APPROVAL:{approval_id}`、`RETRY:{attempt}`。
- retry API 使用 `expected_attempt` 乐观校验；只有 `FAILED_RETRYABLE` 且值匹配时才能原子递增 `attempt_count` 并写 `RETRY:{new_attempt}`。重复请求命中相同 pending key 时返回原结果。
- Worker 不能在领取任务时立即清空 `pending_action`。只有确认 checkpoint 已包含相同 action key 后，才能把 key 复制到 `last_applied_action_key` 并清空 pending 字段，避免领取后崩溃导致恢复命令丢失。
- 是否已执行业务副作用仍由任务、通知、审批等领域唯一键判断，不能只依赖 action key。
- Worker 启动或恢复图前必须加载同时匹配 `graph_version`、`capability_registry_version` 和 `capability_registry_fingerprint` 的不可变快照；不允许用当前默认 registry 自动升级非终态 run。

### 5.6.1 `model_invocations`

保存聊天模型与 Embedding 的非敏感调用元数据。字段包括可空 `request_id/run_id`、task、provider、实际 model、Prompt/Schema 版本、attempt、主备角色、状态、输入/缓存/输出 Token、耗时、错误分类和创建时间。表中不保存 Prompt、用户原文、检索或文件正文、工具原始输出、思维链、密钥、单价或估算金额。

按小时聚合视图 `model_invocation_metrics_hourly` 提供节点/供应商/模型维度的调用次数、Token、P50/P95、成功率、Schema 修复率和切换率。`run_id` 删除时设空，审计记录不反向阻止 run 清理。

### 5.7 `agent_run_events`

保存可向聊天客户端续传的展示事件，不保存完整 checkpoint、原始思维链或未脱敏工具参数。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `bigserial` | 否 | 主键 |
| `agent_run_id` | `uuid` | 否 | 外键 → `agent_runs.id`，删除级联 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联，便于隔离和清理 |
| `sequence` | `integer` | 否 | run 内从 1 单调递增 |
| `attempt` | `smallint` | 否 | 默认 1，对应本次重试 attempt |
| `event_type` | `varchar(32)` | 否 | 允许值见下方 |
| `payload` | `jsonb` | 否 | 默认 `{}`，版本化展示数据 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

允许持久化的事件类型：

- `run.accepted`
- `run.status`
- `progress`
- `message.start`
- `message.delta`
- `message.reset`
- `message.completed`
- `interrupt`
- `run.completed`
- `run.cancelled`
- `error`

约束与索引：

- 唯一约束：`(agent_run_id, sequence)`。
- `sequence > 0`、`attempt > 0`。
- 读取索引：`(agent_run_id, sequence)`。
- 清理索引：`created_at`。
- `heartbeat` 不持久化，也不占用 sequence。
- 写事件时原子增加 `agent_runs.last_event_sequence`，以新值插入本表；禁止使用 Worker 进程内计数器。
- `message.delta` 应按约 100 ms、128 字符或输出结束合并，避免逐 token 写库。

### 5.8 `agent_trace_spans`

保存 Agent 控制流的脱敏 span，用于指标、故障定位和生产结构回放；不作为聊天展示事件、业务审计或副作用幂等事实。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `agent_run_id` | `uuid` | 否 | 外键 → `agent_runs.id`，删除级联 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联，只用于隔离和清理 |
| `trace_id` | `varchar(128)` | 否 | 与 `agent_runs.trace_id` 一致 |
| `span_id` | `varchar(32)` | 否 | trace 内唯一 span 标识 |
| `parent_span_id` | `varchar(32)` | 是 | 父 span；根 `agent.run` 为空 |
| `attempt` | `smallint` | 否 | 当前 run attempt，必须大于 0 |
| `span_kind` | `varchar(32)` | 否 | `RUN`、`NODE`、`MODEL`、`TOOL_LOOP`、`PERMISSION`、`CAPABILITY`、`DOMAIN_TX` |
| `node_name` | `varchar(64)` | 是 | LangGraph 节点名 |
| `work_item_id` | `varchar(128)` | 是 | 组合计划中的稳定工作项标识 |
| `capability_name` | `varchar(128)` | 是 | 仅 Permission/Capability span 使用 |
| `capability_version` | `varchar(32)` | 是 | 能力版本 |
| `registry_fingerprint` | `char(64)` | 否 | 本 span 执行时固定的 registry fingerprint |
| `risk_level` | `varchar(16)` | 是 | `LOW`、`MEDIUM`、`HIGH` |
| `gate_decision` | `varchar(16)` | 是 | `ALLOW`、`DENY`；仅 Permission span 使用 |
| `status` | `varchar(24)` | 否 | `RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED` |
| `error_code` | `varchar(64)` | 是 | 稳定、脱敏错误码 |
| `retry_count` | `smallint` | 否 | 默认 0，不得为负 |
| `input_hash` | `char(64)` | 是 | 规范化且脱敏输入的 SHA-256，不保存输入值 |
| `output_hash` | `char(64)` | 是 | 已验证结构化结果的 SHA-256，不保存结果正文 |
| `trust_level` | `varchar(32)` | 是 | `SYSTEM_FACT`、`USER_CONTENT`、`EXTERNAL_UNTRUSTED` |
| `result_items` | `integer` | 是 | 返回条目数，不得为负 |
| `truncated` | `boolean` | 否 | 默认 `false` |
| `input_tokens` | `integer` | 否 | 默认 0，不得为负 |
| `output_tokens` | `integer` | 否 | 默认 0，不得为负 |
| `started_at` | `timestamptz` | 否 | 开始时间 |
| `ended_at` | `timestamptz` | 是 | 终态时间 |
| `duration_ms` | `integer` | 是 | 终态耗时，不得为负 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(trace_id, span_id)`。
- `trace_id` 必须与所属 run 一致；`registry_fingerprint` 必须与所属 run 固定值一致。
- `SUCCEEDED`、`FAILED`、`CANCELLED` 要求 `ended_at` 和 `duration_ms` 非空。
- 父子关系由同一 `trace_id` 下的 `parent_span_id` 校验；异步写入允许先写子 span，因此不建立自引用外键。
- 查询索引：`(agent_run_id, started_at, span_id)`、`(trace_id, started_at)`。
- 清理索引：`created_at`。
- 禁止增加通用 `attributes` 或 raw payload 字段，避免绕过敏感数据约束。
- 审批、计划发布、任务和通知审计继续由领域表承担；删除 trace 不影响业务恢复或审计。

### 5.9 `approval_decisions`

同时表示待审批请求及其最终决策。一个请求可批准一个或多个独立 plan revision。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键、API `approval_id` |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `agent_run_id` | `uuid` | 否 | 外键 → `agent_runs.id`，删除级联 |
| `interrupt_key` | `varchar(128)` | 否 | LangGraph 中断稳定键 |
| `status` | `varchar(16)` | 否 | `PENDING`、`APPROVED`、`EDITED`、`REJECTED`、`EXPIRED` |
| `approval_version` | `integer` | 否 | 审批自身乐观锁版本，默认 1，必须大于 0 |
| `request_payload` | `jsonb` | 否 | 展示给用户的结构化内容 |
| `decision_payload` | `jsonb` | 是 | 编辑字段、反馈等 |
| `decided_at` | `timestamptz` | 是 | 最终决策时间 |
| `expires_at` | `timestamptz` | 是 | 可选过期时间 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(agent_run_id, interrupt_key)`。
- 非 `PENDING` 状态要求 `decided_at` 非空。
- `expires_at` 必须晚于 `created_at`。
- `approval_version > 0`。
- 索引：`(user_id, status, created_at DESC)`。

### 5.10 `approval_decision_items`

一个审批请求与一个待发布 revision 的关系。组合计划通过多行表达；不在 `request_payload` 中维护不可校验的 revision ID 数组。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `approval_decision_id` | `uuid` | 否 | 外键 → `approval_decisions.id`，删除级联 |
| `plan_revision_id` | `uuid` | 否 | 外键 → `plan_revisions.id`，删除限制 |
| `expected_current_revision_id` | `uuid` | 是 | 外键 → `plan_revisions.id`，删除限制；调整已有计划时为审批生成时的当前 revision，新计划为空 |
| `position` | `smallint` | 否 | 从 0 开始，用于卡片稳定排序 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(approval_decision_id, plan_revision_id)`、`(approval_decision_id, position)`。
- `position >= 0`；业务层验证 `plan_revision_id` 与 `expected_current_revision_id` 属于同一 plan。
- 单计划调整类型的审批必须且只能有一行；组合新建可以有多行。
- 索引：`plan_revision_id`、`expected_current_revision_id`。

### 5.11 `memory_extraction_jobs`

业务库中的可靠作业/outbox；活跃记忆值保存在 LangGraph Store。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `message_id` | `uuid` | 否 | 来源 USER 消息，删除级联 |
| `job_type` | `varchar(24)` | 否 | `EXTRACT`、`COMMAND`、`PURGE`、`CONSOLIDATE` |
| `status` | `varchar(24)` | 否 | `PREPARED`、`RUNNING`、`STORE_APPLIED`、`COMPLETED`、`RETRYABLE`、`FAILED_FINAL` |
| `prompt_version` | `varchar(32)` | 否 | 提取 Prompt 版本 |
| `policy_version` | `varchar(32)` | 否 | 准入/评分 Policy 版本 |
| `attempt_count` | `smallint` | 否 | 默认 0 |
| `next_attempt_at` | `timestamptz` | 是 | 重试时间 |
| `error_code` | `varchar(64)` | 是 | 脱敏稳定错误码 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

- 唯一约束：`(message_id, job_type, prompt_version, policy_version)`。
- Store 与业务库不做分布式事务；稳定记忆键使 `STORE_APPLIED` 后重试保持幂等。
- 按用户串行领取，旧来源消息不得覆盖较新的 Store 文档版本。

### 5.12 `memory_policy_state`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `user_id` | `uuid` | 否 | 主键、外键 → `users.id`，删除级联 |
| `capture_mode` | `varchar(16)` | 否 | `ACTIVE`、`PAUSED`；默认 `ACTIVE` |
| `forget_all_before` | `timestamptz` | 是 | 此时间及以前的来源不得重新写入 |
| `version` | `integer` | 否 | 乐观锁版本，默认 1 |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

该表只由聊天 Memory Command 修改，不暴露设置 API，也不在 `users` 增加记忆开关。

### 5.13 `memory_tombstones`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `memory_key_hash` | `char(64)` | 否 | 类型和规范化键的不可逆哈希，不保存事实值 |
| `blocked_through_message_id` | `uuid` | 是 | 此消息及以前的候选不能复活记忆 |
| `reason` | `varchar(24)` | 否 | `FORGET_ONE`、`FORGET_ALL`、`PAUSE` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `cleared_at` | `timestamptz` | 是 | 用户明确要求重新记住时设置 |

- 活跃 tombstone 唯一约束：`(user_id, memory_key_hash)`，仅 `cleared_at IS NULL` 时生效。
- 普通重复陈述不能解除；只有明确“重新记住/更新”命令可以解除。

### 5.14 `memory_audit_records`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `bigserial` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `job_id` | `uuid` | 是 | 外键 → `memory_extraction_jobs.id`，删除设空 |
| `source_message_id` | `uuid` | 是 | 来源消息，删除设空 |
| `operation` | `varchar(24)` | 否 | `CREATE`、`REINFORCE`、`SUPERSEDE`、`FORGET`、`EXPIRE`、`EVICT`、`REJECT` |
| `memory_key_hash` | `char(64)` | 是 | 不保存事实明文 |
| `reason_code` | `varchar(64)` | 是 | 准入、拒绝或清理原因 |
| `score_data` | `jsonb` | 否 | 默认 `{}`，仅保存分项分数和版本 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

索引：`(user_id, created_at DESC)`、`job_id`、`memory_key_hash`。普通日志不得复制候选值、证据原文或 Store 文档内容。

## 6. 目标、计划与任务

### 6.1 `goals`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `user_agent_id` | `uuid` | 否 | 外键 → `user_agents.id`，删除限制 |
| `title` | `varchar(200)` | 否 | 目标名称 |
| `goal_type` | `varchar(64)` | 否 | 验证版为 `CIVIL_SERVICE_EXAM` |
| `target_date` | `date` | 是 | 截止或考试日期 |
| `constraints` | `jsonb` | 否 | 默认 `{}`，时间和资源限制 |
| `status` | `varchar(16)` | 否 | `DRAFT`、`ACTIVE`、`ACHIEVED`、`ABANDONED` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |
| `completed_at` | `timestamptz` | 是 | 达成或放弃时间 |

索引：`(user_id, status, updated_at DESC)`、`user_agent_id`。

### 6.2 `plans`

计划逻辑实体，不保存具体阶段和任务内容。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `goal_id` | `uuid` | 否 | 外键 → `goals.id`，删除级联 |
| `title` | `varchar(200)` | 否 | 计划名称 |
| `current_revision_id` | `uuid` | 是 | 当前批准版本，循环外键后添加 |
| `status` | `varchar(16)` | 否 | `DRAFT`、`ACTIVE`、`COMPLETED`、`CANCELLED` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 一个目标最多一个未取消计划：对 `goal_id` 建立 `status IN ('DRAFT','ACTIVE')` 的部分唯一索引。
- 索引：`(user_id, status)`。

### 6.3 `plan_revisions`

每个版本发布后不可原地修改。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `plan_id` | `uuid` | 否 | 外键 → `plans.id`，删除级联 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `revision` | `integer` | 否 | 从 1 递增 |
| `status` | `varchar(24)` | 否 | `DRAFT`、`IN_REVIEW`、`PENDING_APPROVAL`、`APPROVED`、`REJECTED`、`SUPERSEDED` |
| `objective_summary` | `text` | 否 | 目标与策略摘要 |
| `start_date` | `date` | 否 | 开始日期 |
| `end_date` | `date` | 否 | 不早于开始日期 |
| `weekly_minutes` | `integer` | 否 | 大于 0 |
| `assumptions` | `jsonb` | 否 | 默认 `[]` |
| `generated_by_run_id` | `uuid` | 是 | 外键 → `agent_runs.id`，删除设空 |
| `change_reason` | `text` | 是 | 相比上一版本的变化原因 |
| `approved_at` | `timestamptz` | 是 | 批准时间 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(plan_id, revision)`。
- `end_date >= start_date`，`weekly_minutes > 0`。
- `APPROVED` 状态要求 `approved_at` 非空。
- 一个 `plan_id` 最多一个 `APPROVED` revision，旧版本批准新版本时必须先改为 `SUPERSEDED`。
- 添加 `plans.current_revision_id` → `plan_revisions.id` 外键，删除限制。
- 业务事务验证 `current_revision_id` 属于当前 `plan_id`。
- `approval_decision_items.plan_revision_id` 关联待审批版本，删除限制。

### 6.4 `plan_stages`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `plan_revision_id` | `uuid` | 否 | 外键 → `plan_revisions.id`，删除级联 |
| `name` | `varchar(128)` | 否 | 阶段名称 |
| `objective` | `text` | 否 | 可验收阶段目标 |
| `sequence` | `smallint` | 否 | 从 1 开始 |
| `start_date` | `date` | 否 | 开始日期 |
| `end_date` | `date` | 否 | 不早于开始日期 |
| `allocated_minutes` | `integer` | 否 | 大于 0 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(plan_revision_id, sequence)`。
- `end_date >= start_date`，`allocated_minutes > 0`。
- 索引：`(plan_revision_id, start_date)`。

在正式任务之前，计划 revision 先持久化经过审核的任务模板。

### 6.5 `plan_task_templates`

任务模板属于不可变 plan revision，供审批后的 Executor 展开未来 7 天任务。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `plan_revision_id` | `uuid` | 否 | 外键 → `plan_revisions.id`，删除级联 |
| `plan_stage_id` | `uuid` | 否 | 外键 → `plan_stages.id`，删除级联 |
| `template_key` | `varchar(128)` | 否 | revision 内稳定键 |
| `title` | `varchar(200)` | 否 | 任务标题模板 |
| `description` | `text` | 是 | 动作和完成标准模板 |
| `frequency` | `jsonb` | 否 | 通过版本化 recurrence Schema 校验 |
| `expected_minutes` | `smallint` | 否 | 1–1,440 |
| `priority` | `smallint` | 否 | 1–5 |
| `source_claim_keys` | `jsonb` | 否 | 默认 `[]`，引用本 revision 的 Claim key |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(plan_revision_id, template_key)`。
- `expected_minutes BETWEEN 1 AND 1440`，`priority BETWEEN 1 AND 5`。
- 索引：`plan_stage_id`、`plan_revision_id`。
- 业务层验证 stage 与 template 属于同一 revision，`source_claim_keys` 均存在。

### 6.6 `tasks`

正式任务只允许从批准的 plan revision 物化。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `plan_revision_id` | `uuid` | 否 | 外键 → `plan_revisions.id`，删除限制 |
| `plan_stage_id` | `uuid` | 否 | 外键 → `plan_stages.id`，删除限制 |
| `plan_task_template_id` | `uuid` | 否 | 外键 → `plan_task_templates.id`，删除限制 |
| `task_key` | `varchar(128)` | 否 | revision 内稳定幂等键 |
| `title` | `varchar(200)` | 否 | 具体动作标题 |
| `description` | `text` | 是 | 完成标准和资料 |
| `scheduled_date` | `date` | 否 | 用户时区下的日期 |
| `due_at` | `timestamptz` | 是 | 可选截止时间 |
| `expected_minutes` | `smallint` | 否 | 1–1,440 |
| `priority` | `smallint` | 否 | 1–5 |
| `status` | `varchar(16)` | 否 | `TODO`、`DOING`、`DONE`、`SKIPPED`、`CANCELLED` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |
| `completed_at` | `timestamptz` | 是 | 完成时间 |

约束与索引：

- 唯一约束：`(plan_revision_id, task_key)`，用于恢复时防重复创建。
- `expected_minutes BETWEEN 1 AND 1440`，`priority BETWEEN 1 AND 5`。
- `DONE` 状态要求 `completed_at` 非空。
- 索引：`(user_id, scheduled_date, status)`、`plan_stage_id`、`due_at`。
- 业务层验证 stage、template 和 task 属于同一 revision，且 revision 已批准。

### 6.7 `task_executions`

统一替代 V1.0 的 `task_record` 和任务型 `learning_record`。

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `task_id` | `uuid` | 否 | 外键 → `tasks.id`，删除级联 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `client_execution_id` | `varchar(64)` | 否 | 客户端幂等 ID |
| `result` | `varchar(16)` | 否 | `COMPLETED`、`PARTIAL`、`SKIPPED` |
| `duration_minutes` | `smallint` | 否 | 0–1,440 |
| `feedback` | `text` | 是 | 用户反馈 |
| `outcome_data` | `jsonb` | 否 | 默认 `{}`，正确率等可选指标 |
| `occurred_at` | `timestamptz` | 否 | 实际执行时间 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(task_id, client_execution_id)`。
- `duration_minutes BETWEEN 0 AND 1440`。
- 索引：`(user_id, occurred_at DESC)`、`task_id`。

## 7. 知识、Claim 与引用

### 7.1 `knowledge_documents`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `owner_user_id` | `uuid` | 是 | 私有文档外键 → `users.id`，公共文档为空 |
| `stored_file_id` | `uuid` | 是 | 私有资料外键 → `stored_files.id`，删除级联且非空时唯一 |
| `visibility` | `varchar(16)` | 否 | `PUBLIC`、`PRIVATE` |
| `domain` | `varchar(64)` | 否 | 例如 `CIVIL_SERVICE_EXAM` |
| `title` | `varchar(300)` | 否 | 文档标题 |
| `source_url` | `text` | 是 | 原始来源，仅允许 HTTP(S) |
| `source_organization` | `varchar(200)` | 是 | 发布机构 |
| `source_level` | `varchar(16)` | 否 | `OFFICIAL`、`AUTHORITATIVE`、`GENERAL`、`USER` |
| `object_key` | `varchar(512)` | 是 | 原文件私有对象 key |
| `mime_type` | `varchar(128)` | 是 | MIME 类型 |
| `sha256` | `char(64)` | 否 | 文件或规范化正文哈希 |
| `document_version` | `integer` | 否 | 从 1 开始 |
| `published_at` | `timestamptz` | 是 | 来源发布时间 |
| `retrieved_at` | `timestamptz` | 否 | 抓取或上传时间 |
| `valid_from` | `timestamptz` | 是 | 生效时间 |
| `valid_to` | `timestamptz` | 是 | 失效时间 |
| `ingestion_status` | `varchar(24)` | 否 | `PENDING`、`PROCESSING`、`READY`、`FAILED`、`DELETING`、`DELETED` |
| `error_code` | `varchar(64)` | 是 | 入库失败原因 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- `PUBLIC` 要求 `owner_user_id` 为空；`PRIVATE` 要求非空。
- `valid_to` 不得早于 `valid_from`。
- 唯一约束：`(owner_user_id, sha256, document_version)`，公共空值场景另建 `sha256 + document_version` 部分唯一索引。
- 索引：`(domain, visibility, source_level, published_at DESC)`、`owner_user_id`、`ingestion_status`。

Qdrant `tenant_id` 映射：公共文档写入字符串 `public`；私有文档写入 `owner_user_id` 的 UUID 字符串。

### 7.2 `knowledge_chunks`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键，同时作为 Qdrant payload `chunk_id` |
| `document_id` | `uuid` | 否 | 外键 → `knowledge_documents.id`，删除级联 |
| `chunk_index` | `integer` | 否 | 从 0 开始 |
| `heading_path` | `text` | 是 | 章节路径 |
| `content` | `text` | 否 | 规范化分块文本 |
| `token_count` | `integer` | 否 | 大于 0 |
| `content_hash` | `char(64)` | 否 | 分块哈希 |
| `embedding_model` | `varchar(128)` | 否 | 模型名称和版本 |
| `qdrant_collection` | `varchar(128)` | 否 | collection 名称 |
| `qdrant_point_id` | `uuid` | 否 | Qdrant point ID |
| `vector_status` | `varchar(16)` | 否 | `PENDING`、`READY`、`FAILED`、`DELETED` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(document_id, chunk_index)`、`(qdrant_collection, qdrant_point_id)`。
- `chunk_index >= 0`，`token_count > 0`。
- 索引：`document_id`、`vector_status`。

### 7.3 `claims`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `agent_run_id` | `uuid` | 否 | 外键 → `agent_runs.id`，删除级联 |
| `plan_revision_id` | `uuid` | 是 | 外键 → `plan_revisions.id`，删除级联 |
| `claim_key` | `varchar(128)` | 否 | run 内稳定键 |
| `claim_text` | `text` | 否 | 最小可验证陈述 |
| `claim_type` | `varchar(24)` | 否 | `FACT`、`ADVICE`、`USER_PROVIDED`、`ASSUMPTION` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- 唯一约束：`(agent_run_id, claim_key)`。
- 索引：`plan_revision_id`、`agent_run_id`。

### 7.4 `citations`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `claim_id` | `uuid` | 否 | 外键 → `claims.id`，删除级联 |
| `knowledge_chunk_id` | `uuid` | 是 | 外键 → `knowledge_chunks.id`，删除时置空；历史引用依赖快照继续存在 |
| `source_url_snapshot` | `text` | 是 | 外部来源快照 URL |
| `evidence_excerpt` | `varchar(1000)` | 是 | 支撑 Claim 的短证据片段 |
| `relation` | `varchar(16)` | 否 | `SUPPORTS`、`CONTRADICTS`、`CONTEXT` |
| `relevance_score` | `numeric(5,4)` | 是 | 0–1，只用于排序，不代表事实正确性 |
| `source_type` | `varchar(24)` | 否 | `WEB`、`PRIVATE_FILE`、`KNOWLEDGE` |
| `source_title_snapshot` | `varchar(300)` | 否 | 展示标题快照 |
| `source_organization_snapshot` | `varchar(200)` | 是 | 发布机构快照 |
| `source_domain_snapshot` | `varchar(255)` | 是 | 域名或私有来源标签快照 |
| `published_at_snapshot` | `timestamptz` | 是 | 发布时间快照 |
| `retrieved_at_snapshot` | `timestamptz` | 否 | 检索时间快照 |
| `source_rank` | `smallint` | 否 | 服务端稳定引用编号，从 1 开始 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- `WEB` 必须有 `source_url_snapshot`；私有资料删除后允许只保留不可变来源快照。
- `relevance_score` 在 0–1 之间。
- 索引：`(claim_id, source_rank, id)`、`knowledge_chunk_id`。

## 8. 文件与通知

### 8.1 `stored_files`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `purpose` | `varchar(32)` | 否 | `AVATAR`、`STUDY_MATERIAL`、`CHAT_ATTACHMENT` |
| `original_filename` | `varchar(255)` | 否 | 展示名，不能直接作为对象 key |
| `object_key` | `varchar(512)` | 否 | 私有且不可猜测的对象 key |
| `mime_type` | `varchar(128)` | 否 | 服务端检测结果 |
| `size_bytes` | `bigint` | 否 | 大于 0，受用途限制 |
| `sha256` | `char(64)` | 否 | 内容哈希 |
| `upload_status` | `varchar(16)` | 否 | `PENDING`、`UPLOADED`、`VERIFIED`、`FAILED`、`DELETED` |
| `scan_status` | `varchar(16)` | 否 | `PENDING`、`CLEAN`、`INFECTED`、`FAILED` |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |
| `deleted_at` | `timestamptz` | 是 | 删除时间 |

约束与索引：

- `object_key` 全局唯一，`size_bytes > 0`。
- 只有 `VERIFIED + CLEAN` 文件可供 Agent 使用。
- 索引：`(user_id, purpose, created_at DESC)`、`(user_id, sha256)`。
- 在该表创建后添加 `users.avatar_file_id` 外键，删除设空。

### 8.2 `notification_jobs`

| 字段 | 类型 | 空值 | 约束/说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | 否 | 主键 |
| `user_id` | `uuid` | 否 | 外键 → `users.id`，删除级联 |
| `task_id` | `uuid` | 是 | 外键 → `tasks.id`，删除级联 |
| `event_type` | `varchar(32)` | 否 | `TASK_REMINDER`、`PLAN_UPDATE` 等 |
| `channel` | `varchar(16)` | 否 | `IN_APP`、`WECHAT`、`SMS`、`EMAIL` |
| `scheduled_at` | `timestamptz` | 否 | 计划发送时间 |
| `payload` | `jsonb` | 否 | 模板参数，不保存密钥 |
| `status` | `varchar(16)` | 否 | `PENDING`、`PROCESSING`、`SENT`、`RETRY`、`CANCELLED`、`DEAD` |
| `attempt_count` | `smallint` | 否 | 默认 0 |
| `max_attempts` | `smallint` | 否 | 默认 3，1–10 |
| `next_attempt_at` | `timestamptz` | 是 | 重试时间 |
| `provider_message_id` | `varchar(255)` | 是 | 渠道响应 ID |
| `idempotency_key` | `varchar(255)` | 否 | 业务幂等键 |
| `last_error_code` | `varchar(64)` | 是 | 脱敏错误码 |
| `sent_at` | `timestamptz` | 是 | 成功发送时间 |
| `created_at` | `timestamptz` | 否 | 默认 `now()` |
| `updated_at` | `timestamptz` | 否 | 默认 `now()` |

约束与索引：

- `idempotency_key` 全局唯一。
- `attempt_count >= 0`，`max_attempts BETWEEN 1 AND 10`。
- `SENT` 要求 `sent_at` 非空。
- Worker 领取索引：`(status, COALESCE(next_attempt_at, scheduled_at))`，实现时使用适合查询的表达式或拆分索引。
- 查询索引：`(user_id, created_at DESC)`、`task_id`。

## 9. 关系总览

```text
users
 ├─ user_identities
 ├─ auth_sessions
 ├─ user_profiles
 ├─ study_availability
 ├─ user_agents ── agent_definitions
 │   ├─ conversations
 │   │   ├─ conversation_segments
 │   │   │   └─ messages
 │   │   │       └─ message_attachments ── stored_files
 │   │   ├─ conversation_summaries
 │   │   └─ agent_runs
 │   │       ├─ agent_run_events
 │   │       ├─ agent_trace_spans
 │   │       ├─ approval_decisions ── approval_decision_items ── plan_revisions
 │   │       └─ claims ── citations ── knowledge_chunks
 │   └─ goals
 │       └─ plans
 │           └─ plan_revisions
 │               ├─ plan_stages
 │               │   └─ plan_task_templates
 │               │       └─ tasks ── task_executions
 │               ├─ approval_decision_items
 │               └─ claims
 ├─ stored_files
 ├─ memory_extraction_jobs
 ├─ memory_policy_state
 ├─ memory_tombstones
 ├─ memory_audit_records
 └─ notification_jobs

knowledge_documents
 └─ knowledge_chunks
     └─ citations
```

## 10. LangGraph Checkpointer、Store 与 Qdrant 映射

### 10.1 Checkpointer

- `agent_runs.segment_id → conversation_segments.thread_id` 是调用 LangGraph 时唯一允许使用的 `thread_id`。
- Checkpointer 内部表由官方 PostgreSQL 实现和迁移管理，不复制到业务 DDL。
- `agent_runs` 保存用户可查询的摘要状态和固定 registry；checkpoint 保存恢复所需的细粒度状态，包括代码维护的 `ToolLoopStateV1`。
- `ToolLoopStateV1` 只保存 round、调用数、规范化参数哈希、紧凑 evidence refs、剩余预算和停止原因，不保存原始 Provider 响应。
- segment 归档后 checkpoint 保留 7 天再按 `thread_id` 清理；消息、摘要和业务事实不依赖 checkpoint。

### 10.2 LangGraph Store

- namespace 固定为 `("users", user_id, "long_term_memory", "v1")`；服务端注入 `user_id`，模型不能覆盖。
- key 为 `category + canonical_key` 的稳定 SHA-256；value 使用版本化 `MemoryDocumentV1`，包含类型、值、来源消息、重要性、确认次数、版本和过期时间。
- Store 启用 pgvector，对候选执行语义 top-20；业务代码再应用精确键、TTL、tombstone、时间衰减和重排，最多注入 8 条/800 Token。
- Store 内部表和向量迁移由官方实现管理；`memory_extraction_jobs`、Policy 和屏障是写入/遗忘真相，Store 不自行决定保留内容。
- Qdrant 不保存用户长期记忆。

### 10.3 Qdrant

Qdrant point ID 使用 `knowledge_chunks.qdrant_point_id`，payload 至少包含：

```json
{
  "tenant_id": "public-or-user-uuid",
  "document_id": "uuid",
  "chunk_id": "uuid",
  "domain": "CIVIL_SERVICE_EXAM",
  "source_level": "OFFICIAL",
  "published_at": "2026-01-01T00:00:00Z",
  "valid_to": null,
  "embedding_model": "provider/model-version"
}
```

服务端查询必须添加：

- `tenant_id IN ('public', current_user_id)`
- `domain = requested_domain`
- `valid_to IS NULL OR valid_to > now`
- 与 `knowledge_documents.ingestion_status = 'READY'` 一致的可用集合版本

### 10.4 逻辑 Agent session 映射

系统不创建保存全部上下文的通用 session JSON 表。逻辑 session 按职责映射：

| 状态 | 存储 |
| --- | --- |
| run、pending action、graph/prompt/registry 版本和 trace | `agent_runs` |
| 节点、interrupt、tool loop 和恢复状态 | LangGraph Checkpointer |
| 消息、segment 和摘要 | conversation 相关表 |
| 画像、计划、审批和任务 | 各领域表 |
| 长期记忆 | LangGraph Store |
| 展示事件 | `agent_run_events` |
| 脱敏控制流 | `agent_trace_spans` |

Worker、进程内 facade 和未来 MCP Server 都通过 `run_id` 重新加载需要的投影，不依赖连接或子进程内存。游标和 evidence ref 是有限期定位符，每次使用都重新校验用户、run 和实体归属；业务副作用仍以领域表和唯一键为事实来源。

## 11. 并发与事务规则

### 11.1 聊天提交、领取与完成

发送消息事务按以下顺序执行：

1. 以 `user_id` 锁用户行，再按固定顺序锁定当前/目标会话及目标 ACTIVE segment。
2. 查询 `(user_id, client_message_id)`；已存在则返回原会话、消息、run 和最新 sequence。
3. 查询活动 run。不存在则创建新 run；`AWAITING_INPUT` 则准备恢复同一 run；其他活动状态返回对应 `409`。
4. 插入 User 消息和本轮 Assistant 占位消息，二者绑定当前 segment；同时创建幂等 `memory_extraction_jobs(EXTRACT)`。
5. 创建或更新 run 的 `pending_action`、稳定 `pending_action_key`、`pending_message_id` 和 `pending_response_message_id`。
6. 原子分配 sequence 并插入 `run.accepted` 事件。
7. 更新 `conversations.last_message_at` 后提交；客户端永远不能指定 `conversation_id` 或 `segment_id`。

run 终态后 Context Builder 重新估算未压缩上下文。达到 70% 只创建/刷新增量摘要；达到 85% 且分段无非终态 run 时，在短事务中封存旧段 `end_message_id`、将其置为 `ARCHIVING`，创建新 ACTIVE segment/thread 并原子切换 `conversations.active_segment_id/context_version`。随后高优先级作业发布旧段 `SEGMENT_FINAL` 和新 `CUMULATIVE_HANDOFF`，成功或确定性降级后再将旧段置为 `ARCHIVED`。归档期间到达的新消息绑定新 ACTIVE segment 并排队，Worker 必须等待前序交接完成后才调用模型。

Worker 使用短事务和 `FOR UPDATE SKIP LOCKED` 领取 `QUEUED` run，或接管 lease 已过期的 `RUNNING/CANCEL_REQUESTED` run。领取 `QUEUED` 或接管 `RUNNING` 时设置 `RUNNING`、`worker_id`、`lease_expires_at` 和 `heartbeat_at`；接管 `CANCEL_REQUESTED` 时保持原状态，只完成取消终态，不再调用模型。模型调用期间不能持有领取事务的行锁。

最终回复完成时，在同一事务内更新 Assistant 消息内容与 `COMPLETED`、run 的 `SUCCEEDED`，并插入 `message.completed`、`run.completed` 事件。发生 interrupt 时完成当前 Assistant 消息、创建必要审批记录，并将 run 置为对应等待态。

Worker 崩溃后，lease 到期的 run 可由其他 Worker 重新领取并从 LangGraph checkpoint 恢复。恢复必须检查当前消息状态、attempt 和业务副作用幂等键。SSE 连接是否存在不参与 run 生命周期判断。

### 11.2 计划批准与任务物化

审批 API 使用 `expected_approval_version` 乐观校验，并以一个事务将 `approval_decisions` 从 `PENDING` 改为用户选择的终态，创建展示决定的 `SYSTEM_EVENT` 和本轮 Assistant 占位消息，并将同一 run 更新为 `QUEUED`、`pending_action=APPROVAL_RESUME`、`pending_action_key=APPROVAL:{approval_id}`。只有结构化审批接口可以完成批准、编辑或拒绝；普通聊天消息不能改变审批终态。Worker 再恢复 LangGraph。`APPROVED` 分支中的发布事务按以下顺序执行：

1. 读取全部 `approval_decision_items`，按关联的 `plan_id` 排序后 `SELECT plans ... FOR UPDATE` 锁定。
2. 校验审批已为 `APPROVED`、各 revision 尚未发布、用户归属正确，且 `expected_current_revision_id` 与当前版本逐项匹配。
3. 对每个已有计划将旧 `APPROVED` revision 改为 `SUPERSEDED`；新计划无此步骤。
4. 将全部新 revision 改为 `APPROVED` 并设置 `approved_at`。
5. 更新全部 `plans.current_revision_id` 和 `status = 'ACTIVE'`。
6. 一次提交发布事务；任一项校验或写入失败则整体回滚。

随后 Executor 读取已批准 revision 和 `plan_task_templates`，生成未来 7 天任务草稿。任务服务在另一个事务中按 `(plan_revision_id, task_key)` 幂等创建任务和通知作业。任一事务失败都保持 run 为可恢复状态；已完成步骤通过状态和唯一键跳过。

### 11.3 任务执行

- 根据 `(task_id, client_execution_id)` 插入；冲突时返回原记录。
- 同一任务可有多次 `PARTIAL`，首次 `COMPLETED` 原子更新任务为 `DONE`。
- 已完成任务的重复完成请求不增加统计时长。

### 11.4 知识入库

- 先写 `knowledge_documents/knowledge_chunks` 为 `PENDING`。
- 向 Qdrant upsert 成功后将 chunk 改为 `READY`。
- 所有 chunk 就绪后才将 document 改为 `READY`。
- 失败记录 `FAILED`，重试使用同一 point ID，不产生重复向量。

## 12. 数据隔离、安全与保留

### 12.1 用户隔离

- 仓储方法必须显式接收认证上下文中的 `user_id`。
- 读取实体使用 `WHERE id = :id AND user_id = :current_user_id`，或通过带用户条件的父表关联。
- 自动化测试必须为每个用户表执行跨用户读写拒绝用例。
- 进入公开测试前，为高风险用户表增加 PostgreSQL RLS 作为纵深防御；备份和迁移使用独立 `BYPASSRLS` 角色。

### 12.2 日志与敏感数据

- 原始 Prompt、用户消息和文件内容不写普通应用日志。
- `message.delta` 事件含用户可见回复内容，按消息同等级访问控制和备份加密，且只能保留到事件清理期限。
- `agent_runs.input_summary` 和错误详情必须在写库前脱敏。
- `agent_trace_spans` 只保存版本、状态、耗时、计数和脱敏哈希；禁止通用 attributes、Prompt、用户原文、工具正文、网页正文、附件正文、完整 checkpoint 和模型思维链。
- 手机号和邮箱使用应用层信封加密；唯一查询使用带服务端密钥的 HMAC，而非无盐哈希。
- 对象存储只保存私有 key，下载 URL 由后端短期签发。

### 12.3 默认保留期

| 数据 | 默认保留 |
| --- | --- |
| 普通 Agent 运行元数据 | 30 天后聚合或删除 |
| Agent 脱敏 trace spans | 30 天；删除不影响业务恢复、审批或审计 |
| 聊天增量事件 | run 终态后默认 7 天；非终态事件不清理，终态消息和 run 摘要不依赖事件恢复 |
| 临时开启的原始调试内容 | 最长 7 天 |
| 审批、计划、任务和引用 | 随业务实体保留 |
| 归档聊天分段、摘要和原消息 | 用户删除账号前保留；checkpoint 在分段归档 7 天后清理 |
| 活跃长期记忆 | 类型 TTL（偏好/习惯 180 天，约束/背景 365 天）或用户明确遗忘 |
| 无明文记忆审计 | 默认 90 天；账号删除时清理 |
| 已删除文件 | 对象存储回收策略完成后清理元数据 |

## 13. 账号和文件删除流程

### 13.1 账号删除

1. 将 `users.status` 改为 `DELETING`，撤销访问令牌。
2. 停止并取消该用户的 run 和通知作业。
3. 删除全部 LangGraph segment checkpoint 和用户 Store namespace；`agent_trace_spans` 随 run/user 外键级联清理。
4. 删除 Qdrant 中 `tenant_id = user_id` 的 points。
5. 删除对象存储中用户文件。
6. 在事务中删除或匿名化 PostgreSQL 用户数据。
7. 成功后保留最小不可逆删除凭证或将用户标记为 `DELETED`；不得保留可识别内容。

任一步失败均重试，用户继续保持不可登录状态。

### 13.2 文件删除

1. 将 `stored_files.upload_status` 改为 `DELETED`，立即禁止下载。
2. 将关联知识文档标记为 `DELETING`。
3. 删除 Qdrant points 和对象存储 object。
4. 完成后保留最小元数据或按用户要求彻底删除记录。

## 14. MVP 必需与后续表

### 14.1 MVP 必需

- `users`、`user_identities`、`auth_sessions`、`user_profiles`、`study_availability`
- `agent_definitions`、`user_agents`
- `conversations`、`conversation_segments`、`conversation_summaries`、`messages`、`message_attachments`、`agent_runs`、`agent_run_events`、`agent_trace_spans`、`approval_decisions`、`approval_decision_items`
- `memory_extraction_jobs`、`memory_policy_state`、`memory_tombstones`、`memory_audit_records`
- `goals`、`plans`、`plan_revisions`、`plan_stages`、`plan_task_templates`
- `tasks`、`task_executions`
- `knowledge_documents`、`knowledge_chunks`
- `claims`、`citations`
- `stored_files`、`notification_jobs`
- LangGraph 官方 PostgreSQL Checkpointer/Store 内部表及 pgvector 扩展

### 14.2 后续按需增加

- 独立通用作业队列和死信表
- Prompt 模板管理表和离线评测结果表
- 非任务型学习记录和能力评估表
- 组织、团队和企业多租户表
- 健康、职业、财务等领域专属表

不得提前创建没有明确查询、生命周期和数据所有者的扩展表。

## 15. 建表与迁移顺序

1. `users`、`user_identities`、`auth_sessions`、`user_profiles`、`study_availability`
2. `agent_definitions`、`user_agents`
3. `conversations`、`conversation_segments`、`conversation_summaries`、`messages`、`agent_runs`、`agent_run_events`、`agent_trace_spans`，再补聊天循环外键
4. `memory_policy_state`、`memory_extraction_jobs`、`memory_tombstones`、`memory_audit_records`
5. `goals`、`plans`、`plan_revisions`、`approval_decisions`、`approval_decision_items`
6. `plan_stages`、`plan_task_templates`、`tasks`、`task_executions`
7. `knowledge_documents`、`knowledge_chunks`、`claims`、`citations`
8. `stored_files`、`message_attachments`、`notification_jobs`，再补附件、头像外键
9. 启用 pgvector，安装并迁移 LangGraph PostgreSQL Checkpointer/Store
10. 创建索引、触发器、最小权限角色和测试数据

每次迁移必须同时提供向前迁移、回滚或前滚补救说明，以及约束和索引的自动化验证。
