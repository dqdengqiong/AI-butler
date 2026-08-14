# AI个人管家设计评审与优化建议 V2.4

## 1. 评审结论

当前技术方向总体可行，但 V1.0 文档主要描述了组件名称和理想流程，缺少可直接指导开发的接口、数据约束、失败分支、人工审批和安全边界。

V2.4 面向 10 人以内产品验证，采用以下基线：

- 后端使用模块化单体，不在验证阶段拆分微服务。
- Profile、Research、Planner 和 Executor 是一个 LangGraph 中的节点或子图，不是独立部署的自治 Agent。
- 每个用户只有一个永久主聊天；后台以 segment 自动归档和轮换 LangGraph thread，用户仍看到连续消息时间线。
- PostgreSQL 保存业务事实、完整消息和派生摘要；LangGraph Checkpointer 保存分段运行状态，PostgresStore + pgvector 保存受控长期记忆；Qdrant 只保存知识分块向量。
- 用户审批之前不得创建正式任务、发送通知或执行其他外部副作用。
- Redis、Celery 均为按需扩展项；验证阶段使用 PostgreSQL 作业表和单 Worker。
- 聊天使用“HTTP POST 命令 + SSE 事件流”；增量事件持久化到 PostgreSQL，断线后可按序列续传。
- `BUTLER` 大管家是唯一用户沟通入口，专业 Agent 仅作为后台领域流程参与计划工作项。
- 组合草案可一次原子批准多个独立计划；已有计划一次只允许调整一个。

## 2. 问题分级

### 2.1 P0：开发前必须解决

| 问题 | 风险 | V2.4 处理 |
| --- | --- | --- |
| 数据表缺少完整类型、主外键、约束和索引 | 无法可靠建表，容易产生孤儿数据和重复数据 | 为 MVP 表定义完整字段、约束、索引和时间类型 |
| `agent_id` 语义不明确 | Agent 定义和用户 Agent 实例可能关联错误 | 统一使用 `agent_definition_id`、`user_agent_id` |
| 缺少会话、消息和 LangGraph 线程映射 | AI 聊天与服务重启恢复无法落地 | 增加 `conversations`、`messages`、`agent_runs` |
| 多会话模型与单聊天框产品不一致 | 用户会看到无意义的创建/归档入口，长线程又会无界增长 | 每用户唯一 conversation，增加 segment、两级摘要、70%/85% 阈值和 thread 轮换 |
| 长期记忆缺少准入和遗忘边界 | 可能保存推断/敏感信息，或更正后旧值复活 | 模型只提候选；代码 Policy、TTL、tombstone、用户级串行和无明文审计控制生命周期 |
| 没有定义聊天传输、流式输出和断线恢复 | 客户端无法判断如何收取回复，网络中断可能重复或丢失文本 | 增加聊天专项设计、`agent_run_events`、SSE sequence、状态补偿和取消/重试接口 |
| 计划版本仅有数字，没有版本实体和审批记录 | 调整后无法判断当前有效计划，也无法审计用户确认 | 增加 `plans`、`plan_revisions`、`approval_decisions` |
| Agent 流程只有成功路径 | 工具失败、资料不足、用户拒绝后流程无定义 | 增加可恢复状态、重试、回退、编辑、拒绝和取消分支 |
| 用户确认只是流程文字 | 无法安全暂停与恢复，可能重复执行副作用 | 使用 LangGraph `interrupt`，恢复节点必须幂等 |
| Prompt 输出只是示例 JSON | 字段漂移会使下游执行错误 | 使用带版本的 JSON Schema/Pydantic 模型并进行运行时校验 |
| RAG 内容没有 Prompt 注入边界 | 恶意网页或用户文件可能控制 Agent 或工具 | 将检索内容标为不可信数据，限制工具权限并加入注入测试 |
| 日志直接保存输入输出且无保留策略 | 可能长期保存手机号、学习记录等隐私 | 默认脱敏，只保存必要调试字段并定义 30 天保留期 |
| 缺少租户过滤规则 | 可能发生跨用户数据泄露 | PostgreSQL 查询强制 `user_id`，Qdrant 强制 `tenant_id` 过滤 |

### 2.2 P1：产品验证阶段应解决

| 问题 | 影响 | V2.4 处理 |
| --- | --- | --- |
| 每项能力均设计为独立 Agent | 调用成本高、链路长、难测试 | 改为单图多节点，仅在达到明确扩展条件后拆分服务 |
| 仅展示来源但缺少约束 | 可能出现错引、伪造 URL 或跨用户资料 | 增加 claim-level citation 与确定性 Evidence Gate |
| 用户画像没有业务表 | Profile 采集结果只能存在 Prompt 或 checkpoint | 增加 `user_profiles` 和 `study_availability` |
| 计划直接展开每日任务 | 用户未确认时可能产生大量无效数据 | 计划草稿与正式任务分离，批准后才物化任务 |
| `task_record` 与学习记录职责重叠 | 统计口径不一致 | 合并为 `task_executions`，非任务学习后续再扩展 |
| 知识文档没有分块和版本元数据 | 无法定位引用、更新旧向量 | 增加 `knowledge_documents`、`knowledge_chunks` 和向量映射 |
| 文件保存公开 URL | 私有资料可能被绕过鉴权访问 | 保存对象存储 key，由后端签发短期访问 URL |
| 通知只有状态字段 | 失败重试可能重复发送 | 增加幂等键、尝试次数、渠道和发送结果 |
| 完整历史直接进入模型上下文 | Context-Rot、成本和延迟随时间增长 | 增加 Token 预算、增量/分段/累计摘要和节点级 ContextBundle |

