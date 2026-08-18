# AI个人管家聊天系统设计文档 V3.0

## 1. 文档目标

本文补充 AI 聊天从客户端发送消息到 Agent 回复落库的完整实现，并作为以下文档的聊天专项设计：

- [《AI个人管家系统详细设计文档》](./AI个人管家系统详细设计.md)
- [《AI个人管家数据库设计文档》](./AI个人管家数据库设计.md)
- [《AI个人管家 Agent 流程与 Prompt 设计文档》](./AI个人管家Agent流程与Prompt设计.md)

本设计保持现有技术栈不变：

- 客户端：uni-app
- API 与 Worker：FastAPI / Python
- Agent 编排：LangGraph
- 业务数据：PostgreSQL
- 向量检索：Qdrant
- 验证阶段不引入 Redis、Celery 或 WebSocket

目标是支持 10 人以内验证，并保留向 100–1,000 用户演进的边界。

## 2. 范围与非目标

### 2.1 本期范围

- 单一消息入口、自动会话归档与历史恢复（不提供手动新建）
- 消息历史分页
- 不可见的上下文分段、智能压缩、自动归档和线程轮换
- 跨归档分段的受控长期记忆
- 用户消息幂等提交
- Agent 异步执行和流式回复
- LangGraph 输入中断、审批中断与同一 run 恢复
- 网络断开后的事件续传和状态补偿
- 取消、可重试失败和服务重启恢复
- 引用、卡片等结构化消息内容
- 消息、run 和事件的用户隔离、脱敏与保留

### 2.2 非目标

- 多人群聊
- 语音/视频实时通话
- Agent 原始思维链展示
- 多端同时编辑同一条消息
- 已发送消息原地修改；用户应发送新消息表达更正

## 3. 架构决策

### 3.1 总体结构

```text
uni-app
  ├─ POST /messages：提交命令
  ├─ GET /events：接收 SSE 事件
  └─ GET /agent-runs/{id}：超时后的状态补偿
        │
        ▼
FastAPI API
  ├─ 鉴权、会话权限、幂等
  ├─ 消息与 run 事务
  ├─ 短期流票据
  └─ 从 agent_run_events 推送 SSE
        │
        ▼
PostgreSQL
  ├─ conversations / conversation_segments / messages
  ├─ conversation_summaries / memory policy jobs
  ├─ agent_runs：数据库任务队列与摘要状态
  └─ agent_run_events：可续传的展示事件
        ▲
        │ FOR UPDATE SKIP LOCKED + lease
Agent Worker
  ├─ LangGraph astream
  ├─ Checkpointer / PostgresStore + pgvector
  ├─ 结构化进度与回复投影
  └─ Qdrant 检索
```

API 和 Agent Worker 可以先部署在同一台服务器，但必须作为两个独立进程运行。API 进程不直接执行长时间模型调用，避免请求超时或进程重启导致 run 丢失。

### 3.2 为什么验证版使用 SSE

聊天的主要实时方向是服务器向客户端持续推送 Agent 输出；用户消息仍通过普通 HTTP 提交。因此验证版使用“POST 命令 + SSE 事件流”，不引入 WebSocket。

优势：

- 复用 HTTP 鉴权、代理、日志和限流能力
- 天然具有事件 ID 和重连语义
- H5 可使用 `EventSource`
- 小程序端可用分块 HTTP 接收同一 SSE 协议
- 断开事件连接不会取消后台 run

如果以后增加语音实时通话、服务端主动要求双向低延迟控制，再单独评估 WebSocket。

### 3.3 PostgreSQL 事件表而非进程内广播

SSE 接口从 `agent_run_events` 按序读取事件，Worker 将面向用户的事件写入该表。不能只使用进程内队列，因为 API 与 Worker 是不同进程，且任一进程重启后必须能够续传。

验证阶段 SSE 服务每 250 ms 左右查询一次新事件；并发提高后可使用 PostgreSQL `LISTEN/NOTIFY` 作为唤醒信号，但事件表仍是真实来源。

## 4. 核心对象与语义

### 4.1 Conversation

- 每个用户可拥有多个会话，但最多一个 `CURRENT`；其他为 `ARCHIVED`。所有会话绑定内置 `BUTLER`，专业会话另固定 `specialist_user_agent_id`。
- 用户不手动新建会话。发送消息时由结构化流程、专业助理边界和 `ConversationRouter` 依次判断延续、恢复或归档并创建场景；归档、创建和首条消息写入同一事务。
- 专业助理欢迎页是客户端临时状态，不写数据库；首次发送才创建专业场景。同一助理存在待回复、待审批或待重试任务时优先恢复。
- 已归档会话可软删除；`deleted_at` 生效后所有公共会话和消息查询均按不存在处理，CURRENT 会话禁止删除。
- 后台 `conversation_segments` 对模型上下文分段，每个 segment 对应一个不可变 `thread_id`；用户看到的消息时间线跨 segment 连续。
- 产品会话归档与 segment 轮换是两套状态机：前者不删除消息、摘要或 checkpoint，后者只压缩和轮换内部上下文。

### 4.2 Segment 与 Summary

