# AI个人管家系统详细设计文档 V2.4

## 1. 文档目标

本文定义 AI个人管家在 10 人以内产品验证阶段的可实施系统设计，并保留演进到 100–1,000 用户的边界。

第一阶段只实现：

- 微信小程序和 H5 的用户登录与基础资料
- AI 管家入口和公考 Agent
- 用户画像采集、资料检索、计划生成与引用审核
- 用户审批、每日任务、执行反馈和计划调整

第一阶段不实现：

- 健康、财务等其他专业 Agent
- 成功率预测和能力评分
- 独立微服务、复杂自动扩容和多区域部署
- 由模型直接执行高风险外部操作
- Celery 和 API Gateway 的强制依赖

## 2. 设计原则

1. **业务事实唯一**：用户、目标、计划、任务等事实只以 PostgreSQL 为准。
2. **先确认后执行**：计划未获用户批准前，不创建正式任务或发送提醒。
3. **结构化边界**：Agent 输出必须通过版本化 Schema 校验后才能进入业务逻辑。
4. **可恢复**：每个聊天分段都有稳定的 `thread_id`；同一分段内的 run 通过 checkpoint 恢复，归档后由摘要承接到新线程。
5. **幂等副作用**：任务创建、审批恢复和通知发送均可安全重试。
6. **最小权限**：模型只能调用当前节点明确授权的工具，用户身份由服务端上下文注入。
7. **来源可追溯**：重要事实按 Claim 关联 Citation，不能只在回答末尾附链接。
8. **隐私可治理**：原始日志可按隐私流程删除，并有保留期限。

## 3. 总体架构

```text
uni-app（微信小程序 / H5）
     | POST 命令 / SSE 事件流
     | HTTPS
            |
FastAPI 模块化单体
  ├─ Auth / User
  ├─ Single Chat / Context Archive
  ├─ Long-term Memory Policy
  ├─ Agent Orchestrator
  ├─ Goal / Plan / Task
  ├─ Knowledge / Citation
  ├─ Chat Stream
  └─ Notification Scheduler
            |
   PostgreSQL Agent 队列
            |
       Agent Worker
            |
单个 LangGraph 工作流
  Router → Profile → Research → Planner
                         ↓
                Deterministic Review
                         ↓
                    Evidence Gate
                         ↓
                  Approval Interrupt
                         ↓
              Executor → Feedback → Adjust
            |
  ┌─────────┼─────────────┬──────────────┐
PostgreSQL  LangGraph              Qdrant       对象存储
业务事实     Checkpoint / Store     知识向量     私有文件
                │
             pgvector
```

验证阶段不设置独立 API Gateway。TLS、限流和请求大小限制由云负载入口或 Nginx 与 FastAPI 共同承担。

## 4. 运行单元

### 4.1 API 进程

职责：

- 认证、授权、参数校验和 API 响应
- 业务事务和租户隔离
- 创建或恢复 Agent run 的数据库任务
- 签发短期聊天流票据，从持久化事件表向客户端推送 SSE
- 向客户端返回消息接收结果、运行状态和事件续传位置

约束：

- 不在请求协程中执行长时间知识入库或批量通知。
- 不在请求协程中直接执行 LangGraph 或长时间模型调用。
- 不信任客户端传入的 `user_id`；从访问令牌中解析。
- 不将模型自由文本直接转为数据库写入或工具参数。

### 4.2 Agent Worker

验证阶段可与 API 使用同一代码库、不同进程启动。

职责：

- 执行 LangGraph 节点和模型调用
- 处理重试、checkpoint 恢复和审批后的继续执行
- 使用 PostgreSQL `agent_runs` 队列、lease 和 heartbeat 领取并续租任务
- 将允许展示的进度与 Response token 写入 `agent_run_events`
- 原子完成 Assistant 消息和 run 状态

