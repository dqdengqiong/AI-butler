# AI 个人管家系统详细设计

## 1. 设计原则

系统采用“单轮 run、segment-scoped 短期状态”的执行模型。每条用户消息创建独立 run 并在本轮进入终态，但同一 segment 的 run 通过同一个 LangGraph Checkpointer thread 共享精简工作状态。消息正文和业务 workflow 不复制进 Checkpoint。

计划业务事实只有在用户确认预览时创建。确认前可以保存聊天、run、事件、模型审计和只读预览快照，但不得写入 goal、plan、revision、task、claim、citation 或 notification。

## 2. 单轮执行

```text
Orchestrator: Initialize → Router
  ├─ GeneralResponse → END
  ├─ Research → END
  ├─ Planning → END
  ├─ TaskCoach → END
  └─ Memory → END
```

Router 输出结构化意图和上下文需求。应用代码按白名单选择能力，模型不能看到工具定义，也不能决定权限、业务写入或实体 ID。

支持的意图为：

- `GENERAL_CHAT`、`CIVIL_QA`、`CLARIFY`、`UNSUPPORTED`：直接回答。
- `DAILY_PLANNING`：读取当前计划和今日任务。
- `PLAN_REVIEW`、`TASK_FEEDBACK`：读取计划、任务和执行记录。
- `RESEARCH` 或 `PUBLIC_KNOWLEDGE`：公共检索。
- `PRIVATE_KNOWLEDGE`：用户资料检索。
- `PLAN_CREATE`、`PLAN_ADJUST`：调用公共计划要求收集工具。

Router 不读取长期记忆。只有路由后的能力节点通过 Runtime 注入的 Store 按需检索；专业节点只能使用当前认证用户的 namespace。Research 的开放式工具循环最多两轮，其他能力不允许开放式循环。

## 3. 计划预览与确认

用户通过普通自然语言说明目标、考试类型、地区、年份、周期和时间安排。缺项时 Assistant 一次询问全部关键信息；下一条回复重新经过 Router。信息完整后 Worker 执行检索、Planner、确定性 Review 和未来七日排期，并保存 `PlanPreviewCard` 消息快照。时间提取模型只生成重复规则和例外；服务端将其展开为连续七日本地日期容量，仅有周总量时按有效星期精确均分。预览展示原始可投入时间，任务排期再应用 85% 安全负荷。确认前不创建计划业务记录。

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

## 6. 三层记忆边界

```text
业务 PostgreSQL
  ├─ messages / conversations / segments / summaries
  ├─ conversation_working_states / workflow_sessions
  └─ memory control / tombstone / audit / profile snapshot

LangGraph PostgreSQL
  ├─ Checkpointer：segment-scoped ShortTermStateV2
  └─ Store：跨 thread 长期记忆正文、metadata、embedding、TTL
```

`thread_id=conversation_segment.thread_id`，`checkpoint_ns=graph_version`。ShortTermStateV2 只保存当前目标、已确认约束、决策、未决问题、workflow/摘要/消息引用、最后节点和版本；不保存完整聊天、附件、证据、计划快照、长期记忆全集或模型思维链。业务 working state 版本不一致时，以业务 PostgreSQL hydrate 后覆盖 Checkpoint。

Store namespace 固定为 `("users", user_id, "long_term_memory")`，key 为稳定 `slot_key` 的 SHA-256。仅 `statement` 进入 1024 维 cosine pgvector 索引。写入采用业务 `PENDING`、Store put、业务 ACTIVE CAS 三阶段协议；检索必须同时校验 ACTIVE、revision、policy generation、TTL 与 tombstone。

## 7. 遗忘与恢复

- run 结束清除临时 intent；workflow 等待输入 7 天过期并清空临时 slots。
- segment 在 3K Token 生成增量结构化摘要，在 8K Token 生成段终和累计摘要并轮换 thread。
- Checkpoint 保留 7 天；已删除 conversation 立即不可见并删除 thread，30 天后硬删除业务记录。
- 自动偏好/习惯 TTL 180 天，自动背景/约束 365 天；读取不刷新，重新表达才刷新。显式记忆默认不自动过期。
- 单条或全部遗忘先提交控制面删除、tombstone、generation 和画像 STALE，再尝试删除 Store；Scheduler 补偿 PENDING、孤儿、过期和版本不一致项。
