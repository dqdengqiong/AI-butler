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