验证阶段 Worker 通过 `FOR UPDATE SKIP LOCKED` 领取 `QUEUED` run。API 和 Worker 不使用进程内队列通信；进程重启后以数据库状态和 LangGraph checkpoint 恢复。

### 4.3 Scheduler Worker

验证阶段使用 PostgreSQL `notification_jobs` 表：

1. Worker 使用 `FOR UPDATE SKIP LOCKED` 领取到期作业。
2. 发送前检查 `idempotency_key` 和用户通知偏好。
3. 记录发送结果、渠道响应和下一次重试时间。
4. 达到最大尝试次数后进入 `DEAD` 状态并触发告警。

需要更高吞吐或复杂调度时，再将实现替换为 Redis + Celery；业务接口和幂等键保持不变。

## 5. 后端模块

### 5.1 Auth / User

职责：

- H5 手机号登录、可配置短信验证、微信登录码与授权手机号交换、访问令牌签发和刷新
- 用户状态、画像、学习时间和通知偏好管理
- 账号停用与数据删除申请

安全规则：

- 手机号、邮箱等敏感字段仅按业务必要性采集。
- 日志中对令牌、手机号、邮箱和用户原文进行脱敏。
- 停用用户不能创建新会话或恢复 Agent run。

### 5.2 Automatic Conversation / Context Archive

职责：

- 提供单一聊天入口；不提供手动新建，由消息入口自动延续、恢复或归档并创建场景
- 保存用户消息、Assistant 占位消息、最终内容和结构化卡片
- 将主聊天拆为内部 `conversation_segments`；每个分段映射一个不可变 LangGraph `thread_id`
- 处理用户级消息幂等、执行槽互斥和跨会话输入中断恢复
- 为客户端提供可续传的聊天事件流
- 依据模型 Token 预算在 70% 软阈值预生成摘要，在 85% 硬阈值归档分段并轮换线程
- 从累计交接摘要、最近消息、长期记忆和最新业务事实分层构建节点级上下文

规则：

- 客户端为每条消息生成 `client_message_id`。
- `(user_id, client_message_id)` 唯一；规范化请求 hash 相同则返回原会话和 `run_id`，相同 ID 的不同内容返回 `409 IDEMPOTENCY_KEY_REUSED`。
- 一个 conversation 同时最多有一个非终态 run。
- 一个 run 可以因输入或审批中断跨越多轮 User/Assistant 消息，不能假设一对一。
- SSE 连接断开不取消 run；取消和重试必须调用显式接口。
- 中间推理内容不写入 `messages`，仅保存可展示结果与必要审计字段。
- Research、Planner 原始 token 不流向客户端；只流式输出 Response 节点和预定义进度代码。
- 同一 segment/thread 开始新 run 时清空上一次 run 的工作字段；中断恢复同一 run 时保留工作状态。
- 归档不删除消息；客户端始终按主聊天全局时间线分页，不能感知或指定 segment。

### 5.3 Long-term Memory Policy

职责：

- 从用户明确陈述中提取低风险、跨分段仍有价值的偏好、习惯、约束和背景
- 使用 LangGraph PostgreSQL Store 保存结构化记忆，使用 pgvector 做语义候选召回
- 执行类型白名单、敏感信息拒绝、证据校验、重要性评分、TTL、冲突更新和遗忘屏障
- 通过 `memory_ref` 将摘要与活跃记忆解耦，避免更正或遗忘后旧摘要恢复旧值

模型只生成 `MemoryCandidateV1`，不能直接写 Store。普通提取异步执行；“记住、更正、忘掉”等明确命令在当前 run 内同步处理。Router 不读取长期记忆，只有确需个性化的节点通过受控 Context Builder 检索。

### 5.4 Agent Orchestrator

职责：

- 为每个用户提供内置 `BUTLER` User Agent，所有主聊天会话均绑定该实例
- 根据意图和专业 `user_agent` 状态选择一个或多个后台专业流程
- 加载当前线程和最新业务事实
- 执行单个 LangGraph 的节点和条件边
- 管理 run 状态、Schema 校验、重试和人工中断