### 2.3 P2：演进前优化

| 问题 | 优化方向 |
| --- | --- |
| API Gateway 在单体阶段没有明确收益 | 验证阶段由 FastAPI 统一提供 API；达到多服务条件后再引入网关 |
| Redis/Celery 被列为默认依赖 | 先用数据库作业队列；需要高吞吐或复杂调度时迁移 |
| 文档缺少可观测性目标 | 增加 trace、token、成本、延迟和失败率指标 |
| 文档缺少数据删除流程 | 明确 PostgreSQL、checkpoint、Qdrant、对象存储的级联清理 |
| Prompt 没有版本和评测 | 增加 `prompt_version`、基准集和发布门禁 |
| 公考知识没有时效策略 | 用 `published_at`、`valid_from`、`valid_to` 和 `retrieved_at` 控制检索 |

## 3. V2.4 架构决策

| 决策 | 结果 | 原因 | 重新评估条件 |
| --- | --- | --- | --- |
| 应用形态 | 模块化单体 | 10 人阶段优先验证价值和快速排障 | 团队独立部署、流量或故障域出现明确需求 |
| Agent 形态 | 单 LangGraph 多节点 | 共享状态、审批和恢复更简单 | 独立能力需要不同扩缩容或安全边界 |
| 业务数据库 | PostgreSQL | 保存用户、目标、计划、任务等事实 | 保持不变 |
| 会话运行状态 | LangGraph PostgreSQL Checkpointer | 支持线程恢复和人工中断 | 保持不变，可迁移托管服务 |
| 聊天形态 | 每用户一个主聊天 + 内部 segment | 符合单聊天框，同时限制每个 thread 的上下文和 checkpoint 生命周期 | 产品明确需要用户管理多个会话 |
| 上下文压缩 | PostgreSQL 两级不可变摘要 + Token 阈值 | 原消息可追溯，70% 提前压缩，85% 安全轮换 | 模型上下文或成本结构发生重大变化 |
| 长期记忆 | LangGraph PostgresStore + pgvector，业务代码 Policy | 复用现有 LangGraph，Store 与 Checkpointer 职责清晰，可精确更新/遗忘 | 关系型多跳记忆成为核心需求 |
| 专业知识 | Qdrant + PostgreSQL 元数据 | 兼顾向量检索和可审计来源 | 小规模时可评估 pgvector，但本轮保持 Qdrant |
| 异步任务 | PostgreSQL 作业表 + 单 Worker | 依赖少、容易维护 | 作业吞吐、延迟或调度复杂度超过单 Worker |
| 聊天传输 | POST 消息 + SSE | 符合服务端单向流式回复，HTTP 下易鉴权、重连和代理 | 出现语音通话或双向低延迟控制需求 |
| 通知 | Worker 调用渠道适配器 | 模型不直接执行副作用 | 保持不变 |

