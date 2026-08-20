# AI个人管家项目技术调研文档（V3）

> 设计基线说明：本文后半部分保留阶段性扩容调研，不代表当前依赖。当前完整版本采用模块化单体、FastAPI、单 LangGraph、PostgreSQL 作业队列和 Qdrant；不部署独立 API Gateway、Redis/Celery 或 MCP runtime。

## 一、服务器部署架构

推荐采用云服务器部署。

整体架构：

```text
用户端
  |
FastAPI 服务
  |
Agent 服务（LangGraph）
  |
---------------------------
|            |             |
业务 PostgreSQL   LangGraph PostgreSQL   Qdrant
业务/作业/控制面  Checkpointer/Store     知识向量
```

文件存储：对象存储（OSS/S3）。

## 二、产品验证阶段服务器资源规划（10 人以内）

目标：支持早期产品验证，仅面向 10 名以内种子用户。

此阶段重点不是高并发，而是验证：

- Agent 流程是否有效
- 用户是否愿意持续使用
- 产品交互是否合理

推荐采用单机部署。

云服务器：

- CPU：2 核
- 内存：4 GB–8 GB
- 硬盘：40 GB–80 GB SSD

部署服务：

- FastAPI
- LangGraph Agent 服务
- PostgreSQL
- Qdrant
- PostgreSQL 作业队列

文件存储：可使用云对象存储，或小规模本地存储。

预计成本：约 50–200 元/月。

说明：由于 AI 模型采用 API 调用方式，不需要 GPU 服务器。

## 三、MVP 阶段服务器资源规划（1,000 以内用户）

当产品验证完成，需要扩大测试范围。

目标：支持 100–1,000 名用户。

推荐配置：

- 应用服务器：4 核 CPU、8 GB–16 GB 内存
- 数据库：PostgreSQL 独立部署
- 向量数据库：Qdrant 独立部署
- 任务系统：默认继续使用 PostgreSQL；只有作业持续积压并影响量化 SLO 时才引入 Redis + Celery
- 文件：对象存储

预计成本：约 300–1,000 元/月。

## 四、Beta 阶段服务器资源规划（1 万级用户）

目标：支持规模化测试。

建议拆分服务：

1. 网关服务
2. API 服务集群
3. Agent 服务
4. PostgreSQL 数据库
5. Qdrant 向量数据库
6. 可选 Redis 任务队列（仅在 PostgreSQL 队列容量数据证明必要时）
7. 文件存储

增加：

- 负载均衡
- 自动扩容
- 数据备份
- 日志系统
- 监控系统

## 五、生产环境运维设计

需要建设：

1. 日志系统
   - 用户请求
   - Agent 执行流程
   - 错误信息
2. 监控系统
   - CPU
   - 内存
   - 服务状态
   - API 响应时间
3. 数据备份
   - PostgreSQL 备份
   - 用户文件备份
   - 知识库备份
4. 安全
   - HTTPS
   - API 鉴权
   - 用户数据隔离
   - 密钥安全管理

## 六、硬件要求

开发阶段无需 GPU。

原因：AI 能力通过云端大模型 API 提供。

开发机器：

- CPU 4 核以上
- 推荐 16 GB 内存
- SSD 硬盘

未来只有在以下场景才需要 GPU 服务器：

- 本地部署大模型
- 私有化交付
- 大规模推理

## 七、记忆存储选型结论

采用 LangGraph 原生三层混合方案，而不是 Redis、全部塞入 Store 或另建 Qdrant memory collection：

- Checkpointer 原生表达 thread-scoped Agent State、节点恢复和故障恢复；`thread_id` 绑定 conversation segment，默认保留 7 天。
- AsyncPostgresStore 原生提供 namespace/key、TTL 和可选 pgvector 语义检索；当前显式配置 1024 维 embedding、`statement` 字段和 cosine 距离，未配置 index 的普通 `asearch` 不视为语义检索。
- 业务 PostgreSQL 提供消息分页、workflow、乐观版本、权限、retention、control record、tombstone 和审计，是可见性权威源。Checkpoint 或 Store 都不能承担权限判断。
- Redis 会新增恢复、持久化、双写一致性和运维依赖，却不能替代业务数据库或跨 thread 语义 Store；当前吞吐与 SLO 没有证明其必要性。
- Qdrant 继续只保存知识文档向量，避免用户记忆治理与知识检索生命周期混合。

Store 与业务数据库不是同一事务边界，因此写入采用 PENDING→Store→ACTIVE CAS，遗忘采用先业务屏障、后物理删除。Scheduler 执行 TTL sweep、超时 PENDING、已删除/过期正文和孤儿补偿；API/Worker 不运行常驻 TTL sweeper。