`BUTLER` 是唯一用户可见的沟通入口。专业 Agent 不拥有用户可见会话，只为计划工作项提供领域处理能力；MVP 仅启用 `CIVIL_SERVICE_EXAM`。验证版节点不是独立服务，不拥有各自的数据库或用户会话。

### 5.5 Goal / Plan / Task

职责：

- 管理用户目标及生命周期
- 将计划逻辑实体与不可变 revision 分离
- 单计划批准后原子更新当前有效 revision；组合草案批准时，在一个事务内发布全部独立 revision，任一版本冲突则整组回滚
- Executor 根据全部已批准模板幂等物化近期任务
- 保存任务执行和反馈，按规则触发计划调整

关键不变量：

- 一个 `plan_revision` 发布后不可原地修改；编辑产生下一版本。
- 一个计划最多有一个当前批准 revision。
- `tasks.plan_revision_id` 必须指向批准过的 revision。
- 调整计划不能覆盖既有任务执行记录。
- 组合草案可以一次确认多个独立计划，但一次已有计划调整必须且只能包含一个 `target_plan_id`。
- 单计划调整只读取其他活动计划进行跨计划负荷校验；不得生成或发布其他计划的 revision。
- 如果目标计划的建议变化会造成总负荷冲突，返回警告或替代方案，并要求用户另起一次调整处理其他计划。

### 5.6 Knowledge / Citation

职责：

- 保存官方公告、大纲、真题等文档元数据
- 分块、生成 embedding 并写入 Qdrant
- 按知识域、租户、来源等级和有效期检索
- 将 Agent Claim 映射到具体知识分块

Qdrant 规则：

- 每个 embedding 模型和知识域使用共享 collection，不按用户创建 collection。
- Payload 至少包含 `tenant_id`、`document_id`、`chunk_id`、`source_level`、`published_at`、`valid_to`。
- 公共知识使用保留的公共租户标识；用户上传资料只能使用当前用户租户标识。
- 所有查询由服务端强制注入租户过滤，模型和客户端不能覆盖。
- `tenant_id`、`document_id`、`source_level` 等过滤字段创建 payload index。

### 5.7 Notification

模型只能提出提醒建议，不能直接调用微信、短信或邮件渠道。

由业务服务在计划批准或任务变化后创建 `notification_jobs`；Scheduler Worker 负责实际发送。

## 6. 核心数据流

### 6.1 新消息与 Agent 运行

```text
客户端发送消息 + client_message_id
  → 鉴权并幂等解析用户唯一主聊天及 ACTIVE segment
  → 事务锁定 conversation，检查幂等记录、归档状态和活动 run
  → 创建 User message、Assistant 占位 message、agent_run/run.accepted 事件
  → API 返回 202 和短期流票据，客户端建立 SSE
  → Agent Worker 领取 run，使用 agent_run.segment_id 对应的 thread_id 启动或恢复 LangGraph
  → Response token 与预定义进度写入 agent_run_events
  → SSE 按 sequence 推送；断线后从 Last-Event-ID 续传
  → 完成：原子保存最终 Assistant message 和 run 状态
  → 暂停：保存 interrupt，进入 AWAITING_INPUT / AWAITING_APPROVAL
  → 失败：保存结构化错误，进入 FAILED_RETRYABLE / FAILED_FINAL
  → 终态后计算预计上下文；70% 刷新摘要
  → 85% 时先封存旧段并原子创建新 ACTIVE segment/thread，再生成交接摘要
```

发送消息时按活动 run 状态处理：

