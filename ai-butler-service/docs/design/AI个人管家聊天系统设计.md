# AI 个人管家聊天系统设计

## 1. 消息与 run

聊天采用严格的一问一答关系：

```text
User message 1 ── run 1 ── Assistant message 1
User message 2 ── run 2 ── Assistant message 2
```

每条新消息都重新执行 Router。澄清问题不挂起图；Assistant 发出问题后本轮成功结束，用户回答时创建下一轮。

## 2. 快捷输入

快捷标签只携带自然语言和 `SEND_MESSAGE` / `FILL_COMPOSER` 展示行为。考公“制定备考计划”会立即发送普通消息；客户端不得附加 intent、工具名或计划模式。

## 3. 卡片

`PlanPreviewCard` 是 Assistant 消息快照。修改动作聚焦输入框并预填自然语言提示；新预览生成后，旧预览只保留为不可确认的历史展示。确认按钮调用独立确认接口，并在请求期间禁用以防重复点击。

## 4. 并发与取消

同一用户同一时间只执行一个 Worker run。默认策略遇到正在执行的任务返回冲突；用户明确确认切换时可使用 `CANCEL_OTHER`。可重试失败不会接收后续输入，用户发送新消息时旧 run 被终结并创建新 run。

## 5. 展示投影

客户端只显示：

- `QUEUED`、`RUNNING`、`CANCEL_REQUESTED`：处理中；
- `FAILED_RETRYABLE`：可重试；
- 其他终态：已完成或明确失败。

预览状态属于消息卡片，不属于 run 或计划生命周期。

## 6. Segment、摘要与 Checkpoint

conversation 是用户可见会话，segment 是内部上下文和 Checkpoint 边界。每个 segment 创建唯一 `thread_id`；同一 segment 的所有 run 复用该 thread，图版本通过 `checkpoint_ns` 隔离。消息查询严格按当前 segment，不从整个 conversation 拉取旧原文。

ContextAssembler 的选择顺序是当前输入和业务事实、累计/当前段摘要、最近原始轮次、画像/长期记忆、外部证据。摘要采用结构化字段 `current_goal`、`confirmed_constraints`、`decisions`、`open_questions`、`recent_context` 和引用，不使用固定占位文本。达到 3K Token 更新增量摘要；达到 8K Token 生成段终与累计交接摘要，归档旧 segment 并建立新 thread。

## 7. 清空与删除

`context_policy=ARCHIVE_AND_START` 或文本“清空当前上下文”会终结当前 workflow、归档旧 conversation、删除其 Checkpoint，并建立新的 conversation/segment/thread。历史消息仍可查看，显式长期记忆和用户画像不随之删除。

删除已归档 conversation 后，记录立即对用户查询和 Agent 不可见，相关 Checkpoint 立即删除；只有该 conversation 作为来源的自动长期记忆进入删除补偿，显式保存的长期记忆保留。业务消息、摘要、workflow 和 working state 30 天后硬删除。
