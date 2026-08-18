# AI 个人管家系统详细设计

## 1. 设计原则

系统采用单轮、无状态的 Agent 执行模型。每条用户消息创建一个独立 run；run 只关联本轮一条 User 消息和一条 Assistant 消息，并在本轮内进入终态。澄清问题也是普通 Assistant 回复，用户的下一条消息重新经过意图路由。

计划业务事实只有在用户确认预览时创建。确认前可以保存聊天、run、事件、模型审计和只读预览快照，但不得写入 goal、plan、revision、task、claim、citation 或 notification。

## 2. 单轮执行

```text
Initialize → Router → Response → END
                    ↘ ToolExecutor → END
```

Router 输出结构化意图和上下文需求。应用代码按白名单选择能力，模型不能看到工具定义，也不能决定权限、业务写入或实体 ID。

支持的意图为：

- `GENERAL_CHAT`、`CIVIL_QA`、`CLARIFY`、`UNSUPPORTED`：直接回答。
- `DAILY_PLANNING`：读取当前计划和今日任务。
- `PLAN_REVIEW`、`TASK_FEEDBACK`：读取计划、任务和执行记录。
- `RESEARCH` 或 `PUBLIC_KNOWLEDGE`：公共检索。
- `PRIVATE_KNOWLEDGE`：用户资料检索。
- `PLAN_CREATE`、`PLAN_ADJUST`：调用公共计划要求收集工具。

## 3. 计划预览与确认

用户通过普通自然语言说明目标、考试类型、地区、年份、周期和时间安排。缺项时 Assistant 一次询问全部关键信息；下一条回复重新经过 Router。信息完整后 Worker 执行检索、Planner、确定性 Review 和未来七日排期，并保存 `PlanPreviewCard` 消息快照。确认前不创建计划业务记录。

修改操作只在输入框预填自然语言提示；新消息生成新 run 和新预览，旧预览改为 `SUPERSEDED`。

`POST /v1/plan-previews/{message_id}/confirm` 在一个数据库事务中校验归属、哈希、有效期、日期、负荷和调整基线，然后创建或更新目标、正式计划、已批准版本、阶段、模板、未来任务、来源和通知。失败全部回滚，重复请求通过 `Idempotency-Key` 返回首次结果。

确认时建立用户本地“今天至第 6 天”的排期水位。Scheduler 每日用确定性工具补齐窗口末端；删除计划时软删除计划并取消未完成任务、提醒和排期水位。

## 4. 状态与失败

run 状态只有 `QUEUED`、`RUNNING`、`SUCCEEDED`、`FAILED_RETRYABLE`、`FAILED_FINAL`、`CANCEL_REQUESTED`、`CANCELLED`。

Planner、检索或 Executor 失败只影响聊天 run，不留下计划草稿。用户发送新消息时会创建新 run；旧的可重试 run 会被终结，不作为后续输入上下文恢复。

## 5. 安全边界

- 所有实体查询和确认都校验当前用户归属。
- 私有检索按用户和文件访问控制过滤。
- 检索内容视为不可信数据，不能覆盖系统规则。
- 预览哈希由服务端对规范化快照计算。
- 确认事务内重新执行全部确定性校验，不能信任客户端展示数据。