- 无活动 run：创建新 run，`pending_action=START`。
- `AWAITING_INPUT`：创建本轮消息，恢复同一 run，`pending_action=INPUT_RESUME`。
- `AWAITING_APPROVAL`：返回 `409 APPROVAL_REQUIRED`，要求使用审批接口。
- `QUEUED`、`RUNNING`、`CANCEL_REQUESTED`：返回 `409 CONVERSATION_BUSY`。
- `FAILED_RETRYABLE`：返回 `409 RUN_RETRY_REQUIRED`，用户先选择重试或取消。

完整协议见[《AI个人管家聊天系统设计文档》](./AI个人管家聊天系统设计.md)。

### 6.2 计划生成与审批

```text
Profile 完整
  → Research 生成 Claims/Citations
  → Planner 生成 plan_revision、阶段和任务模板草稿
  → 确定性计划校验与 Evidence Gate
  → 持久化草稿并创建 approval_decisions(PENDING)
  → LangGraph interrupt
  → 用户 APPROVE / EDIT / REJECT

APPROVE → 记录决策并恢复图 → 原子发布 revision → Executor 创建任务 → 创建通知作业
EDIT    → 产生下一 revision → 重新审核
REJECT  → 保留历史 → run 正常结束，不创建任务
```

审批接口在保存决定的同一事务创建展示决定的 `SYSTEM_EVENT` 和新的 Assistant 占位消息，并设置 `pending_action=APPROVAL_RESUME`；批准、编辑或拒绝都恢复原 `run_id/thread_id`，不创建第二个 run。

组合草案包含多个独立计划工作项时，一个 `approval_decision` 关联多个待发布 revision。审批事务按 `plan_id` 稳定排序加锁，逐项校验预期当前版本，再一次性发布全部 revision；任一项不存在、越权或版本冲突时整体回滚，不创建任何正式任务。任务物化仍按各自 `plan_revision_id` 幂等执行。

### 6.3 反馈和计划调整

```text
用户提交任务执行结果
  → 幂等写 task_execution
  → 更新任务状态
  → 评估调整触发条件
  → 未触发：结束
  → 触发：生成新 plan_revision
  → 审核并再次请求用户确认
```

默认调整触发条件：用户明确要求调整，或连续 3 个计划日未完成关键任务。系统只能建议调整，不能静默替换当前计划。

调整意图必须先解析唯一 `target_plan_id`。未命中或命中多个活动计划时进入输入中断并发送 `SelectionCard`；获得唯一选择后才允许生成 revision。用户发送“确认”等自由文本不能完成审批，必须提交结构化审批决定。

## 7. 公共 API

所有接口使用 `/v1` 前缀、JSON 和 UTF-8。时间使用 ISO 8601 且带时区。分页使用 `cursor` 和 `limit`。

### 7.1 用户与 Agent

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/v1/auth/config` | 获取公开登录能力配置 |
| `POST` | `/v1/auth/phone/verification-codes` | 发送手机号登录验证码 |
| `POST` | `/v1/auth/phone/login` | 使用唯一手机号登录 |
| `POST` | `/v1/auth/wechat/login` | 使用微信登录码和授权手机号换取访问令牌 |
| `POST` | `/v1/auth/refresh` | 轮换刷新令牌并签发新访问令牌 |
| `POST` | `/v1/auth/logout` | 撤销当前刷新会话 |
| `GET` | `/v1/me` | 获取当前用户 |
| `PATCH` | `/v1/me` | 更新允许修改的基础资料 |
| `GET`/`PUT` | `/v1/me/profile` | 获取或更新规划画像 |
| `GET`/`PUT` | `/v1/me/availability` | 获取或更新学习时间 |
| `GET` | `/v1/agent-definitions` | 获取可启用的 Agent 类型 |
| `POST` | `/v1/user-agents` | 启用一个 Agent |
| `PATCH` | `/v1/user-agents/{id}` | 暂停、恢复或完成 User Agent |

### 7.2 自动会话与运行

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/v1/conversations` | 获取非空当前话题和历史话题 |
| `GET` | `/v1/conversations/{id}/messages` | 跨内部 segment 分页获取可展示消息 |
| `POST` | `/v1/messages` | 自动决定真实会话，创建用户消息并启动或恢复 run |
| `GET` | `/v1/agent-runs/{run_id}` | 获取运行结果、进度或待办动作 |
| `POST` | `/v1/agent-runs/{run_id}/stream-ticket` | 获取短期、仅限本 run 的流票据 |
| `GET` | `/v1/agent-runs/{run_id}/events` | 通过 SSE 获取和续传展示事件 |
| `POST` | `/v1/agent-runs/{run_id}/cancel` | 幂等请求取消 run |
| `POST` | `/v1/agent-runs/{run_id}/retry` | 从同一 checkpoint 重试可重试失败 |
| `POST` | `/v1/approvals/{approval_id}/decisions` | 批准、编辑或拒绝待确认内容 |

