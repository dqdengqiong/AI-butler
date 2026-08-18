# AI 个人管家设计决策与实施台账 V3.0

## 1. 重新评估结论

本文不再把候选建议当作目标架构清单，而是记录最终决策、实现位置和发布门禁。验证环境数据可全部丢弃，因此 V3.0 采用全量重建，不提供旧数据库迁移、字段回填、兼容视图或数据恢复。

| 结论 | 决策 | 当前实现 |
| --- | --- | --- |
| 保留并作为上线门禁 | 用户隔离、消息/run/event 持久化、SSE 续传、不可变 revision、结构化审批、幂等副作用、失败恢复、模型 Schema、RAG 安全、引用溯源、隐私日志、通知幂等、跨存储删除、Prompt 版本与评测 | 已实现；以本文件第 5 节检查为准 |
| 调整后保留 | 自动路由多会话 + 会话内 segment；70%/85% 为可配置默认值；LangGraph 编排状态，确定性服务执行业务事务；只有可验证事实必须形成 Claim/Citation | 已实现 |
| 保留为架构约束 | 模块化单体、单 LangGraph 多节点、PostgreSQL 作业队列、Qdrant 仅保存知识向量、PostgresStore 保存长期记忆 | 已实现 |
| 不保留 | 每用户唯一永久 conversation | 已删除；每用户最多一个 `CURRENT`，可有多个 `ARCHIVED` 或挂起工作流会话 |
| 本期不引入 | 微服务、独立 API Gateway、Redis/Celery、MCP runtime | 未引入；均不是部署依赖 |

## 2. 已实现的 V3.0 基线

### 2.1 数据与环境

- Alembic 已压缩为单个 `0001_initial_schema.py`，空库一次创建最终外键、CHECK、部分唯一索引、幂等键、摘要来源、run 版本快照、记忆/通知/删除作业及 trace。
- `make verification-rebuild CONFIRM=DELETE_ALL_VERIFICATION_DATA` 只接受 `development/test`，停止 API/Worker 后删除 Compose 项目的 PostgreSQL、LangGraph、Qdrant 和对象卷，再按业务迁移、Checkpointer/Store、Qdrant payload index、合成种子数据的顺序重建。
- PostgreSQL 是业务事实源；checkpoint 只保存恢复游标，Qdrant payload 不作为授权事实。

### 2.2 Agent、上下文与记忆

- Worker 通过 `segment.thread_id` 运行单个 LangGraph，节点为 Initialize、Router、Profile、Research、Planner、Review、Evidence Gate、Approval、Executor、Feedback/Adjust、Response。
- START、INPUT_RESUME、APPROVAL_RESUME、RETRY 使用稳定 action key；只有 LangGraph `ainvoke` 成功保存 checkpoint 后才清除 pending action。
- 输入和审批使用 `interrupt`；恢复时从 PostgreSQL 重读所有者、run 状态、审批动作和版本，checkpoint 不替代业务授权。
- `ContextBundleV1` 区分系统事实、用户内容和外部不可信内容；Token 预算按固定优先级裁剪。
- 软/硬阈值默认 70%/85%，配置必须满足 `0 < soft < hard <= 0.95`。硬阈值只在 run 终态后生成 segment final/cumulative handoff 并轮换 thread。
- 长期记忆支持“记住、纠正、忘记、暂停/恢复记忆”，模型或异步提取只能生成候选；Policy 拒绝敏感内容，遗忘写 tombstone。偏好/习惯 TTL 180 天，约束/背景 365 天；检索上限为 8 条和 800 Token。

### 2.3 计划、审批、任务和通知

- 新卡统一输出 `PlanCard 1.1`；`BUNDLE_CREATE` 至少两项，`SINGLE_PLAN_ADJUST` 恰好一项。历史 1.0 仍可只读/审批展示，未知或结构错误的卡降级为只读状态。
- 一个 CREATE run 可产生多个独立 work item；多个活动计划的 ADJUST 未解析唯一 `target_plan_id` 时返回 SelectionCard。
- 每项生成不可变 revision，一个 approval 关联全部 item。批准时按 `plan_id` 排序加锁，任一 expected revision 冲突会回滚全部发布。
- EDIT 终结旧 approval，并为组合中的全部项目创建新 revision 和新 approval，不修改旧 revision。
- 批准提交后，Worker 在独立事务幂等物化未来七天任务；任务 key 和通知 key 均唯一。新 revision 取消用户时区当天及以后的旧 revision `TODO/DOING` 任务，终态任务和执行记录保留。
- 通知通过适配器发送，失败按 1、5、30 分钟重试，达到上限进入 `DEAD`。