- segment 状态为 `ACTIVE → ARCHIVING → ARCHIVED`；每个 conversation 同时只能有一个 ACTIVE segment。
- 当前 segment 存在非终态 run 时不能归档；等待输入或审批的 run 必须在原 segment/thread 恢复。
- `SEGMENT_FINAL` 总结单段，`CUMULATIVE_HANDOFF` 将上一交接摘要与新段摘要增量压缩。
- 长期用户事实在摘要中只保存 `memory_ref`，运行时解析活跃 Store 值，避免更正或遗忘后旧摘要复活事实。

### 4.3 Message

消息角色：

- `USER`：用户输入，提交成功即为 `COMPLETED`。
- `ASSISTANT`：Agent 可展示回复，依次经历 `PENDING → STREAMING → COMPLETED`，也可能变为 `FAILED` 或 `CANCELLED`。
- `SYSTEM_EVENT`：审批、计划已创建等可展示系统事件，不承载内部日志。

`messages.content` 保存最终可显示文本；引用、计划卡片、按钮和警告写入 `structured_content`。Agent 原始推理、工具参数和未脱敏检索内容不能写入消息表或推给客户端。

一次 run 可能跨越多轮消息。例如 Agent 询问缺失资料后进入 `AWAITING_INPUT`，用户回答时恢复同一个 run，并为这一轮新建一个 Assistant 占位消息。因此不能假设一个 run 只对应一条 Assistant 消息。

### 4.4 Agent Run

run 是一次可以暂停和恢复的 LangGraph 业务执行，状态如下：

```text
QUEUED → RUNNING
RUNNING → AWAITING_INPUT → QUEUED → RUNNING
RUNNING → AWAITING_APPROVAL → QUEUED → RUNNING
RUNNING → SUCCEEDED
RUNNING → FAILED_RETRYABLE → QUEUED → RUNNING
RUNNING → FAILED_FINAL
RUNNING → CANCEL_REQUESTED → CANCELLED
QUEUED/等待态/可重试失败 → CANCELLED
```

每个会话最多一个非终态 run；全用户同一时间最多一个真正执行中的 run。等待用户的流程可跨会话并存。

用户级执行互斥状态包括：

- `QUEUED`
- `RUNNING`
- `CANCEL_REQUESTED`

`AWAITING_INPUT`、`AWAITING_APPROVAL`、`FAILED_RETRYABLE` 属于可持久挂起状态，不占用用户级执行槽。

### 4.5 Event

事件是 run 面向客户端的增量投影，不是完整审计日志。每个 run 内的 `sequence` 从 1 单调递增，并在数据库事务中分配。

持久化事件从 run 进入终态后默认保留 7 天；非终态 run 的事件不得按时间直接清理。最终消息、run 摘要和审批结果按业务数据保留策略保存。

## 5. API 契约

### 5.1 多会话

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/agent-definitions` | 获取专业快捷入口目录 |
| `GET` | `/v1/conversations` | CURRENT 优先、最近消息倒序分页 |
| `GET` | `/v1/conversations/{id}` | 获取会话详情和活动 run |
| `DELETE` | `/v1/conversations/{id}` | 幂等软删除已归档会话 |
| `GET` | `/v1/conversations/{id}/messages` | 跨内部 segment 分页获取该会话消息 |

客户端在路径中传公开会话 ID，但归属只从认证用户校验；跨用户访问统一安全 404。客户端不传 `segment_id`、`thread_id` 或 `user_agent_id`。

历史消息按 `(created_at, id)` 排序，游标必须同时包含这两个字段，避免相同时间戳导致遗漏或重复。首屏返回最新消息，向上滚动时获取更早消息。

### 5.2 发送消息

```http
POST /v1/messages
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "schema_version": "1.0",
  "client_message_id": "0190...",
  "target_conversation_id": null,
  "specialist_code": null,
  "context_policy": "AUTO",
  "execution_policy": "REJECT",
  "content": "我每天晚上可以学习两个小时",
  "attachments": []
}
```

成功返回 `202 Accepted`：

```json
{
  "schema_version": "1.0",
  "conversation_id": "uuid",
  "transition": {
    "kind": "CONTINUED",
    "archived_conversation_id": null
  },
  "user_message": {
    "id": "uuid",
    "status": "COMPLETED"
  },
  "assistant_message": {
    "id": "uuid",
    "status": "PENDING"
  },
  "run": {
    "id": "uuid",
    "status": "QUEUED",
    "execution_mode": "START"
  },
  "stream": {
    "events_url": "/v1/agent-runs/uuid/events",
    "ticket": "short-lived-signed-ticket",
    "expires_at": "2026-08-06T12:10:00Z",
    "last_sequence": 1
  }
}
```

`execution_mode` 取值：

- `START`：创建新 run。
- `INPUT_RESUME`：当前 run 在等待普通用户输入，恢复同一 run。

服务端对规范化 `content + 有序附件 ID` 计算 `client_request_hash`。重复提交相同 `conversation_id + client_message_id` 且 hash 相同时，返回第一次写入的消息、run 和当前流位置，不创建新数据；相同 ID 携带不同内容时返回 `409 IDEMPOTENCY_KEY_REUSED`。

### 5.3 发送时的状态分支

API 在事务内锁定会话，并按当前非终态 run 决定行为：

| 当前状态 | 行为 |
|---|---|
| 无非终态 run | 创建用户消息、Assistant 占位消息和新 run |
| `AWAITING_INPUT` | 创建本轮消息，设置 `pending_action=INPUT_RESUME`，恢复同一 run |
| `AWAITING_APPROVAL` | 返回 `409 APPROVAL_REQUIRED`，必须调用审批接口 |
| `QUEUED` / `RUNNING` / `CANCEL_REQUESTED` | 返回 `409 CONVERSATION_BUSY` |
| `FAILED_RETRYABLE` | 返回 `409 RUN_RETRY_REQUIRED`，先重试或取消 |

附件通过 `message_attachments` 关联，必须已完成上传和安全扫描、用途为 `CHAT_ATTACHMENT`，且属于当前用户；否则整次消息提交失败。客户端传入顺序参与 hash，服务端不接受对象存储 key 或公开 URL。

### 5.4 流票据与事件流

H5 原生 `EventSource` 不方便添加 Bearer Header，因此使用短期签名流票据：

```http
POST /v1/agent-runs/{run_id}/stream-ticket
Authorization: Bearer <access_token>
```

票据要求：

- 有效期默认 10 分钟
- 只绑定 `user_id + run_id + purpose=chat_stream`
- 不包含可逆的用户隐私数据
- 事件接口再次校验 run 所有权
- 代理访问日志必须隐藏 `ticket` 查询参数

票据只在建立连接时校验；已经建立的合法流连接可以运行到 run 终态或连接断开，不因票据中途过期被强制关闭。

连接接口：

```http
GET /v1/agent-runs/{run_id}/events?ticket=<ticket>&after=41
Accept: text/event-stream
Last-Event-ID: 41
```

`after` 和 `Last-Event-ID` 同时存在时必须一致，否则返回 `400 INVALID_STREAM_CURSOR`。已登录且支持 Header 的客户端也可以使用 Bearer Token，不强制申请票据。

响应头至少包含：

```text
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