客户端不传 `segment_id`。普通输入由服务端自动路由；历史续聊可传 `target_conversation_id`，专业入口可传 `specialist_code`。客户端必须以响应中的实际 `conversation_id` 建立消息流。

消息请求：

```json
{
  "schema_version": "1.0",
  "client_message_id": "01J...",
  "content": "我准备参加 2027 年国考，每天可学习 2 小时",
  "attachments": []
}
```

成功返回 `202 Accepted`，同时包含已完成的 User 消息、`PENDING` Assistant 占位消息、`QUEUED` run、`events_url`、短期 `ticket` 和 `last_sequence`。重复提交相同 `client_message_id` 且规范化内容/附件 hash 相同则返回首次结果，不同则返回 `409 IDEMPOTENCY_KEY_REUSED`。

重试请求携带 `expected_attempt`。服务端只允许匹配的 `FAILED_RETRYABLE` run 原子递增 attempt 并重排队；重复的同一 attempt 请求返回首次结果，其他版本冲突返回 `409 RUN_ATTEMPT_CONFLICT`。

审批请求：

```json
{
  "action": "APPROVE",
  "expected_approval_version": 1,
  "feedback": null,
  "edited_fields": null
}
```

`action` 取值为 `APPROVE`、`EDIT`、`REJECT`。`APPROVE` 和 `REJECT` 只能由结构化卡片按钮触发；`EDIT` 的 `feedback` 保存用户在编辑上下文中提交的说明。每个审批关联项分别携带 `plan_revision_id` 和 `expected_current_revision_id`。重复提交同一审批决策返回首次处理结果；审批版本或任一计划版本冲突返回 `409`，组合审批不允许部分成功。

### 7.3 聊天事件流

验证版使用“POST 消息 + SSE 事件流”，不引入 WebSocket：

- H5 使用 `EventSource` 和短期签名流票据。
- 微信小程序使用 `uni.request(enableChunked=true)` 和 `onChunkReceived` 解析同一 SSE 协议。
- 持久化事件包含 `run.accepted`、`run.status`、`progress`、`message.start`、`message.delta`、`message.reset`、`message.completed`、`interrupt`、`run.completed`、`run.cancelled` 和 `error`。
- `heartbeat` 每 15 秒发送但不持久化。
- 每个 run 的 `sequence` 单调递增；客户端使用 `Last-Event-ID` 或 `after` 续传并按序列去重。
- 事件从 run 终态后默认保留 7 天，非终态事件不清理。游标已过期时返回 `410 STREAM_CURSOR_EXPIRED`，客户端重新获取消息和 run 状态。
- 断开 SSE 不取消后台 run；页面返回时先进行状态补偿，再决定是否重连。

SSE 响应设置 `Content-Type: text/event-stream`、`Cache-Control: no-cache, no-transform` 和 `X-Accel-Buffering: no`。流票据默认 10 分钟有效，只绑定当前用户与 run，代理日志必须隐藏查询参数。