### 2.4 RAG、安全、治理和可观测性

- Research 只输出带 evidence ref 的结构化片段；Evidence Gate 校验 URL、来源映射、引用集合及数字支持关系，Claim/Citation 保存不可变来源快照。
- Qdrant 查询强制 `tenant_id`/`document_id` 过滤，召回结果再由 PostgreSQL 校验用户、文件、文档和 chunk 归属。
- 用户内容、网页、附件和工具结果统一视为不可信数据；公共搜索查询会去除常见标识符并限制长度。
- 模型审计仅保存 provider/model、版本、耗时、Token、状态和错误分类，不保存 Prompt、用户原文、文件/工具正文或思维链。
- 事件保留 7 天，终态 run/trace 保留 30 天，记忆审计保留 90 天；非终态 run/event 不按时间删除。
- 账号删除作业按 CANCEL_WORK、CHECKPOINT、STORE、QDRANT、OBJECTS、BUSINESS 步骤执行。任一步失败时用户保持 `DELETING` 并可重试，成功后仅保留脱敏用户墓碑和作业结果。

## 3. 公共契约

- 路径保持现有消息、conversation、run、SSE、审批、计划和任务接口，不开放手动创建会话、segment、thread 或记忆管理 API。
- 客户端不得发送 `user_id`、`user_agent_id`、segment 或 thread。所有身份来自访问令牌，Qdrant 和 Store namespace 由服务端生成。
- `AgentStateV1` 与 run 固定 graph、prompt bundle、capability registry 版本和 fingerprint；等待中的 run 只能用创建时版本恢复。
- 审批必须携带 `approval_id` 与预期版本；自由文本不会产生批准副作用。
- OpenAPI 1.1.0 是前端生成类型与客户端的唯一来源。

## 4. 统一词汇

| 词汇 | 定义 |
| --- | --- |
| Conversation | 自动路由形成的用户话题容器；每用户最多一个 CURRENT，可保留多个 ARCHIVED |
| Conversation Segment | 会话内部上下文分段，与一个 LangGraph `thread_id` 一一对应 |
| Run | 可暂停、恢复和同 checkpoint 重试的一次 Agent 执行 |
| Plan Work Item | 组合请求中的一个独立目标、草稿和 revision |
| Approval Item | approval 与待发布 revision 及 expected current revision 的关系 |
| Long-term Memory | PostgresStore 中按用户隔离、受 Policy/TTL/tombstone 控制的稳定用户事实 |
| Knowledge Vector | Qdrant 中的知识 chunk 向量；授权事实仍在 PostgreSQL |

## 5. 发布门禁

- 后端：`make ci`、空库初始迁移、重复初始化、验证环境重置保护和完整基础设施集成测试。
- Agent：崩溃 lease 接管、输入/审批恢复、重试不重复副作用、segment 归档竞态和 SSE sequence 续传。
- 计划：组合批准整体回滚、单计划调整唯一目标、EDIT 新 approval、未来七天任务和通知幂等。
- 跨存储：账号删除每个步骤注入失败后续跑，最终 PostgreSQL、checkpoint、Store、Qdrant 和对象文件均无用户内容。
- 安全：Prompt Injection、伪造引用、SSRF、工具越权、跨租户向量、日志泄露和敏感记忆误写。
- 记忆：敏感错误写入为 0，更正/遗忘 100%，固定合成标注集 Recall@8 不低于 90%。
- 前端：PlanCard 1.0/1.1、组合审批、EDIT 新 approval、未知卡降级、SSE UTF-8 跨块和 sequence 去重；通过 `pnpm ci:check` 与 OpenAPI 同步检查。

## 6. 延后项及重新评估条件

| 延后项 | 重新评估条件 |
| --- | --- |
| 微服务/API Gateway | 出现独立部署、隔离故障域或不同扩缩容需求 |
| Redis/Celery | PostgreSQL 作业持续积压，单 Worker 无法满足已量化 SLO |
| MCP runtime | 存在必须跨进程共享且无法由进程内类型化能力满足的工具生态 |

相关详细约束见[系统详细设计](./AI个人管家系统详细设计.md)、[数据库设计](./AI个人管家数据库设计.md)、[接口设计](./AI个人管家接口设计.md)、[聊天系统设计](./AI个人管家聊天系统设计.md)和[Agent 设计](./AI个人管家Agent流程与Prompt设计.md)。