服务端每 15 秒发送一次不持久化的 `heartbeat`。反向代理空闲超时应大于 45 秒，并关闭该路径的响应缓冲和压缩聚合。

事件保留期已过且无法从请求序列续传时返回 `410 STREAM_CURSOR_EXPIRED`。客户端重新获取消息和 run 状态，再从服务端返回的最新序列建立连接。

FastAPI 使用 `EventSourceResponse` 和异步生成器实现该接口。生成器的逻辑顺序固定为：校验用户与游标 → 分批查询 `sequence > cursor` → 逐条发送并推进 cursor → 无新事件时等待约 250 ms → 到 heartbeat 时间发送保活 → run 终态且事件已发完后关闭。客户端断开只结束生成器，不能修改 run 状态。

```python
async def run_events(run_id: UUID, cursor: int, principal: Principal):
    await assert_run_owner(run_id, principal.user_id)
    while True:
        events = await event_repo.list_after(run_id, cursor, limit=100)
        for item in events:
            cursor = item.sequence
            yield to_sse(item)
        if await run_repo.is_terminal_and_drained(run_id, cursor):
            return
        if heartbeat_due():
            yield heartbeat_event()
        await asyncio.sleep(0.25)
```

此代码仅表达控制流程；实现时数据库会话不能跨 `yield` 长期占用，连接关闭和任务取消异常必须在生成器边界处理。