### 7.4 目标、计划和任务

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/goals` | 创建目标 |
| `GET` | `/v1/goals` | 列出目标 |
| `PATCH` | `/v1/goals/{id}` | 更新允许变更的目标字段 |
| `GET` | `/v1/plans/{id}` | 获取计划和当前 revision |
| `GET` | `/v1/plans/{id}/revisions` | 获取版本历史 |
| `GET` | `/v1/tasks` | 按日期、状态查询任务 |
| `GET` | `/v1/tasks/{id}` | 获取任务详情及来源计划 |
| `POST` | `/v1/tasks/{id}/executions` | 打卡、记录时长和反馈 |

### 7.5 文件

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/files/upload-intents` | 获取私有对象上传凭证 |
| `GET` | `/v1/files/{id}/download-url` | 获取短期签名下载地址 |
| `DELETE` | `/v1/files/{id}` | 删除文件及关联向量 |

## 8. 统一响应和错误契约

Agent 运行响应：

```json
{
  "schema_version": "2.0",
  "run_id": "uuid",
  "status": "AWAITING_APPROVAL",
  "data": {},
  "citations": [],
  "warnings": [],
  "next_action": {
    "type": "REVIEW_PLAN",
    "approval_id": "uuid"
  },
  "error": null
}
```

`status` 取值：

- `QUEUED`
- `RUNNING`
- `AWAITING_INPUT`
- `AWAITING_APPROVAL`
- `SUCCEEDED`
- `FAILED_RETRYABLE`
- `FAILED_FINAL`
- `CANCEL_REQUESTED`
- `CANCELLED`

错误响应：

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

客户端只根据 `code` 分支，不解析 `message`。

## 9. 状态、事务与幂等

### 9.1 事务边界

- 创建 User 消息、Assistant 占位消息、对应 run 和首个事件在同一事务提交。
- 完成 Assistant 消息、run 终态和完成事件在同一事务提交。
- 恢复等待输入的 run 时，创建本轮消息并更新 `pending_action` 在同一事务提交。
- 每次 `START/INPUT_RESUME/APPROVAL_RESUME/RETRY` 写稳定 `pending_action_key`；确认该 key 已进入 checkpoint 后才清空 pending action。
- 每个聊天事件的序列分配和事件插入在同一事务提交。
- 单计划审批或组合审批的全部 revision 发布和当前版本切换在同一事务提交；组合审批任一项失败则整体回滚。
- Executor 生成的任务与对应通知作业在后续独立事务中提交，并通过 `task_key` 和通知幂等键安全恢复。
- Qdrant 和对象存储不参与 PostgreSQL 分布式事务，使用状态字段和可重试补偿作业。

### 9.2 幂等键

| 操作 | 幂等键 |
| --- | --- |
| 发送消息 | `conversation_id + client_message_id` |
| 创建 Agent run | `trigger_message_id` |
| 写聊天事件 | `agent_run_id + sequence` |
| 取消 run | `agent_run_id`，重复取消返回当前状态 |
| 重试 run | `agent_run_id + expected_attempt`；生成稳定的下一 attempt key |
| 启动/恢复命令 | `agent_run_id + pending_action_key`，并与 checkpoint 对账 |
| 审批恢复 | `approval_id + expected_approval_version`，只允许一个终态决策 |
| 创建任务 | `plan_revision_id + task_key` |
| 任务打卡 | `task_id + client_execution_id` |
| 发送通知 | `user_id + channel + event_type + entity_id + scheduled_at` |

LangGraph interrupt 恢复时会重新进入节点，因此中断前不得执行非幂等副作用；副作用应放入审批后的独立节点。

### 9.3 重试策略

