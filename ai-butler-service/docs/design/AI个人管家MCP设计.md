# AI个人管家 Agent 能力与 MCP 演进设计 V1.2

## 1. 文档状态

- 状态：MCP-ready 能力设计，MCP runtime 暂缓实施。
- 更新时间：2026-08-10。
- 当前适用范围：后端 Agent Worker、LangGraph 节点、应用服务和进程内能力门面。
- 未来适用范围：满足启用条件后的内部 Read/Action MCP Adapter。
- 非适用范围：外部 AI 客户端、第三方 MCP Host 和用户侧远程 MCP 接入。

当前仓库已经实现画像、自动多会话、计划、知识、审批、任务、长期记忆和治理服务；这些能力当前通过进程内类型化接口调用。MCP runtime 仍是延后项，本文的 MCP Server 部分只描述未来映射，不是当前部署依赖。

相关设计：

- [系统详细设计](./AI个人管家系统详细设计.md)
- [数据库设计](./AI个人管家数据库设计.md)
- [聊天系统设计](./AI个人管家聊天系统设计.md)
- [Agent 流程与 Prompt 设计](./AI个人管家Agent流程与Prompt设计.md)
- [LLM 与 Agent 开发指南](../ai/llm-agent-development.md)

参考资料：

- [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [AgentGuide MCP 协议详解](https://github.com/adongwanai/AgentGuide/blob/main/docs/02-tech-stack/14-mcp-protocol.md)
- [MCP Specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

## 2. 架构决策摘要

当前项目只有一个内部 Python/LangGraph Host，业务应用服务尚未落地。MCP 不进入 MVP 关键路径，也不能替代应用服务、普通函数调用、权限校验、事务或业务事实来源。

当前架构固定为：

```text
LangGraph
  -> AgentCapabilityFacade
  -> Application Service
  -> Domain / Repository Port
```

满足 MCP 启用条件后才增加协议适配层：

```text
LangGraph
  -> MCP Client
  -> MCP Adapter
  -> AgentCapabilityFacade
  -> Application Service
```

核心决策：

1. 先设计稳定的 Agent 能力，不从 MCP 工具目录反推业务接口。
2. MVP 默认不向模型开放工具；模型输出结构化查询计划，由代码节点确定性调用能力门面。
3. 只有 Agent Tool Eval 证明模型自主追加检索优于确定性执行时，才开放两项高层只读研究工具。
4. 写操作始终由确定性节点调用，不将 Action 能力定义绑定给模型。
5. MCP 只承担未来的标准化连接、权限隔离和跨进程调用，不成为业务状态或工作流引擎。

## 3. 目标与非目标

### 3.1 目标

1. 为 LangGraph 提供任务导向、强 Schema、可审计的应用能力。
2. 模型不得直接访问数据库、对象存储、Qdrant、网络或通知渠道。
3. 用户身份和节点权限由 Worker 注入，模型和能力参数不能指定 `user_id`。
4. 所有写操作遵守 PostgreSQL 事务、审批、乐观锁和业务幂等约束。
5. 能力结果按来源标记信任级别，外部内容不能改变系统指令或工具权限。
6. 使用真实多步骤任务评测能力名称、描述、参数、结果结构和调用策略。
7. 未来 MCP Adapter 直接复用当前业务 Schema、安全规则和评测集。

### 3.2 非目标

- 不为了“支持 MCP”提前安装 SDK、启动 stdio 子进程或维护协议注册表。
- 不把每个 repository、API endpoint 或底层 SDK 方法包装为工具。
- 不提供通用 SQL、任意 URL、shell、对象 key、原始 LLM 或 Embedding 工具。
- 不通过 MCP 完成用户审批决定；审批仍由结构化 HTTP API 接收。
- 不在进程内 facade 或未来 MCP Server 中维护跨调用业务会话状态。
- 不在首期提供 MCP Prompts。
- 不直接向外部客户端开放内部 MCP Server。

## 4. 设计原则

### 4.1 Capability-first 与 Function-first

实现顺序固定为：

```text
Domain/Application Function
  -> AgentCapabilityFacade
  -> LangGraph deterministic node
  -> optional MCP Resource/Tool Adapter
```

应用服务先通过普通 Python 接口完成事务、授权和单元测试。只有语义稳定、输入输出有明确 Schema，并满足 MCP 启用条件的能力才增加 MCP Adapter。Adapter 只做协议解析、调用上下文解析和结果映射，不实现领域事务。

### 4.2 围绕任务结果设计能力

- 优先实现少量、目的互斥、能完成高价值工作流的能力。
- 不直接暴露 `list_*`、`get_*`、`fetch_*` 等要求模型拼接大量低层调用的接口。
- 允许高层只读能力在内部完成检索、受控抓取、去重、排序和证据聚合。
- “合并频繁调用”只适用于模型可见的只读工具；高风险写操作仍按事务、一致性和恢复边界拆分。
- 新增能力必须由真实任务和评测失败证明必要性，不能只因为底层 API 存在。

### 4.3 确定性优先

Router、Context Builder、计划候选解析、画像写入、审批恢复、发布、任务物化和记忆写入都具有可确定的调用时机，应由代码节点调用。模型不需要在这些能力之间自主选择。

Research 首先输出结构化查询计划，代码节点调用检索 facade，Evidence Normalizer 再消费受控结果。只有评测证明需要模型自主迭代搜索时，才绑定候选模型工具。

### 4.4 高信号、低 Token 输出

- 返回完成当前任务所需的信息，不返回完整数据库实体或底层 Provider JSON。
- 优先使用标题、来源名称、业务状态和语义化引用；UUID 只在后续调用必须时返回。
- 大结果必须支持过滤、分页或截断，并给出可执行的下一步提示。
- 输出格式和默认预算由 Agent Tool Eval 决定，不照搬其他 Agent 的 Token 上限。

### 4.5 双重授权与不可信输出

Host 只允许节点调用静态白名单中的能力；facade 或未来 MCP Server 再校验 `graph_version + node_name + capability_name`。客户端过滤只改善模型选择，不能作为安全边界。

网页、用户附件、用户私有资料、检索片段和错误文本都是数据而非指令，必须与 System Prompt 分区，并携带信任级别和来源引用。

## 5. 分阶段运行架构

### 5.1 阶段一：当前 MVP

```text
Agent Worker / LangGraph
├─ deterministic nodes
├─ AgentCapabilityFacade
└─ Application Services
```

- 使用进程内、类型化 Python 调用。
- 节点白名单和调用上下文现在实施。
- 不安装 MCP SDK，不启动子进程，不执行 MCP 协议测试。
- 不依赖 `tools/list`、Resource Templates 或 MCP 进程内状态。

### 5.2 阶段二：内部 MCP

满足启用条件后，Worker 管理两个持久化 stdio 子进程：

```text
Agent Worker / LangGraph Host
├─ Read MCP Client ── stdio ── butler-mcp-read
└─ Action MCP Client ─ stdio ── butler-mcp-action
```

Read MCP：

- 提供工作流上下文 Resources 和只读研究能力。
- 使用 PostgreSQL、Qdrant 和对象存储只读权限。
- 仅官方来源适配器允许访问域名白名单内的公网。

Action MCP：

- 提供确定性写能力。
- 使用最小化业务写角色，禁止公网访问，不持有通知渠道凭据。
- 工具定义不绑定给模型，只由确定性 LangGraph 节点调用。

两个进程共用 application/domain 代码，但使用不同配置、凭据和注册表。stdout 只传输 MCP 协议数据，日志写 stderr。

### 5.3 阶段三：外部远程 MCP

对外开放时单独设计远程传输、OAuth、scope、Origin、防重放、速率限制、公开 Schema 和兼容周期。不得把内部 Read/Action Server 直接暴露给第三方 Host。

### 5.4 MCP 启用门槛

满足以下任一条件时，由后端架构负责人发起评估：

- 同一能力被两个以上独立 Host 或 Agent 使用。
- 出现跨语言或独立进程调用。
- 需要不同凭据、网络权限或故障隔离。
- 工具需要独立扩缩容。
- 需要向外部客户端开放。

评估必须记录触发条件、替代方案、运行成本、安全收益、回滚方式和负责人。未满足条件时，默认继续使用进程内 facade。若试运行 MCP 未产生预期复用或隔离收益，回退为进程内调用，但保留稳定能力 Schema。

## 6. AgentCapabilityCardV1

每项当前能力和未来 MCP 映射都必须关联版本化 `AgentCapabilityCardV1`：

```text
name
version
owner
purpose
exposure
use_when
do_not_use_when
agent_visible
allowed_nodes
risk_level
input_model
output_model
data_sources
trust_level
side_effects
idempotency
approval_precondition
timeout_ms
max_result_items
max_result_bytes
max_result_tokens
response_formats
examples
retry_policy
audit_fields
eval_suite_id
mcp_mapping
```

字段规则：

- `exposure` 只能是 `INTERNAL_ONLY`、`HOST_CONTEXT`、`MODEL_TOOL` 或 `DETERMINISTIC_ACTION`。
- `mcp_mapping` 当前默认为 `NONE`，未来只能是 `READ_MCP` 或 `ACTION_MCP`。
- `risk_level` 只能是 `LOW`、`MEDIUM` 或 `HIGH`。
- `allowed_nodes` 不得使用通配符。
- `agent_visible=true` 只允许只读能力，并且必须关联已通过的 `eval_suite_id`。
- `use_when` 和 `do_not_use_when` 必须说明调用条件及与相邻能力的边界。
- `response_formats` 对候选模型工具必须包含默认的 `concise`，可选 `detailed`。
- `examples` 至少包含一个正确示例和一个误用反例。
- `side_effects`、`idempotency` 和 `approval_precondition` 不得留空。
- 结果数量、字节和 Token 预算必须是有限正数，由能力评测决定。
- `audit_fields` 只能记录标识、状态、计数、耗时和脱敏哈希，不得包含正文。

能力卡以代码注册表作为未来唯一来源，并生成设计文档和测试快照。当前文档先固定字段和行为；业务实现落地时再建立代码注册表。工具描述属于 Agent Prompt 接口，必须与 Schema 一起版本化和评测。

## 7. 调用上下文与身份

当前 facade 和未来 MCP Client 都使用模型不可见的 `ToolCallContextV1`：

```text
run_id
node_name
graph_version
prompt_version
tool_call_id
deadline
capability_registry_version
registry_fingerprint
trace_id
span_id
parent_span_id
replay_mode
```

约束：

1. 上下文由 Worker 根据当前执行中的 run 生成，不进入模型工具参数 Schema。
2. 服务端使用 `run_id` 从 PostgreSQL 重新加载 `user_id`、run 状态、segment 和 pending action。
3. 不接受模型输出、普通参数或自定义元数据中的 `user_id` 作为身份事实。
4. `tool_call_id` 在同一节点重试时保持稳定，用于 trace 和幂等派生键。
5. 实际超时取能力卡超时与剩余 deadline 的较小值。
6. `capability_registry_version + registry_fingerprint` 必须与 run 固定的注册表快照一致。
7. `trace_id` 贯穿 run；调用方为权限检查和能力调用创建父子 span。
8. `replay_mode` 只能由受信 Worker 设置，普通执行为 `LIVE`；任何 replay 都强制只读。
9. 写能力额外校验节点类型、run 状态、审批事实和业务版本。
10. 实体 ID、Resource URI 和 evidence ref 都只是定位信息，不能代替授权。

未来通过 MCP 传递时，调用上下文放入供应商前缀的请求元数据，例如 `io.aibutler/runContext`，不得占用 MCP 保留字段。该元数据仍然只是定位和策略上下文，不是身份凭证。

## 8. 运行控制面

### 8.1 Agent loop 的位置

Agent loop 属于 LangGraph Host，不属于 MCP Server。运行时明确区分三类循环：

```text
Worker Run Loop
  -> 领取或恢复 run
  -> 调用 LangGraph
  -> 处理 interrupt、retry、cancel、complete

LangGraph Workflow Loop
  -> Planner 修复、审批编辑、节点重试等显式状态转换

Bounded Model Tool Loop
  -> 仅在评测启用后用于 Research
```

MVP 没有模型自主工具循环，继续使用：

```text
Query Plan
  -> AgentCapabilityFacade
  -> Evidence Normalize
```

候选模型工具通过评测后，Research 才可启用独立的 `BoundedToolLoop` 子图：

```text
Model Tool Decision
  -> Registry Resolve
  -> Permission Preflight
  -> Capability Execute
  -> Output Gate
  -> Checkpoint
  -> Continue / Stop
```

每个节点最多 2 个 tool round、2 次能力调用。获得充分证据、预算或 deadline 耗尽、参数哈希重复、不可重试错误、run 取消或下一轮无法满足上下文预算时立即停止。Research 只能调用 `butler_research_collect_evidence`；Action 能力永远不进入模型循环。

代码节点维护并写入 checkpoint 的 `ToolLoopStateV1`：

```text
node_name
work_item_id
round_index
call_count
seen_call_hashes[]
evidence_refs[]
remaining_result_tokens
last_tool_call_id
stop_reason
```

模型只能提出工具调用，不能修改 round、预算、调用哈希或停止原因。工具结果先通过 Output Gate，再以紧凑观察和 evidence refs 写入 checkpoint；原始 Provider 响应不进入 AgentState。

### 8.2 Tool Registry

内部 Tool Registry 不使用公共 MCP Registry，也不信任 Server 自报信息。每条注册项由三部分组成：

```text
CapabilitySpecV1
  name/version/title/description/input_schema/output_schema/examples

CapabilityPolicyV1
  exposure/risk/allowed_nodes/data_sources/network_policy
  side_effects/approval/idempotency/budgets/audit_fields

CapabilityBinding
  callable/implementation_version/server_profile
```

启动时编译不可变 `CapabilityRegistrySnapshotV1`：

```text
registry_version
registry_fingerprint
graph_compatibility[]
entries[]
build_id
```

规则：

- 注册表只从随应用发布的受信 Python 代码加载，不从 MCP Server、数据库或网络动态扩展。
- 重名、缺少 binding、Schema 不合法、节点通配符和模型可见写能力都导致 Worker 启动失败。
- fingerprint 是按能力名排序的 canonical JSON SHA-256，覆盖描述、Schema、权限、风险、预算和实现版本，不包含构建时间等易变字段。
- Graph manifest 固定 `registry_version + registry_fingerprint`，创建 run 时写入 `agent_runs`。
- 等待输入、审批或技术重试的 run 必须恢复原快照；缺少兼容快照时暂停领取并告警，不自动升级。
- Read/Action MCP 从同一快照生成按名称排序的稳定视图；`tools/list` 不按连接或节点动态变化。
- Host 只绑定节点白名单内的模型工具，facade 或 Server 在调用时再次使用同一快照校验。
- 不支持运行时热加载。旧快照保留到引用它的非终态 run 全部结束。

### 8.3 Permission Gate

所有能力调用固定经过：

```text
Registry Binding Gate
  -> Host Permission Preflight
  -> Facade / MCP Server Enforcement
  -> Domain Transaction Guard
  -> Output Gate
```

权限决策使用 `PermissionDecisionV1`：

```text
decision              # ALLOW / DENY
reason_code
policy_version
risk_level
effective_constraints
approval_fact_id
```

- Binding Gate 决定模型能看见哪些只读能力；未绑定的 Action 名称不能进入模型上下文。
- Host Gate 校验 graph/registry/node、参数 Schema、deadline、调用轮次、结果预算和取消状态。
- facade 或 MCP Server 根据 `run_id` 重新加载用户、run、实体归属和状态，不信任参数中的身份、节点或权限。
- Domain Guard 在同一业务事务内重新校验审批、期望版本、幂等键和锁定事实，防止检查后状态变化。
- Infrastructure Gate 通过只读数据库角色、网络策略和凭据拆分形成纵深防御。
- Output Gate 校验 output Schema、结果预算、信任标签、来源引用和禁止字段，再允许结果进入模型上下文。

风险策略：

| 风险 | 行为 |
| --- | --- |
| LOW 只读 | 节点和数据范围通过后自动允许 |
| MEDIUM 只读 | 增加租户、敏感类别、来源和结果预算校验 |
| MEDIUM 写入 | 仅确定性节点；要求明确用户证据和业务版本 |
| HIGH | 仅确定性 Action；重新读取持久化审批、版本和幂等事实 |

HIGH 操作不使用 MCP elicitation 重复审批，也不信任 checkpoint 中的审批副本。稳定拒绝码为 `CAPABILITY_NOT_ALLOWED`、`REGISTRY_MISMATCH`、`RUN_STATE_INVALID`、`APPROVAL_REQUIRED`、`APPROVAL_STALE`、`RISK_POLICY_DENIED`、`BUDGET_EXCEEDED` 和 `DEADLINE_EXCEEDED`。所有权失败与资源不存在使用相同安全表现。

### 8.4 Session state

MCP 没有协议级 session，不能依赖连接、Client 或 Server 进程内状态串联调用。逻辑 Agent session 是多个事实来源的组合：

| 状态 | 唯一事实来源 |
| --- | --- |
| run、pending action、版本和 trace | PostgreSQL `agent_runs` |
| 当前节点、tool loop、interrupt 和恢复状态 | LangGraph PostgreSQL Checkpointer |
| 消息、segment 和摘要 | PostgreSQL conversation 表 |
| 画像、计划、审批和任务 | 领域业务表 |
| 长期记忆 | LangGraph PostgreSQL Store |
| 知识与附件正文 | Qdrant / 对象存储 |
| 用户可续传事件 | `agent_run_events` |

不新增保存全部上下文的通用 session JSON 表。每次调用使用 `run_id` 重新装载所需投影；MCP 子进程重启或不同 run 复用连接不影响状态。游标、搜索句柄和 evidence ref 必须显式、不透明、有限期，并在每次使用时重新校验 run、用户和实体归属，不能视为 bearer capability。

### 8.5 Context compaction

上下文使用三级预算控制：

1. 单次能力结果由能力卡限制 items、bytes、tokens、分页和 `concise/detailed`。
2. 每次模型调用前由 Host 的 `ContextBudgetGuard` 校验 projected input 和预留 output。
3. run 终态后执行已有 conversation segment 的 70%/85% 维护策略。

预算必须包含 system/prompt、tool schemas、当前输入、业务事实、handoff、最近消息、长期记忆、检索证据、tool observations 和预留输出。检查在每次模型调用前、每次工具结果通过 Output Gate 后以及 run 终态后触发。

单次调用超限时按固定顺序：

1. 删除重复或低相关 evidence。
2. 删除低排名 memory。
3. 用 evidence refs 和短摘要替换已消费的 tool observations。
4. 删除已被发布摘要覆盖的旧消息。
5. 删除与当前节点无关的中间状态。

不得裁剪系统规则、当前用户输入、服务端身份、审批事实、当前业务版本和完成 Claim 验证必需的引用。仍无法满足预算时不得调用模型，返回 `CONTEXT_BUDGET_EXCEEDED`。MCP Server 只负责单次结果预算和截断提示，不负责压缩会话。

### 8.6 Trace 与 replay

内部 trace 使用以下 span 层级：

```text
agent.run
  └─ graph.node
      ├─ model.call
      ├─ tool.loop.round
      │   ├─ permission.check
      │   └─ capability.call
      └─ domain.transaction
```

`TraceSpanV1` 只包含脱敏控制面元数据：

```text
trace_id/span_id/parent_span_id
run_id/attempt/node_name/work_item_id
span_kind
capability_name/version
registry_fingerprint
risk_level/gate_decision
status/error_code/retry_count
input_hash/output_hash
trust_level/result_items/truncated
input_tokens/output_tokens
started_at/ended_at/duration_ms
```

span 写入 `agent_trace_spans`，默认保留 30 天。不得记录用户原文、Prompt、工具正文、附件、网页正文、完整 checkpoint 或模型思维链；审批和业务副作用审计继续使用领域表，不能依赖 trace。

回放模式：

- `STRUCTURAL_REPLAY`：生产可用，只根据 checkpoint、span 顺序、版本和哈希重建控制流，不调用模型或工具。
- `STUB_REPLAY`：仅合成数据环境，模型与能力返回录制 fixture。
- `LIVE_COMPARE`：仅合成数据环境，使用 Fake/sandbox adapter 重跑并比较结构化业务结果。
- 所有 replay 都强制 `REPLAY_READ_ONLY`；Permission Gate 拒绝真实写入、通知和公网访问。

`ReplayManifestV1` 记录 graph、prompt、model、registry、checkpoint、合成数据集版本、固定时间、随机种子和有序 span/call ID。模型结果只比较 Schema、业务结果和安全属性，不要求字节级一致。

## 9. 当前能力分类

| 能力 | 当前调用方式 | 模型可见 | 未来 MCP 映射 |
| --- | --- | --- | --- |
| Agent 定义、画像、可用时间、负荷和记忆召回 | Context Builder 确定性调用 | 否 | Read Resource |
| 计划候选解析 | Router 后处理确定性调用 | 否 | Read capability |
| 知识与官方来源检索 | 查询规划输出驱动 facade | 否 | Read Tool 候选 |
| 画像更新、审批草稿、发布、任务物化和记忆写入 | 确定性节点调用 application service | 否 | Action MCP |
| 审批决定 | HTTP API | 否 | 不映射 MCP |

`memory_recall`、`agent_definitions_list` 和 `plan_candidates_resolve` 是普通能力，不注册为模型工具。

官方搜索与抓取保留为 facade 内部服务函数。内部搜索结果句柄绑定当前 run、用户、规范化 URL、允许域名和过期时间；抓取函数只接受该句柄，并在每次重定向时重新校验目标。模型不能直接提供 URL 或句柄内容。

## 10. 候选模型工具

模型工具默认关闭。只有第 16 节的 Agent Tool Eval 证明自主追加检索优于确定性执行时，才把以下能力的 `exposure` 改为 `MODEL_TOOL` 并绑定到指定节点。

### 10.1 `butler_research_collect_evidence`

目的：供 Research 针对已确认目标收集可引用证据，不负责制定计划或形成最终事实判断。

输入语义：

```text
queries[]             # 1..3，每项包含 query、purpose、source_preference
freshness_requirement # 可选，业务语义而不是供应商过滤表达式
cursor                # 可选，不透明分页游标
response_format       # concise（默认）或 detailed
```

约束：

- 服务端完成公共/私有知识检索、官方域名搜索、受控抓取、去重、排序和结果预算。
- 不接受 URL、`user_id`、对象 key、SQL、任意过滤表达式或调用方指定租户。
- `concise` 返回证据摘要、语义化 `evidence_ref`、标题、来源名称、时效、信任级别和缺失证据。
- `detailed` 只增加后续 Citation 或调试所需的受控标识和元数据，不返回底层 Provider JSON。
- 截断时返回游标，并指导模型缩小查询或继续分页。

工具命名采用 `butler_<workflow>_<outcome>`。前缀、后缀或名称变更必须经过工具选择率评测，不能只按开发者偏好调整。

## 11. 结果与错误契约

不要求所有能力返回包含大量空字段的统一 `ToolResultV1<T>`。每项能力定义任务专用 data Schema，并组合紧凑的 `CapabilityResultMetaV1`：

```text
request_id
trust_level
provenance_refs[]
truncated
next_cursor
warnings[]
retry_hint
```

字段规则：

- `trust_level` 为 `SYSTEM_FACT`、`USER_CONTENT` 或 `EXTERNAL_UNTRUSTED`。
- `provenance_refs` 使用安全、语义化的引用；完整数据库 ID 只在确定性链式调用需要时出现。
- `next_cursor` 是服务端签名或编码的不透明游标。
- `retry_hint` 只包含安全、可执行的重试策略，不包含供应商原始错误。
- 不适用于某项能力的计数或分页字段不进入该能力的 data Schema。
- 模型工具默认返回 `concise`；`detailed` 必须由调用方明确请求。
- 大文档只返回摘要、相关片段、引用和计数，不返回完整原始 JSON。

参数错误必须告诉模型哪个字段错误、合法范围以及一个安全示例。授权、所有权和资源不存在使用同一安全错误表现，不能为了“可操作”泄露实体是否存在。未预期异常返回稳定内部错误，不返回异常消息或堆栈。

未来 MCP Adapter：

- 使用 JSON Schema 2020-12 定义 `inputSchema` 和 `outputSchema`。
- 业务结果放入 MCP `structuredContent`，并提供兼容的文本内容；不得用业务信封替代 MCP 结果层。
- 协议解析、方法不存在和参数 Schema 错误使用 MCP/JSON-RPC 协议错误。
- 已执行工具的预期业务失败使用结构化工具结果，并明确是否可重试和下一步。
- 遵守 2026-07-28 版本的结果类型、能力协商和错误码保留范围。

## 12. 未来 MCP Resources

未来 Resources 按工作流上下文聚合，不直接暴露数据库实体目录和低层分块：

```text
butler://runs/{run_id}/contexts/profile
butler://runs/{run_id}/contexts/planning/{plan_id}
butler://runs/{run_id}/contexts/review/{work_item_id}
butler://runs/{run_id}/contexts/execution/{revision_id}
butler://runs/{run_id}/evidence/{evidence_ref}
```

规则：

- Host 主动预取，不向模型提供自由遍历权限。
- `resources/list` 只返回模板，不枚举用户私有实体。
- 附件片段、知识分块和 Citation 在 facade 内聚合为上下文或 evidence。
- Resource 内容遵守节点投影、大小预算、信任标记和来源追踪。
- 私有资料只返回当前节点完成任务所需的最少字段。
- 不返回对象存储 key、私有永久 URL、系统 Prompt、checkpoint state 或数据库内部字段。
- 私有上下文使用私有缓存范围；动态 run 上下文默认不缓存或使用 `ttlMs=0`。

## 13. 未来 Action MCP 映射

以下名称只表示未来确定性 Action MCP Adapter 的映射，不是模型工具：

| 能力 | 风险 | 调用前提 | 幂等与事务 |
| --- | --- | --- | --- |
| `butler_profile_update_apply` | MEDIUM | 字段来自用户明确输入；携带 `expected_profile_version` | 版本匹配后单事务更新 |
| `butler_plan_stage_for_approval` | MEDIUM | Planner Schema 和 Deterministic Review 已通过 | 按 `run_id + interrupt_key` 幂等保存草稿和审批 |
| `butler_approval_status_get` | MEDIUM，只读 | 同一 run 正在执行 `APPROVAL_RESUME` | 只读 PostgreSQL 审批事实 |
| `butler_approved_revisions_publish` | HIGH | 审批为 `APPROVED`；全部期望版本匹配 | 稳定顺序锁定计划并原子发布全部 revisions |
| `butler_tasks_materialize_7d` | HIGH | revisions 已发布；任务草稿通过确定性校验 | 按 `plan_revision_id + task_key` 创建任务及通知作业 |
| `butler_memory_candidate_apply` | MEDIUM | Policy、证据、版本、敏感类别和遗忘屏障通过 | 使用稳定 memory key 幂等写入 |

审批边界：

- MCP 不提供批准、编辑或拒绝审批的工具。
- 用户决定通过 `/v1/approvals/{approval_id}/decisions` 接收并写入 PostgreSQL。
- 发布能力只接收 `approval_id`，不能接收模型提供的“已批准”布尔值、revision 列表或用户 ID。
- 发布时重新读取审批项、计划当前版本和用户归属，不能信任 checkpoint 副本。
- 组合审批任一项冲突时整体回滚。
- HIGH 能力继承已完成的结构化审批，不重复确认，但每次重试都重新校验审批事实。

通知边界：

`butler_tasks_materialize_7d` 可以在业务事务中创建幂等 `notification_jobs`，但不得调用通知 Provider。Scheduler Worker 使用自己的凭据发送；当前 facade 和未来 MCP 子进程都不持有通知渠道密钥。

## 14. 节点权限矩阵

| 节点 | 当前模型工具 | 当前确定性能力 | 未来 Host Resources / MCP |
| --- | --- | --- | --- |
| Router | 无 | 读取 Agent 定义；解析计划候选 | profile/plan 摘要或 Read capability |
| Profile | 无 | 读取上下文；应用已验证画像更新 | profile context；Action update |
| Context Builder | 无 | 召回受控记忆并构造节点投影 | workflow contexts |
| Research | 默认无 | 执行结构化查询计划 | 评测后可绑定 `butler_research_collect_evidence` |
| Planner | 无 | 读取已验证上下文 | planning context |
| Deterministic Review | 无 | 业务校验；保存待审批草稿 | Action stage |
| Approval Resume | 无 | 读取审批、发布已批准版本 | Action status/publish |
| Executor | 无 | 物化任务和通知作业 | execution context；Action materialize |
| Feedback | 无 | 读取任务和计划摘要 | planning/execution contexts |
| Memory Policy | 无 | 应用记忆候选或遗忘策略 | Action memory apply |
| Response | 无 | 读取已验证展示数据 | 无 |

任一节点新增模型工具时，必须同时更新能力卡、权限矩阵、安全测试、工具评测集和 Prompt 基准集。

## 15. 安全、供应链与恢复

### 15.1 身份与 Confused Deputy

- 身份只能从 PostgreSQL run 事实解析。
- 每个仓储查询显式携带服务端解析的 `user_id`。
- 跨用户访问和资源不存在使用同一错误表现。
- entity ID、evidence ref、Resource URI 和搜索句柄都不能代替授权。

### 15.2 Prompt Injection、SSRF 与文件访问

- 能力描述和 Schema 来自随应用发布的受信注册表，不从运行时远程加载。
- 网页、附件和检索内容携带信任级别并与系统指令分区。
- 工具输出中的“调用其他工具”或“忽略规则”只能作为内容处理。
- 官方来源搜索使用域名白名单和固定适配器；抓取只接受服务端短期句柄。
- 每次重定向都拒绝 loopback、link-local、私网地址和非 HTTP(S) scheme。
- 文件读取只接受 `stored_file_id` 或 attachment ID，不接受路径和对象 key。
- 文件必须属于当前用户，并且处于 `VERIFIED + CLEAN` 状态。

### 15.3 供应链与协议发现

当前阶段：

- 不新增 MCP SDK 或运行时依赖。
- 能力名称、描述、Schema 和能力卡变更必须评审并运行评测。

启用 MCP 后：

- 使用官方 MCP Python SDK，约束主版本并由 `uv.lock` 固定准确版本和哈希。
- 不在运行时安装、下载或连接任意第三方 MCP Server。
- Worker 使用固定 console script 和构建清单启动内部子进程。
- `clientInfo`、`serverInfo` 和 `tools/list` 是协议发现信息，不是安全身份。
- Worker 根据固定可执行文件、构建清单、能力注册表版本和指纹建立信任。
- `tools/list` 对每个 Read/Action Server 保持稳定；节点白名单由 Host 绑定，并在调用时由 Server 再校验。
- Tool annotations 只作为风险提示，不能代替权限、审批或运行时校验。

### 15.4 日志、失败与恢复

允许记录 request/run/tool call ID、节点、能力、风险、版本、耗时、数量、模型 Token 用量、状态、重试次数和脱敏参数哈希。禁止记录访问令牌、密钥、用户原文、附件或网页正文、完整检索片段、长期记忆值、系统 Prompt、checkpoint state 和模型思维链。

所有写能力在响应丢失或进程退出后都通过 PostgreSQL 状态和幂等键恢复。只读失败不能静默切换到越权的数据访问路径。未来子进程退出时，Worker 使用有上限的指数退避重启；stdout 污染、注册表不匹配或未知工具视为安全故障。

## 16. Agent Tool Eval 与发布门禁

### 16.1 评测集

复用现有 Prompt 基准，并增加真实、可验证的多步骤任务：

- 官方来源与知识库一致、冲突、过期和无结果。
- 初次检索不足，需要针对性追加检索。
- 宽泛查询导致截断，Agent 应缩小范围或使用游标。
- 两个候选工具的正确选择，以及不应调用工具的场景。
- 无效参数、过期游标、受控来源失败和可重试错误。
- 网页、附件和检索结果中的 Prompt Injection。
- 伪造用户、run、节点、URL、引用和跨租户访问。

任务使用与真实业务结构相同的合成数据。每项任务提供可验证业务结果；工具调用路径可作为观察指标，但不得把唯一调用顺序写死，因为可能存在多条正确路径。

### 16.2 指标

- 最终任务成功率和 Citation 支持正确率。
- 正确工具选择率、参数合法率和无工具场景误调用率。
- 工具调用次数、返回 Token、总耗时、错误率和截断恢复率。
- 越权、任意 URL、跨用户读取和未审批副作用次数。

生产环境不记录模型思维链。离线评测只保存结构化选择理由、调用轨迹、脱敏结果和明确反馈字段；业务正确性、安全和副作用使用确定性 verifier，模型 grader 只辅助判断语义质量。

### 16.3 发布门禁

- 新增模型工具在 held-out 任务上不得降低最终任务成功率。
- 新工具至少降低工具调用次数或返回 Token，或者解决确定性方案无法完成的真实场景。
- 安全用例必须零越权、零未审批副作用。
- 工具名称、描述、Schema、默认分页和结果格式变更均运行完整回归。
- 评测不通过时保持 `agent_visible=false`，继续使用确定性 facade。

## 17. 实施与验收

### 17.1 当前必须实施

1. 先完成业务表、repository port 和 application service。
2. 定义 `CapabilitySpecV1`、`CapabilityPolicyV1`、`AgentCapabilityCardV1`、`CapabilityRegistrySnapshotV1`、`PermissionDecisionV1`、`ToolCallContextV1`、`ToolLoopStateV1`、`CapabilityResultMetaV1`、`TraceSpanV1`、`ReplayManifestV1` 和能力专用 Schema。
3. 实现 `AgentCapabilityFacade`、不可变 registry、Permission/Output Gate、`ContextBudgetGuard`、节点静态白名单和确定性 LangGraph 集成。
4. 实现 Research 查询规划到 facade 的确定性检索流程。
5. 建立 Agent Tool Eval；候选模型工具保持关闭。
6. 在架构评审点检查 MCP 启用条件并记录决定。

当前测试必须覆盖：

- application service 的事务、所有权、版本冲突和幂等。
- facade 输入输出 Schema、预算、分页、来源和安全错误。
- 节点无法调用白名单外能力，模型无法传入身份、URL 或权限。
- 确定性 Research 能正确处理证据一致、冲突、过期、缺失和注入内容。
- Action 能力重复调用、响应丢失和恢复不产生重复副作用。
- 能力卡、节点矩阵和评测用例保持一致。
- registry 重名、Schema 漂移、指纹不匹配和缺少旧快照时失败关闭。
- 伪造 run/node、跨用户引用、过期审批和 HIGH Action 绕过被各层 Permission Gate 拒绝。
- 工具循环达到轮次、重复参数、取消或上下文预算时稳定停止，checkpoint 恢复不重复调用。
- 压缩顺序稳定，且身份、审批、当前输入、业务版本和必要 Citation 不被裁剪。
- trace 父子关系、哈希、重试和 gate 决策完整，敏感字段扫描通过。
- `STRUCTURAL_REPLAY` 不产生外部 I/O；合成 `STUB_REPLAY/LIVE_COMPARE` 可重建图路径，所有 Action 被只读模式拒绝。

### 17.2 MCP 启用后追加

1. 实现 Read Resources 和通过评测的只读工具 Adapter。
2. 实现 Action MCP 的确定性写 Adapter。
3. 实现双 stdio 子进程监督、版本协商、发现、退避重启和注册表校验。
4. 增加 MCP 协议、stdout、缓存、并发 run 隔离和子进程恢复测试。

追加验收：

- 模型客户端不能发现或调用 Action MCP 工具。
- Read MCP 数据库角色不能写入；Action MCP 不能访问公网或通知渠道。
- 并发 run 不会串用户、节点、上下文、游标或 evidence ref。
- 伪造 run、node、Resource URI、过期版本和注册表不匹配均被拒绝。
- 任一子进程在业务提交前后退出，恢复后不重复副作用。
- stdout 污染、敏感日志和未知能力作为 CI 阻断项。

## 18. 演进规则

- 首期不实现 MCP runtime，进程内类型化函数是默认方案。
- 候选模型工具必须先以 facade 能力完成应用服务测试，再做 Agent Tool Eval，最后决定是否暴露。
- 新增领域 Agent 时先扩展应用服务和节点权限矩阵，不复制通用数据库工具。
- 删除或重解释能力属于破坏性变更，必须提升能力卡版本，并为等待恢复的旧 run 保留兼容实现。
- MCP SDK 或协议升级只在 runtime 启用后适用，并必须更新锁文件、协议测试、构建指纹和回滚方案。
- 外部远程 MCP 是独立产品和安全边界，不继承内部 Server 的可见工具目录或凭据。
