# 文档索引

## 项目文件目录说明

```text
ai-butler-backend/
├── src/ai_butler/          # 后端应用源码
│   ├── api/                # FastAPI 应用、依赖、Schema 和路由
│   ├── application/        # 用例编排、事务边界和业务流程
│   ├── domain/             # 领域错误及不依赖外部框架的领域规则
│   ├── agent/              # Agent 状态、上下文、能力与安全策略
│   ├── adapters/           # 微信、模型、联网搜索、Embedding、向量、通知和存储适配器
│   ├── infrastructure/     # 数据库连接等基础设施实现
│   ├── workers/            # Agent Worker 与 Scheduler Worker 入口和运行时
│   ├── evaluation/         # Agent 评测数据、执行器和确定性验证器
│   ├── scripts/            # 种子、LangGraph 初始化和评测命令实现
│   ├── config.py           # Pydantic Settings 统一配置入口
│   └── security.py         # JWT、Refresh Token、签名票据等安全工具
├── migrations/             # Alembic 环境及数据库版本迁移
│   └── versions/           # 按版本顺序执行的迁移脚本
├── tests/                  # 自动化测试
│   ├── unit/               # 领域、适配器和运行时单元测试
│   ├── integration/        # PostgreSQL 业务闭环集成测试
│   ├── smoke/              # OpenAPI 等快速冒烟测试
│   └── evals/              # Agent 合约、安全和质量评测
├── evals/tasks/            # 本地合成 Agent 评测数据集
├── infra/postgres/init/    # PostgreSQL 首次启动初始化脚本
├── scripts/                # 工程启动、检查和 OpenAPI 生成脚本
├── docs/                   # 开发规范、AI 指南和产品架构设计
├── Dockerfile              # API 和 Worker 共用的后端镜像
├── compose.dev.yml         # 本地完整验证环境编排
├── Makefile                # 启动、迁移、测试和质量检查快捷命令
├── pyproject.toml          # Python 项目、依赖及工具配置
├── uv.lock                 # 锁定的 Python 依赖版本
├── alembic.ini             # Alembic 配置
├── openapi.json            # 供前端生成类型客户端的 API 契约
├── .env.example            # 可提交的本地环境变量模板
├── README.md               # 项目快速开始与常用命令
├── CONTRIBUTING.md         # 贡献流程说明
└── AGENTS.md               # 仓库内 AI 协作约束
```

### 源码分层与调用方向

- `api` 负责 HTTP/SSE 协议转换、鉴权依赖和输入输出校验，不承载业务规则。
- `workers` 负责后台进程入口、作业领取、lease、heartbeat 和调度循环。
- `application` 统一编排业务用例、Repository 操作和事务，是 API 与 Worker 复用的应用层。
- `domain` 与 `agent` 保存领域规则、状态契约和安全策略，不依赖 FastAPI 路由。
- `adapters` 和 `infrastructure` 封装数据库、微信、SearchProvider、模型、Qdrant、通知及对象存储等外部能力。

主要调用方向为：`api/worker → application → domain/agent`；外部系统通过
`adapters/infrastructure` 接入。新增功能时应避免让领域层反向依赖 API 或具体供应商实现。

### 生成文件与运行时文件

- `openapi.json` 由 `make openapi` 生成，是前端契约同步的唯一输入，不应手工修改。
- `.env.local` 从 `.env.example` 复制后按本机环境填写，不提交真实密钥。
- `.venv/`、缓存目录、覆盖率报告和 `local-storage/` 属于本地运行产物，不是业务源码。

## 开发规范

- [通用开发规范](./standards/general-development.md)
- [代码注释规范](./standards/code-comments.md)
- [后端开发规范](./standards/backend-development.md)
- [API 与数据库规范](./standards/api-database.md)
- [测试与质量规范](./standards/testing-quality.md)

## AI 开发指南

- [AI 辅助研发指南](./ai/ai-assisted-development.md)
- [LLM 与 Agent 开发指南](./ai/llm-agent-development.md)

## 产品与架构设计

- [需求分析](./design/AI个人管家_需求分析.md)
- [系统详细设计](./design/AI个人管家系统详细设计.md)
- [数据库设计](./design/AI个人管家数据库设计.md)
- [接口设计](./design/AI个人管家接口设计.md)
- [聊天系统设计](./design/AI个人管家聊天系统设计.md)
- [Agent 流程与 Prompt 设计](./design/AI个人管家Agent流程与Prompt设计.md)
- [Agent 能力与 MCP 演进设计](./design/AI个人管家MCP设计.md)
- [技术调研](./design/AI个人管家_技术调研.md)
- [设计评审与优化建议](./design/AI个人管家_设计评审与优化建议.md)

发生冲突时，安全与隐私约束优先，其次是已评审的详细设计、仓库开发规范和局部实现说明。