- 模型限流、连接错误、网络超时和 5xx：切换到静态跨供应商备用；SDK 自动重试关闭。
- Schema 校验失败：仅在同一实际模型修复 1 次；单个逻辑任务连同主备切换最多调用 3 次。
- 找不到可靠来源：不盲目重试，返回 `INSUFFICIENT_EVIDENCE` 并请求补充信息或人工处理。
- Qdrant/对象存储短暂失败：记录待补偿状态，由 Worker 重试。
- 参数错误、权限错误和用户拒绝：不重试。
- `FAILED_RETRYABLE` 从同一 checkpoint 重试同一个 run；不得创建新 run。
- 重试已有部分回复时先写 `message.reset`，再输出新 attempt。
- Worker 使用 lease/heartbeat；lease 到期后其他 Worker 可从 checkpoint 重新领取。

## 10. 数据归属与一致性

| 数据 | 主存储 | 辅助存储 | 一致性规则 |
| --- | --- | --- | --- |
| 用户、目标、计划、任务 | PostgreSQL | 无 | 强一致事务 |
| 可展示聊天消息 | PostgreSQL | LangGraph checkpoint 包含工作副本 | PostgreSQL 为用户可见事实 |
| 聊天分段与压缩摘要 | PostgreSQL | LangGraph checkpoint | 分段控制线程轮换；摘要是可重建的派生上下文 |
| 用户长期记忆 | LangGraph PostgreSQL Store | PostgreSQL 任务、屏障和无明文审计 | Store 保存活跃值；业务库控制准入、遗忘和重试 |
| 聊天增量事件 | `agent_run_events` | 无 | 只保存展示投影，按 run sequence 续传，run 终态后默认保留 7 天 |
| Agent 运行状态 | `agent_runs` | LangGraph checkpoint | run 保存摘要状态，checkpoint 保存恢复细节 |
| 知识元数据 | PostgreSQL | Qdrant payload | `knowledge_chunk.id` 是统一映射 ID |
| 文件 | 对象存储 | PostgreSQL `stored_files` | PostgreSQL 状态控制可见性 |

任何跨存储删除均先在 PostgreSQL 标记 `DELETING`，完成外围删除后标记 `DELETED`；失败进入可重试作业。

## 11. 安全与隐私

### 11.1 访问控制

- 每个业务查询均从认证上下文获得 `user_id`，禁止由请求体覆盖。
- 所有用户实体查询同时匹配实体 ID 和 `user_id` 所有权路径。
- Qdrant 用户资料查询必须包含服务端生成的 `tenant_id` 过滤。
- 管理员知识入库接口与用户 API 使用不同权限范围。
- 流票据只绑定当前 `user_id + run_id`、短期有效，且不得出现在访问日志和埋点中。
- Markdown 使用白名单渲染，禁用原始 HTML、脚本和危险 URL scheme。

### 11.2 Prompt 与工具安全

- 用户消息、网页、文档和检索片段都视为不可信数据，不得改变系统指令。
- 工具参数使用 Schema 和服务端所有权校验，不能直接执行模型生成的 SQL、URL 或代码。
- Research 节点只有只读检索工具；Planner 不具备写业务数据权限。
- Executor 只能提出经过 Schema 校验的任务草稿，正式写入由业务服务完成。
- 外部 URL 访问启用协议、域名、重定向、响应大小和超时限制，防止 SSRF。

### 11.3 隐私与保留

- `agent_runs` 默认只保存脱敏输入摘要和结构化输出；`model_invocations` 只保存模型、Token、耗时、状态和主备元数据。
- 调试用原始输入输出默认关闭；临时开启时保留不超过 7 天并限制管理员访问。
- 普通运行日志默认保留 30 天，审核和审批记录随业务实体保留。
- 用户删除账号时清理 PostgreSQL 业务数据、全部分段 checkpoint、LangGraph Store namespace、Qdrant 用户向量和对象文件。
- 长期记忆不在设置页展示；用户可用聊天指令更正、遗忘或暂停后续捕获。遗忘立即阻断 Store、摘要引用和历史召回，但不删除用户仍可见的原聊天消息。

## 12. 可观测性

每个请求贯穿以下标识：

