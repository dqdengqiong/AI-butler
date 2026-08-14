# AI Butler Backend

AI个人管家的 FastAPI模块化单体、Agent Worker与 Scheduler Worker后端仓库。

## 快速开始

```bash
mise install
cp .env.example .env.local
make doctor
make bootstrap
make infra-up
make migrate
make seed
make dev-api
make dev-worker
make dev-scheduler
```

API默认监听 `http://127.0.0.1:8000`，OpenAPI文档位于 `/docs`。

默认 `SEARCH_PROVIDER=fake`，联网问答使用明确标记的合成来源，不产生外部费用。需要真实联网时，在 `.env.local` 设置 `SEARCH_PROVIDER=tavily` 和 `TAVILY_API_KEY`，再执行 `make stack-up`；Make 会通过 `docker compose --env-file .env.local` 传入搜索配置。服务端固定关闭 Tavily answer/raw content，并限制每轮查询数、结果数和超时。`OFFICIAL_SOURCE_DOMAINS` 仅用于服务端域名分级，不代表真实性审核。

## 常用命令

- `make doctor`：检查 Python、uv、Docker、Compose、端口和配置。
- `make dev-api`：启动 API热更新。
- `make dev-worker`：启动带数据库 lease/heartbeat 的 Agent Worker。
- `make dev-scheduler`：启动提醒、保留期与删除补偿 Scheduler Worker。
- `make stack-up`：构建并启动 API、两个 Worker、pgvector PostgreSQL 与 Qdrant，执行迁移、LangGraph setup 和合成种子导入。
- `make openapi`：重新生成 `openapi.json`。
- `make eval-smoke`：运行本地合成数据的 DeepEval 快速门禁。
- `make eval`：运行完整的确定性 Agent 合约与安全评测。
- `make eval-live`：使用配置的真实 Agent Runner 执行每题三次评测并保存报告。
- `make ci`：运行格式检查、lint、类型、测试、文档链接和密钥检查。

Agent 评测只使用本地合成数据。DeepEval 的 dotenv 自动加载、遥测和云端报告在命令中均被禁用；`make eval-live` 还要求通过 `EVAL_RUNNER_FACTORY=module.path:factory` 显式提供真实 Agent Runner，避免把合约自测误报为模型质量基线。

规范与架构入口见 [docs/README.md](docs/README.md)。