### 5.5 Run 控制与补偿查询

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/agent-runs/{id}` | 获取 run 状态、当前消息和最新序列 |
| `POST` | `/v1/agent-runs/{id}/cancel` | 幂等请求取消 |
| `POST` | `/v1/agent-runs/{id}/retry` | 从同一 checkpoint 重试可重试失败 |
| `POST` | `/v1/approvals/{approval_id}/decisions` | 提交审批并恢复同一 run |

网络断开不会调用取消接口，也不会隐式取消 run。取消和重试都必须显式操作。

重试请求必须携带当前看到的 attempt：

```json
{
  "schema_version": "1.0",
  "expected_attempt": 1
}
```

服务端只允许 `FAILED_RETRYABLE` 且 attempt 匹配时原子递增，生成 `pending_action_key=RETRY:2` 并重新排队。相同请求重复到达且当前 pending key 已为 `RETRY:2` 时返回首次结果；attempt 不匹配且不是该重复请求时返回 `409 RUN_ATTEMPT_CONFLICT`。

审批请求固定为：

```json
{
  "schema_version": "1.0",
  "action": "APPROVE",
  "expected_approval_version": 1,
  "feedback": null
}
```

`APPROVE` 和 `REJECT` 只能由当前审批卡的结构化按钮触发。用户点击“继续修改”后，客户端进入 `APPROVAL_EDIT` 编辑上下文，下一段文字以 `action=EDIT` 和 `feedback` 提交本接口，不调用消息接口。组合审批的全部 revision 整组成功或整组失败。

### 5.6 结构化卡片协议

所有卡片写入 `messages.structured_content.cards`，统一信封为：

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

允许的卡片：

| 类型 | 必需实体 | 用途 | 允许动作 |
| --- | --- | --- | --- |
| `SelectionCard` | 候选项的稳定实体 ID | 缺失信息或多计划目标选择 | `SELECT`、`SUBMIT_SELECTION` |
| `PlanCard` | `approval_id`、`approval_version`、一个或多个 `plan_revision_id` | 展示组合新建或单计划调整草案 | `APPROVE`、`EDIT`、`REJECT` |
| `StatusCard` | `run_id` | 展示预定义进度，不包含内部推理 | 无，或 `CANCEL_RUN` |
| `SourceCard` | 有序 `citation_ids` 和来源摘要 | 展示回答引用来源入口 | `OPEN_SOURCE` |

动作对象至少包含 `action_id`、`action_type`、`label` 和执行所需的稳定实体 ID。客户端不得根据标题或按钮文案反推业务操作。未知 `schema_version` 或 `card_type` 时回退为只读文本，不展示可写按钮。

新输出的 `PlanCard 1.1` 示例：

```json
{
  "schema_version": "1.1",
  "card_id": "uuid",
  "card_type": "PlanCard",
  "entity_refs": {
    "approval_id": "uuid",
    "approval_version": 1,
    "items": [
      {
        "work_item_id":"work-1",
        "plan_id":"plan-1-uuid",
        "plan_revision_id":"revision-1-uuid",
        "expected_current_revision_id":null
      },
      {
        "work_item_id":"work-2",
        "plan_id":"plan-2-uuid",
        "plan_revision_id":"revision-2-uuid",
        "expected_current_revision_id":null
      }
    ]
  },
  "payload": {
    "mode":"BUNDLE_CREATE",
    "title":"组合计划草案",
    "plans":[
      {
        "work_item_id":"work-1",
        "plan_id":"plan-1-uuid",
        "plan_revision_id":"revision-1-uuid",
        "title":"行测计划",
        "objective_summary":"四周行测训练",
        "weekly_minutes":240
      },
      {
        "work_item_id":"work-2",
        "plan_id":"plan-2-uuid",
        "plan_revision_id":"revision-2-uuid",
        "title":"申论计划",
        "objective_summary":"四周申论训练",
        "weekly_minutes":180
      }
    ],
    "total_weekly_minutes":420,
    "available_weekly_minutes":500,
    "warnings":[]
  },
  "actions": [
    {"action_id":"approve","action_type":"APPROVE","label":"确认并创建"},
    {"action_id":"edit","action_type":"EDIT","label":"继续修改"},
    {"action_id":"reject","action_type":"REJECT","label":"放弃"}
  ]
}
```

调整卡的 `payload.mode` 为 `SINGLE_PLAN_ADJUST`，且 `items` 与 `plans` 必须各恰好一项；`BUNDLE_CREATE` 至少两项。客户端兼容展示历史 PlanCard 1.0；服务端只产生 1.1。未知版本或非法基数统一降级为只读状态，不渲染审批按钮。`SelectionCard` 提交计划选择后，服务端再次校验候选计划属于当前用户且仍为活动状态。

### 5.7 聊天错误码

| HTTP | 错误码 | 含义 |
| --- | --- | --- |
| `400` | `INVALID_STREAM_CURSOR` | `after` 与 `Last-Event-ID` 不一致或格式错误 |
| `401/403` | `STREAM_TICKET_INVALID` | 票据无效、过期或不属于当前 run |
| `409` | `CHAT_CONTEXT_ROTATING` | 仅内部诊断；正常请求应持久化排队并等待归档完成 |
| `409` | `CONVERSATION_BUSY` | 会话已有正在排队、执行或取消中的 run |
| `409` | `APPROVAL_REQUIRED` | 当前必须使用结构化审批接口 |
| `409` | `RUN_RETRY_REQUIRED` | 当前 run 等待重试或取消选择 |
| `409` | `RUN_ATTEMPT_CONFLICT` | 重试 attempt 已变化 |
| `409` | `APPROVAL_VERSION_CONFLICT` | 审批版本已变化或已终结 |
| `409` | `PLAN_REVISION_CONFLICT` | 审批关联计划的当前版本已变化；组合审批整体失败 |
| `409` | `IDEMPOTENCY_KEY_REUSED` | 同一消息幂等 ID 被用于不同请求内容 |
| `409` | `ATTACHMENT_NOT_READY` | 附件未验证、扫描失败或用途不符 |
| `410` | `STREAM_CURSOR_EXPIRED` | 所需事件已超过保留期 |

错误文案可以本地化，客户端只能按稳定错误码分支。

## 6. SSE 事件协议

### 6.1 统一信封

```text
id: 42
event: message.delta
data: {"schema_version":"1.0","run_id":"uuid","sequence":42,"created_at":"2026-08-06T12:00:00Z","payload":{"message_id":"uuid","delta":"下一步建议","attempt":1}}