参考资料：

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangChain Long-term Memory](https://docs.langchain.com/oss/python/langchain/long-term-memory)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)
- [FastAPI Server-Sent Events](https://fastapi.tiangolo.com/tutorial/server-sent-events/)
- [uni-app RequestTask 分块响应](https://uniapp.dcloud.net.cn/api/request/request.html)
- [Qdrant Multitenancy](https://qdrant.tech/documentation/tutorials/multiple-partitions/)
- [OWASP LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [AgentGuide 上下文工程实践](https://github.com/adongwanai/AgentGuide/blob/main/docs/02-tech-stack/11-context-engineering-practices.md)
- [AgentGuide Agent Memory](https://github.com/adongwanai/AgentGuide/blob/main/docs/02-tech-stack/15-agent-memory.md)

## 4. 需求追踪矩阵

| 需求 | 页面/API | Agent 节点 | 核心数据 | 验收场景 |
| --- | --- | --- | --- | --- |
| 用户系统 | 登录、个人设置；`/v1/me` | 无 | `users`、`user_profiles` | 用户只能读取和修改自己的资料 |
| AI 管家入口 | 首页、唯一 AI 聊天；`/v1/chat`、SSE、run 控制 API | Context Builder、Router、Response | `conversations`、`conversation_segments`、`conversation_summaries`、`messages`、`agent_runs`、`agent_run_events` | 消息时间线连续；70% 只摘要、85% 归档换 thread；断线和归档竞态不丢消息 |
| 长期连续性 | 无管理页面；聊天更正/遗忘命令 | Memory Command/Extractor、Profile、Planner、Response | LangGraph Store、`memory_extraction_jobs`、`memory_tombstones`、`memory_policy_state` | 只写明确低风险事实；相关记忆跨 segment 召回；过期/遗忘不复活 |
| Agent 管理 | 大管家后台路由；用户 Agent API | Router | `agent_definitions`、`user_agents` | MVP 只路由到公考流程，未开放 Agent 不会创建计划 |
| 公考 Agent | 首页、计划、聊天 | Profile、Research、Planner | `goals`、`user_profiles` | 信息不足时只追问一个高优先级问题 |
| 资料检索 | 资料页；知识搜索 API | Research | `knowledge_documents`、`knowledge_chunks`、Qdrant | 事实包含可访问来源且无跨用户结果 |
| 规划生成 | 计划页；计划 API | Planner、Deterministic Plan Validation | `plans`、`plan_revisions`、`plan_stages` | 不可行目标返回调整建议，不伪造计划 |
| RAG 引用 | 回答正文与 SourceCard | Research、Evidence Gate | `claims`、`citations` | 错引、伪造 URL、时效事实无来源或跨用户资料会被拦截 |
| 用户确认 | 聊天计划卡；审批 API | Approval Interrupt | `approval_decisions`、`approval_decision_items` | 组合计划整组发布或整组回滚；只有结构化按钮可完成批准或拒绝 |
| 任务管理 | 今日任务、任务详情；任务 API | Executor | `tasks`、`task_executions` | 只有批准的 revision 能生成正式任务 |
| 提醒 | 通知设置 | Scheduler/Worker，不由 LLM 执行 | `notification_jobs` | 重试不会重复发送同一通知 |
| 成长记录 | 成长记录页 | Feedback、Adjust | `task_executions`、`plan_revisions` | 反馈可追溯到任务和有效计划版本 |

## 5. 跨文档统一词汇

| 统一词汇 | 定义 |
| --- | --- |
| Agent Definition | 系统提供的 Agent 类型模板，例如公考 Agent |
| User Agent | 某个用户启用的 Agent 实例 |
| Node | LangGraph 中执行单一职责的 Profile、Research 等能力 |
| Conversation | 用户唯一、永久且可见的主聊天；包含多个内部 segment |
| Conversation Segment | 不可见的上下文分段，与一个 LangGraph `thread_id` 一一对应 |
| Handoff Summary | 跨归档分段传递的累计派生上下文，不是业务事实 |
| Long-term Memory | 从用户明确陈述提取、跨 segment 使用并受 Policy/TTL/遗忘控制的语义记忆 |
| Run | 一次可暂停、恢复和重试的 Agent 业务执行；可因输入/审批中断跨越多轮消息 |
| Chat Event | run 面向客户端的持久化展示投影，按 run 内 sequence 有序续传 |
| Plan | 一个目标下的计划逻辑实体 |
| Plan Revision | 计划的一次不可变版本，只有批准版本可生成任务 |
| Claim | Agent 输出中需要事实来源支持的最小陈述 |
| Citation | Claim 与知识分块或外部来源的关联 |
| Plan Work Item | 组合请求中一个独立目标及其专业流程、草稿、审核和 revision 工作状态 |
| Approval Item | 一个审批请求与一个待发布 plan revision 的关系及其预期当前版本 |

## 6. 验收门槛

V2.4 文档达到以下条件后，才视为可进入开发：

- 每项 MVP 需求至少映射到一个页面/API、一个持久化位置和一个验收场景。
- 数据表不存在语义不明确的外键，所有用户数据都有可验证的归属路径。
- Agent 的每个暂停点、失败点和副作用都有恢复及幂等规则。
- 聊天具备消息提交幂等、流式事件续传、状态补偿、显式取消和同 checkpoint 重试规则。
- 下游系统只消费通过 Schema 校验的结构化结果，不解析自由文本执行操作。
- 无引用、来源冲突或已过期的重要事实不能通过审核。
- 用户未批准时不创建正式任务、不发送提醒。
- PostgreSQL、LangGraph、Qdrant 和对象存储都有删除与恢复规则。
- Prompt 基准集覆盖正常、边界、失败、安全和跨用户隔离场景。
- 组合计划审批任一项冲突时不发布任何 revision，也不创建任何任务。
- 已有计划调整必须解析出唯一 `target_plan_id`，且只能产生该计划的新 revision。
- 自由文本“确认”不能完成计划审批；审批必须携带 `approval_id` 和预期版本。
- 客户端不能创建或指定 conversation/segment；历史消息跨归档段连续分页。
- 70% 软阈值不轮换线程，85% 硬阈值只在 run 终态后归档；旧 checkpoint 清理不影响消息。
- 记忆安全测试中敏感信息错误写入为 0，Recall@8 不低于 90%，更正和遗忘场景通过率为 100%。
