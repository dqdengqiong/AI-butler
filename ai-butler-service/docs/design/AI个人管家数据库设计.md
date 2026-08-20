# AI 个人管家数据库设计

## 1. 数据分层

数据库把确认前的交互事实与确认后的计划事实分开：

- 交互事实：`conversations`、`conversation_segments`、`messages`、`agent_runs`、run 事件、trace、模型审计。
- 计划事实：`goals`、`plans`、`plan_revisions`、`plan_stages`、`task_templates`、`tasks`、`claims`、`citations`、`notification_jobs`。
- 可重建短期事实：`conversation_working_states`、`workflow_sessions`、`context_manifests` 和三类 `conversation_summaries`。
- 长期记忆控制面：`memory_control_records`、`memory_tombstones`、`memory_policy_state`、`memory_extraction_jobs`、`memory_audit_records` 和可丢弃的 `user_profile_snapshots`。

`PlanPreviewCard` 作为 Assistant 消息中的只读 JSON 快照保存，不单独创建计划草稿表。

## 2. 核心状态

### agent_runs

`status` 只允许：

- `QUEUED`
- `RUNNING`
- `SUCCEEDED`
- `FAILED_RETRYABLE`
- `FAILED_FINAL`
- `CANCEL_REQUESTED`
- `CANCELLED`

每个 run 固定保存 `trigger_message_id` 和 `pending_response_message_id`，分别指向本轮唯一的 User 与 Assistant 消息。活动 run 使用部分唯一索引限制同一会话并发。

### goals 与 plans

正式确认时直接创建目标和 `ACTIVE` 计划。项目没有计划草稿状态。软删除后状态为 `DELETED`，并保存删除时间和审计原因；业务查询默认排除已删除记录。

### plan_revisions

版本状态只有 `APPROVED` 和 `SUPERSEDED`。新建计划的首个版本直接批准；调整计划时锁定目标计划，校验预览的基线版本，再将旧版本标记为已被替代。

## 3. 预览快照

预览卡保存：

- `operation`、`target_plan_id` 和调整基线；
- 目标、日期、阶段、模板和未来七日任务；
- 来源摘要和可追溯引用；
- `preview_hash`、`generated_at`、`expires_at`；
- `READY`、`CONFIRMED`、`SUPERSEDED`、`DISMISSED` 或 `EXPIRED` 展示状态。

确认时对消息行加锁，并只接受属于当前用户、状态为 `READY`、未过期且哈希匹配的预览。

## 4. 确认事务

事务按以下顺序执行：

1. 锁定预览消息和目标计划。
2. 校验用户归属、哈希、日期、负荷及调整基线。
3. 创建或更新 goal、plan，并创建 `APPROVED` revision。
4. 创建阶段、任务模板、claims 和 citations。
5. 物化未来七日 tasks 与 notification jobs。
6. 将预览快照标记为 `CONFIRMED` 并保存确认结果引用。

任一步失败都会整体回滚。重复确认读取消息中已保存的首次确认结果，不重复创建业务对象。

## 5. 滚动排期与删除

`plan_schedule_watermarks` 保存当前 revision 已物化到的本地日期。确认时水位设为用户本地今天加 6 天（不超过计划结束日）；Scheduler 每日只补齐缺少的窗口末端日期。任务稳定键和数据库唯一约束保证并发轮询幂等，任务、通知与水位在同一事务提交。

删除计划时锁定 plan、goal 和水位，将计划标记为 `DELETED`，取消未完成任务及待处理通知并删除水位；revision、完成任务、执行记录、claims 和 citations 继续保留用于内部审计。

## 6. 初始结构

项目未上线，唯一的 `0001_initial_schema.py` 就是当前结构来源。开发和测试数据库从该结构重建，不提供旧 schema 数据迁移。

## 7. LangGraph 与业务表职责

LangGraph Checkpointer 和 Store 使用独立的 LangGraph PostgreSQL 数据库。Checkpointer 的 thread 是恢复缓存，不是消息数据库；Store 的正文和向量不是权限事实。业务数据库是会话、workflow、工作状态、记忆可见性和遗忘屏障的权威源。

`memory_control_records` 不保存正文，只保存 `store_key`、slot/statement hash、category、status、revision、source type、policy generation、TTL、来源 conversation 和 Store 清理时间。状态只允许 `PENDING`、`ACTIVE`、`CONFLICTED`、`DELETED`、`EXPIRED`。PENDING 永不参与检索。

`memory_tombstones` 区分 SLOT 与 USER 范围。`memory_policy_state` 保存自动提取开关、generation、`forget_before` 和画像快照状态。所有并发写入和遗忘先取得用户级 advisory lock，并通过 revision/generation CAS 防止旧作业复活记忆。

`conversation_working_states` 对 conversation 唯一，并带 segment、state version、目标、约束、决策、问题、workflow/summary/message 引用和 graph/prompt/tool/policy 版本。Checkpoint 不存在时由这些字段和消息/摘要重建。

`context_manifests` 按 run 和任务记录目标/硬预算、最终估算 Token、选中引用及是否发生裁剪，不保存 Prompt 或正文，用于验证 4K/8K/10K/12K 硬上限。