```

`id` 与 `data.sequence` 必须相同。一个空行表示一帧结束。客户端必须按 `sequence` 去重，不能假定每个网络数据块恰好包含一帧。

### 6.2 事件类型

| 事件 | 是否持久化 | 用途 |
|---|---|---|
| `run.accepted` | 是 | 消息事务完成，run 已进入队列 |
| `run.status` | 是 | run 状态变化 |
| `progress` | 是 | 可展示的阶段进度，不含内部推理 |
| `message.start` | 是 | Assistant 占位消息开始输出 |
| `message.delta` | 是 | 文本增量 |
| `message.reset` | 是 | 重试时清空上一 attempt 的未完成文本 |
| `message.completed` | 是 | 最终文本和结构化内容已落库 |
| `interrupt` | 是 | 需要用户输入或审批 |
| `run.completed` | 是 | run 成功结束 |
| `run.cancelled` | 是 | run 已取消 |
| `error` | 是 | 可展示错误、是否可重试和错误码 |
| `heartbeat` | 否 | 保活，无事件 ID |

`progress.payload` 只允许预定义代码，例如：

- `SEARCHING_WEB`：正在检索网络
- `RETRIEVING_PRIVATE`：正在检索我的资料
- `ORGANIZING_CITATIONS`：正在整理引用
- `GENERATING_ANSWER`：正在生成回答

客户端根据代码本地化文案。不得将 LangGraph 节点原始 state、模型思维链、工具调用参数或数据库错误堆栈作为进度发送。

### 6.3 增量合并

逐 token 写 PostgreSQL 会造成过多写入。Worker 在内存中按以下任一条件合并 `message.delta`：

- 距上次写入约 100 ms
- 累积到 128 个字符
- 当前模型输出结束

合并只影响事件粒度，不影响最终内容。Worker 同时维护当前 attempt 的完整文本，完成时一次性更新 `messages.content`、`structured_content` 和 `status=COMPLETED`。

## 7. 端到端流程

### 7.1 新消息

1. 客户端生成全局唯一 `client_message_id`，先在本地显示发送中消息。
2. API 鉴权并校验 conversation、Agent 和附件归属。
3. API 开启事务并锁定 conversation，检查幂等记录和活动 run。
4. 写入 User 消息、Assistant 占位消息、`agent_runs` 和 `run.accepted` 事件。
5. 提交事务并返回 `202`；不能先返回再异步补写这些业务记录。
6. 客户端建立 SSE 连接。
7. Worker 领取 run，设置 lease，使用 `run.segment_id → segment.thread_id` 调用 LangGraph。
8. Worker 将结构化进度和 Response 节点 token 投影为事件。
9. 回复结束后，Worker 在一个事务内完成 Assistant 消息和 run，再写完成事件。
10. 客户端收到完成事件后，以服务端消息作为最终状态。

### 7.2 输入中断

1. LangGraph 使用 interrupt 返回结构化问题。
2. Worker 写 `interrupt` 事件，将 run 更新为 `AWAITING_INPUT`，当前 Assistant 消息写为 `COMPLETED`。
3. 用户通过正常消息接口提交回答。
4. API 识别等待态，创建新的 User/Assistant 消息，并将同一 run 设为 `QUEUED`、`pending_action=INPUT_RESUME`。
5. Worker 使用 `Command(resume=<validated_input>)` 从原 checkpoint 恢复。

### 7.3 审批中断

审批不能通过自由文本隐式通过：

1. Worker 写审批记录和带 `approval_id` 的 `interrupt` 事件。
2. 客户端展示带 `approval_version` 的“确认、拒绝、继续修改”按钮。
3. 审批接口使用 `approval_id + expected_approval_version` 保存唯一终态决定；组合计划的所有审批项整组处理。
4. 在同一事务创建展示用户决定的 `SYSTEM_EVENT` 和新的 Assistant 占位消息，将同一 run 设置为 `QUEUED`、`pending_action=APPROVAL_RESUME`。
5. Worker 使用新占位消息作为本轮输出目标；批准或拒绝后仍恢复同一 `run_id/thread_id`。
6. 所有创建任务、发送通知等副作用只允许在批准后的节点执行。
7. “继续修改”后的文字以审批 `EDIT.feedback` 提交，不作为普通 User 消息隐式批准。

### 7.4 断线与重连

1. 客户端保存最后完整处理的 `sequence`。
2. 事件连接断开后进行指数退避重连：1、2、5、10 秒，最大 30 秒，并增加随机抖动。
3. 重连携带 `Last-Event-ID` 或 `after`。
4. 服务端先补发数据库中更大的序列，再等待新事件。
5. 连续重连失败时调用 run 查询接口；run 已结束则刷新消息，仍在运行则继续重连。

客户端收到重复事件时按 `run_id + sequence` 丢弃。断线期间 Agent run 正常继续，不依赖 SSE 连接存活。

### 7.5 取消

取消接口按当前状态幂等处理：

- `RUNNING`：更新为 `CANCEL_REQUESTED` 并记录 `cancel_requested_at`，由当前或接管的 Worker 停止。
- `QUEUED`、`AWAITING_INPUT`、`AWAITING_APPROVAL`、`FAILED_RETRYABLE`：在锁定 run 后可直接进入 `CANCELLED`，同时取消未完成消息和待审批项。
- 已为终态：返回当前终态，不回退状态。

Worker 在以下边界检查取消：

- 领取任务前
- 每个 LangGraph 节点前后
- 可中止的长工具调用之间
- 流式输出循环中

模型或外部工具无法立即终止时，状态可以短暂保持 `CANCEL_REQUESTED`。确认停止后将当前 Assistant 消息改为 `CANCELLED`，run 改为 `CANCELLED` 并写事件。重复取消返回当前状态。

### 7.6 重试与服务重启

- `FAILED_RETRYABLE` 只允许从同一 LangGraph checkpoint 重试同一个 run，不创建新 run。
- 重试前 `attempt + 1`，设置 `pending_action=RETRY`。
- 重试复用本轮 `pending_response_message_id`：将 `FAILED` Assistant 消息重新置为 `PENDING`。如果上一 attempt 已输出部分内容，先写 `message.reset`，客户端清除未完成文本。
- Worker 使用 lease 和 heartbeat；进程退出且 lease 到期后，其他 Worker 可重新领取。
- 恢复前检查 checkpoint、幂等副作用记录和最终消息状态，避免重复创建任务或发送通知。

每次 `START`、输入恢复、审批恢复和重试都有稳定 `pending_action_key`。Worker 只有确认相同 action key 已写入 LangGraph checkpoint 后才能清空数据库 pending action；接管 Worker 先比较 action key 再决定继续 checkpoint 或提交命令。action key 只防止恢复命令丢失或重复，任务和通知仍必须使用各自的领域幂等键。

如果 Worker 在流式回复中退出，接管者先检查 checkpoint：已有最终 Response 时直接完成消息落库；只有部分增量时增加 attempt，写 `message.reset` 后从安全 checkpoint 重新输出，避免将两次生成结果拼接。

## 8. Worker 队列与并发

### 8.1 领取规则

Worker 使用数据库任务表轮询：

```sql
SELECT id
FROM agent_runs
WHERE status = 'QUEUED'
   OR (status IN ('RUNNING', 'CANCEL_REQUESTED') AND lease_expires_at < now())
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

