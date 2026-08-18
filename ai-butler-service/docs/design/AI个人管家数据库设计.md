# AI 个人管家数据库设计

## 1. 数据分层

数据库把确认前的交互事实与确认后的计划事实分开：

- 交互事实：`conversations`、`conversation_segments`、`messages`、`agent_runs`、run 事件、trace、模型审计。
- 计划事实：`goals`、`plans`、`plan_revisions`、`plan_stages`、`task_templates`、`tasks`、`claims`、`citations`、`notification_jobs`。

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