- `request_id`
- `user_id`（日志中使用不可逆内部标识）
- `conversation_id`
- `segment_id`
- `thread_id`
- `run_id`
- `trace_id`
- `message_id`
- `attempt`
- `worker_id`

核心指标：

- API p50/p95 延迟和错误率
- Agent run 成功率、暂停率、重试率和最终失败率
- 排队时长、首个进度时长、首 token 时长和总耗时
- SSE 在线连接、重连率、补发量和事件推送延迟
- Worker lease 过期和 checkpoint 恢复成功率
- 各节点模型延迟、Token、调用次数、Schema 修复率和主备切换率
- Schema 校验失败率
- Evidence Gate 拦截率和无可引用来源率
- 通知积压、发送成功率和死信数量
- Qdrant 检索延迟和空结果率
- 活跃分段 Token 占比、摘要压缩率、线程轮换次数和归档等待时长
- 记忆候选准入率、拒绝原因、Recall@8、过期/遗忘误召回数和检索延迟（日志不记录记忆明文）

告警最低要求：服务不可用、数据库连接耗尽、run 长时间排队、Worker heartbeat 超时、连续模型失败、SSE 异常重连、通知死信、备份失败和疑似跨租户访问。

## 13. 部署与备份

### 13.1 验证阶段

推荐单台 2 核、8 GB 内存、80 GB SSD 云服务器；4 GB 仅适用于 PostgreSQL、Qdrant 或对象存储至少一项使用托管服务的情况。

```text
docker compose
  ├─ api
  ├─ agent-worker
  ├─ scheduler-worker
  ├─ postgres
  └─ qdrant

外部：模型 API、对象存储、微信登录/通知渠道
```

LangGraph 数据库必须启用 pgvector 扩展，并由 `AsyncPostgresStore.setup()` 管理 Store/向量索引。Qdrant 继续只承担知识文档向量，不混入用户长期记忆。

容器使用固定版本，不使用 `latest`。密钥通过部署环境或密钥管理服务注入，不写入镜像和仓库。Nginx 对聊天事件路径关闭响应缓冲，空闲超时大于 45 秒；SSE heartbeat 默认每 15 秒发送。

### 13.2 备份目标

- PostgreSQL：每日全量备份，开启持续归档条件允许时启用；默认 RPO 24 小时。
- Qdrant：知识库更新后或每日快照；向量可由 PostgreSQL 元数据和原文件重建。
- 对象存储：开启版本控制或回收站策略。
- 每月至少执行一次恢复演练；默认 RTO 4 小时。

## 14. 演进到 100–1,000 用户

只有观测数据达到阈值后才扩展：

1. 将 PostgreSQL、Qdrant 和对象存储迁移为独立或托管实例。
2. API 和 Agent Worker 分别横向扩容，继续共享数据库和 checkpoint。
3. 当数据库作业表出现持续积压时，引入 Redis + Celery，保留原幂等契约。
4. 当知识量或检索延迟要求提升时，增加 Qdrant 副本、分片和独立备份。
5. 当某节点需要独立扩缩容、安全域或团队所有权时，才拆分为独立服务。

## 15. 开发顺序

1. 用户、认证、Agent 定义和 User Agent
2. 单主聊天、segment、消息、run、持久化聊天事件、SSE 与 LangGraph PostgreSQL checkpoint
3. Token 预算、两级摘要、自动归档、线程轮换和 checkpoint 清理
4. LangGraph Store、记忆提取作业、Policy、检索、更正与遗忘
5. 用户画像、目标和 Profile 节点
6. 知识入库、Qdrant 检索和 Claim/Citation
7. Planner、确定性计划校验和 Evidence Gate
8. Approval interrupt、计划版本发布和任务物化
9. 任务反馈、通知 Worker、可观测性、安全测试和恢复演练

每个阶段完成数据库约束、接口测试和失败恢复测试后再进入下一阶段。