领取后在同一事务内设置：

- `status=RUNNING`
- `worker_id`
- `lease_expires_at`
- `heartbeat_at`

Worker 周期性续租。业务处理不能持有领取事务的行锁。接管 `CANCEL_REQUESTED` 时只完成取消，不再调用模型；接管租约过期的 `RUNNING` 时先检查 checkpoint 和消息状态，再决定完成已有结果或开始新 attempt。

### 8.2 一个会话一个活动 run

数据库使用部分唯一索引保证同一 `conversation_id` 只有一个非终态 run。API 的会话行锁用于返回可理解的业务错误，唯一索引是最终并发防线。

### 8.3 事件序列

写持久化事件时先原子增加 `agent_runs.last_event_sequence`，使用新值作为事件 `sequence`，并在同一事务插入 `agent_run_events`。禁止使用进程内计数器。

## 9. LangGraph 流式投影

Worker 调用 LangGraph `astream`，但只向用户投影允许的内容：

- `stream_mode=messages`：只接受标记为 Response 节点的最终回答 token。
- `stream_mode=custom`：节点主动发出预定义进度代码。
- `stream_mode=updates`：仅供内部调试或指标，不直接发送给客户端。

通过节点名称或显式 tag 过滤流事件。Research、Planner 的原始 token 不展示，避免把未经过 Evidence Gate 的内容、Prompt 注入内容和内部推理提前泄露给用户。

Response 节点结束后再用最终结构化结果更新消息；客户端流式文本只是临时展示，最终以 `message.completed` 和消息查询结果为准。

实现轮廓：

```python
async for mode, chunk in graph.astream(
    graph_input_or_resume_command,
    config={"configurable": {"thread_id": segment.thread_id}},
    stream_mode=["messages", "custom"],
    version="v2",
):
    await cancel_guard(run_id)
    if mode == "messages" and is_public_response_chunk(chunk):
        await delta_buffer.append(chunk)
    elif mode == "custom" and is_allowed_progress(chunk):
        await event_repo.append_progress(run_id, chunk)
```

过滤必须使用服务端维护的节点/tag 白名单，不能采信模型在内容中自报的节点名称。

### 9.1 对话上下文构建

Context Builder 使用模型 tokenizer 计算：

```text
usable_context_tokens = model_context_window
                        - reserved_output_tokens
                        - tool_call_reserve
                        - safety_margin
```

`estimated_context_tokens` 是下一轮未做新压缩时，稳定 Prompt/工具、累计摘要、当前 segment 消息、业务事实、长期记忆和知识检索预算的预计总量。达到 70% 是软阈值，只预生成摘要；达到 85% 是硬阈值，在当前 run 终态后归档 segment 并轮换 thread。

每次 `START` 或恢复按以下优先级组装 `ContextBundleV1`：

1. 稳定系统规则、工具 Schema、当前用户消息和活动 run/审批状态。
2. 从 PostgreSQL 重新加载的最新业务事实。
3. 最新有效 `CUMULATIVE_HANDOFF` 和上一归档段最后 3 个完整轮次。
4. 当前 ACTIVE segment 中稳定排序的 `COMPLETED` 可展示消息。
5. 与本节点及当前输入相关的长期记忆。
6. 当前节点必要的知识检索结果。

超限时依次裁剪低相关知识片段、低排名记忆、已被摘要覆盖的最旧消息和非本节点中间结果；前两项和当前输入/业务事实不得裁剪。新 run 清空上一 run 的工作字段；输入、审批或重试恢复同一 run 时保留 checkpoint 并只注入已验证的新输入。

摘要使用“上一版摘要 + 新增消息”增量生成，目标不超过 1500 Token 或可用预算 15%。每项必须关联来源消息/segment；业务对象只保存 ID；已接受的长期事实只保存 `memory_ref`。摘要失败时保留上一发布版本，并以最后 3 轮消息降级，不伪造摘要。

### 9.2 自动归档与线程轮换

1. run 终态后重新估算 Token；70% 时创建或刷新 `INCREMENTAL` 摘要。
2. 85% 时在一个短事务中锁定 conversation，封存旧段的 `end_message_id`、将其置为 `ARCHIVING`，同时创建下一序号 ACTIVE segment/新 `thread_id` 并切换 `active_segment_id`。
3. 高优先级归档任务为旧段生成 `SEGMENT_FINAL`，再结合上一累计摘要发布新的 `CUMULATIVE_HANDOFF`，最后将旧段标为 `ARCHIVED`。
4. 归档期间到达的新消息绑定已经创建的新 ACTIVE segment 并正常入库排队；该 segment 的 Worker 必须等待前序分段交接成功或确定性降级完成后再调用模型。
5. 旧 thread checkpoint 保留 7 天后清理；消息和摘要不受影响。

前端始终按当前 conversation 的 `(created_at, id)` 分页，不展示 segment 边界，也不提供 segment 归档操作。

### 9.3 长期记忆生命周期

- 提取输入限当前消息及最近两个轮次；事实证据必须来自 USER 消息。允许 `PREFERENCE/HABIT`（180 天）和 `CONSTRAINT/BACKGROUND`（365 天），拒绝推断、临时状态、敏感信息和已有业务对象。
- 安全 Policy 通过后计算 `0.35×明确 + 0.25×稳定 + 0.20×后续用途 + 0.10×具体 + 0.10×重复确认`；自动阈值 0.75，明确“记住”阈值 0.60，但不能绕过安全规则。
- 检索合并规范化键精确命中与 pgvector top-20，按 `0.55×语义相关 + 0.20×重要性 + 0.15×时间新鲜度 + 0.10×类型匹配` 重排，最多注入 8 条/800 Token。Router 不检索记忆。
- 同键同值为 `REINFORCE`；同键新值为 `SUPERSEDE`；可同时成立的不同键为 `COMPLEMENT`；不能唯一判断时为 `AMBIGUOUS` 并追问。
- “忘掉一项/全部”“以后不要记住”“继续记住”“重新记住”由 Memory Command 处理。遗忘先写屏障使 Store、摘要 `memory_ref` 和历史召回立即失效，再物理清理；原聊天消息仍可由用户查看。
- 每用户最多 200 条活跃记忆；先清理过期和精确重复，再淘汰最低保留分的自动提取项。用户明确要求记住的内容在 TTL 内受保护。

## 10. uni-app 客户端实现

### 10.1 H5

- 使用 `EventSource(events_url + '?ticket=...&after=...')`。
- 在 `onmessage` 和具名事件监听器中统一解析信封。
- ticket 过期或鉴权失败时关闭旧连接，通过已登录 API 获取新 ticket 后重连。
- 页面离开可以关闭 EventSource，但不能调用取消 run。
- 页面返回时先查询 run 和消息，再决定是否重连。

### 10.2 微信小程序

- 使用 `uni.request` 并开启 `enableChunked: true`。
- 通过 `RequestTask.onChunkReceived` 接收 `ArrayBuffer` 数据块。
- 使用增量 UTF-8 解码器，避免中文字符被网络分块截断后乱码。
- 将文本追加到缓冲区，按空行拆分完整 SSE 帧；保留最后一个不完整片段等待下一块。
- 网络数据块、UTF-8 字符和 SSE 帧三者边界都可能不同，不能逐块直接 `JSON.parse`。

### 10.3 客户端状态

建议按 `message_id` 保存消息，并为当前 run 维护：

```ts
type ChatStreamState = {
  runId: string;
  assistantMessageId: string;
  lastSequence: number;
  attempt: number;
  connection: 'CONNECTING' | 'OPEN' | 'RECONNECTING' | 'CLOSED';
  runStatus: string;
  composerMode: 'MESSAGE' | 'APPROVAL_EDIT';
  editingApprovalId?: string;
  editingApprovalVersion?: number;
};
```

状态处理规则：

- `message.delta`：只追加 sequence 更新且 attempt 匹配的文本。
- `message.reset`：清空对应 Assistant 临时文本并更新 attempt。
- `message.completed`：用完整服务端内容覆盖临时文本。
- `interrupt`：展示输入框或审批卡片。
- 进入 `APPROVAL_EDIT` 后，提交键调用审批接口；成功或取消后恢复 `MESSAGE`，不得把反馈误发为普通消息。
- `error`：按 `retryable` 决定展示重试按钮。
- App 重启：本地状态只用于加速展示，必须从服务端重新校准。

## 11. 安全与隐私

- conversation、segment、消息、run、事件和记忆查询都按当前 `user_id` 校验，客户端不能指定内部 conversation/segment 或 Store namespace。
- 流票据不能写入业务日志、埋点、Referer 或错误上报；服务端访问日志隐藏票据。
- Markdown 使用白名单渲染，禁用原始 HTML、脚本和危险 URL scheme。
- 引用 URL 必须经过协议和域名安全校验；外链默认使用安全跳转策略。
- 附件下载使用短期签名 URL，不把私有对象键暴露给其他用户。
- 客户端只收到展示事件，不收到系统 Prompt、隐藏 state、工具密钥、数据库异常或模型原始思维链。
- `progress/error` 事件和 `agent_runs.input_summary` 在写库前脱敏。`message.delta` 必须保留用户最终可见文本才能续传，按聊天消息同等级保护、默认终态后 7 天清理，且不得写入普通应用日志。
- 限制单条消息长度、附件数量、并发 run 数和每用户请求频率。

## 12. 可观测性

每个日志和指标使用以下关联字段：

- `request_id`
- `trace_id`
- `user_id`（日志中使用不可逆标识）
- `conversation_id`
- `segment_id`
- `run_id`
- `message_id`
- `thread_id`
- `attempt`
- `worker_id`

核心指标：

- 消息提交成功率和幂等命中率
- 排队时长、首个进度时长、首 token 时长、总耗时
- run 各状态数量和状态停留时长
- SSE 在线连接数、重连率、补发事件数和推送延迟
- Worker lease 过期次数和恢复成功率
- 模型/工具错误率、Token、调用次数和主备切换率
- 取消响应时长和重复副作用拦截次数
- 当前分段 Token 占比、摘要压缩率、70% 软阈值任务数、85% 线程轮换数和归档等待时长
- 记忆准入/拒绝数、Recall@8、注入 Token、过期/遗忘误召回数和检索延迟

告警至少覆盖：

- `QUEUED` 长时间未领取
- `RUNNING` heartbeat 超时
- 大量 `CONVERSATION_BUSY` 或流重连
- 事件序列冲突、消息已完成但 run 未终态等一致性异常

## 13. 验收场景

### 13.1 基础流程

- 首次打开时获得 `CURRENT` 会话，发送消息并按顺序收到流式回复
- 刷新页面后消息和最终状态一致
- 历史消息跨归档 segment 游标翻页不重复、不遗漏，页面不展示分段边界
- 相同 `client_message_id` 重复提交只产生一条用户消息和一个处理动作

### 13.2 中断与恢复

- Agent 询问缺失资料，用户回答后恢复同一 `run_id/thread_id`
- 审批批准、拒绝和调整均只处理一次
- 未审批前不创建任务、不发送通知
- 组合审批任一计划版本冲突时，全部 revision 均不发布
- 用户发送文字“确认”收到 `APPROVAL_REQUIRED`，只有当前 PlanCard 按钮可批准
- 多计划调整先展示 SelectionCard，选择后只出现一个计划的调整卡
- “继续修改”后的文字作为 `EDIT.feedback` 提交，不创建普通消息审批路径

### 13.3 异常与恢复

- SSE 断开后按序补发，无重复文本
- API、Worker 分别重启后 run 可以从 checkpoint 恢复
- Worker lease 过期被重新领取时不重复副作用
- 同一 segment/thread 创建后续新 run 时不继承上一 run 的草稿、错误或待办动作
- 70% 软阈值只刷新摘要；85% 硬阈值在 run 终态后归档并轮换新 thread
- 归档期间到达的消息不丢失、不重复，等待交接摘要发布后在新 thread 执行
- 摘要失败时使用上一发布版本和最后 3 轮消息降级；清理旧 checkpoint 不影响历史消息
- 允许记忆可跨 segment 召回，推断/敏感/临时/业务事实不会写入
- 更正、TTL、容量清理和聊天遗忘后，旧值不会从 Store、摘要或历史召回复活
- 部分输出失败后重试，客户端先 reset 再显示新 attempt
- 取消后不再新增回复内容或业务副作用
- 事件过期后客户端通过消息与 run 查询恢复正确状态

### 13.4 安全

- 用户 A 不能读取用户 B 的会话、消息、事件或流票据
- 恶意 Markdown、Prompt 注入检索内容和工具错误不会作为可信指令执行或泄露
- 票据、Prompt、附件私有地址和敏感资料不出现在日志

## 14. 实现顺序

1. 建立多会话、segment、summary、messages、全局单 run 和 event 数据约束。
2. 实现 `/v1/conversations` 消息事务、会话切换约束和 Worker 领取/续租。
3. 实现 Response/进度事件投影、SSE 和客户端断线恢复。
4. 实现 Token 预算、增量摘要、自动归档、thread 轮换和 checkpoint 清理。
5. 实现 PostgresStore、记忆提取作业、安全 Policy、精确/向量检索和上下文注入。
6. 实现强化、替代、TTL、容量清理、聊天遗忘及防复活屏障。
7. 实现输入中断、审批恢复、取消和重试。
8. 增加重连、归档竞态、服务重启、记忆评测与跨用户隔离测试。
9. 接入指标、保留期清理和一致性巡检。

## 15. 官方实现参考

- [LangGraph Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangChain Long-term Memory](https://docs.langchain.com/oss/python/langchain/long-term-memory)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [FastAPI Server-Sent Events](https://fastapi.tiangolo.com/tutorial/server-sent-events/)
- [uni-app `uni.request` 与 `RequestTask.onChunkReceived`](https://uniapp.dcloud.net.cn/api/request/request.html)
