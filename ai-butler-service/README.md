# AI Butler Backend

AI个人管家的 FastAPI模块化单体、Agent Worker与 Scheduler Worker后端仓库。

## 环境准备

首次在本机运行时执行：

```bash
mise install
cp .env.example .env.local
make doctor
make bootstrap
```

`make doctor` 会检查 Python、uv、Docker、Docker Compose、端口和本地配置；
`make bootstrap` 会按照锁文件安装开发依赖。后续请选择下面一种启动方式，不要同时运行
Docker 全栈和本机 API，以免占用相同端口。

## 启动方式一：Docker 全栈

需要快速运行完整后端时执行：

```bash
make stack-up
```

该命令会：

1. 构建后端镜像并启动 API、Agent Worker、Scheduler Worker、pgvector PostgreSQL 和 Qdrant；
2. 执行业务数据库迁移；
3. 初始化 LangGraph 数据库；
4. 导入本地合成种子数据。

API 默认监听 `http://127.0.0.1:8000`，OpenAPI 文档位于
`http://127.0.0.1:8000/docs`。

### 日常是否需要重复执行 `make stack-up`

不一定。根据当前状态选择：

| 场景 | 操作 |
| --- | --- |
| 服务仍在运行 | 无需重复启动 |
| 电脑重启，但原容器仍存在 | 启动 Docker Desktop 后通常会自动恢复；先检查容器状态 |
| 原容器存在，但处于停止状态 | 使用 `docker compose --env-file .env.local -f compose.dev.yml start` |
| 执行过 `make stack-down`，或容器不存在 | 执行 `make stack-up` |
| 后端代码、依赖、Dockerfile 或 Compose 配置有变化 | 执行 `make stack-up` 重新构建 |
| `.env.local` 中传入容器的配置有变化 | 执行 `make stack-up` 重新创建相关容器 |

检查完整后端的容器状态：

```bash
docker compose --env-file .env.local -f compose.dev.yml ps
```

停止并删除容器和 Compose 网络：

```bash
make stack-down
```

`make stack-down` 没有删除命名数据卷，PostgreSQL、Qdrant 和本地对象存储数据会保留。

> `make stack-up` 可以重复执行，但每次都会检查构建，并再次执行迁移、LangGraph 初始化和
> 种子导入，因此在服务已经正常运行且代码、依赖和配置均未变化时没有必要重复执行。

## 启动方式二：本机热更新开发

需要频繁修改 Python 代码时，只用 Docker 启动基础设施，再在本机启动三个独立进程：

```bash
make infra-up
make migrate
make seed
```

随后分别在三个终端中运行：

```bash
make dev-api
```

```bash
make dev-worker
```

```bash
make dev-scheduler
```

`make dev-api` 提供 API 热更新。Worker 和 Scheduler 修改代码后需要重新启动对应进程。
如果开发内容需要 Qdrant，将 `make infra-up` 替换为 `make infra-up-knowledge`。

本机开发结束后可执行：

```bash
make infra-down
```

## 启动前端

后端命令不会启动前端。需要 H5 客户端时，在同一工作区的前端仓库中另开终端：

```bash
cd ../ai-butler-app
pnpm dev:h5
```

前端首次运行前还需要按照其 `README.md` 安装依赖并创建 `.env.local`。

## 搜索配置

默认 `SEARCH_PROVIDER=fake`，联网问答使用明确标记的合成来源，不产生外部费用。需要真实联网时，在 `.env.local` 设置 `SEARCH_PROVIDER=tavily` 和 `TAVILY_API_KEY`，再执行 `make stack-up`；Make 会通过 `docker compose --env-file .env.local` 传入搜索配置。服务端固定关闭 Tavily answer/raw content，并限制每轮查询数、结果数和超时。`OFFICIAL_SOURCE_DOMAINS` 仅用于服务端域名分级，不代表真实性审核。

## 自动会话配置

客户端不提供手动新建会话，所有消息通过 `POST /v1/messages` 提交。`CONVERSATION_TOPIC_IDLE_SECONDS` 默认 `86400`，用于提高长时间无活动后的新话题倾向；`CONVERSATION_TOPIC_CONFIDENCE` 默认 `0.85`，低于阈值或语义模糊时要求用户确认。路由器不可用时延续当前话题，专业助理切换和历史续聊仍按确定性规则处理。

## 模型路由配置

本地开发和普通测试默认 `MODEL_ROUTING_ENABLED=false`，只使用确定性 Fake。真实调用由 `model-routing.toml` 固定路由；当前配置全部使用千问，且不设置备用模型。需要多模型容灾时，可为具体路由显式增加跨供应商 fallback；只在超时、连接失败、429 或 5xx 时切换。网关只初始化路由实际引用的模型，未引用的 provider 或模型不要求配置密钥。运行时不按价格、延迟或模型自评选模。新 run 固定为 `butler-graph-v3`/`butler-prompts-v3`，旧 v2 run 仍按创建时版本恢复。

启用真实模型时，在未提交到版本库的 `.env.local` 中设置：

```dotenv
MODEL_ROUTING_ENABLED=true
MODEL_ROUTING_FILE=./model-routing.toml
MODEL_API_KEYS={"qwen":"replace-me"}
```

`MODEL_SHADOW_MODE=true` 仅用于显式评测：存在备用模型时，主模型成功后还会调用备用模型并只记录调用元数据，不影响正式输出；真实会话应保持关闭。单供应商配置允许所有环境使用 `fallbacks=[]`；配置了多个启用的模型供应商时，生产与 staging 的每条路由必须恰好有一个跨供应商备用。配置不接受价格、`price_as_of` 或其他未定义字段。工作流调用用 `run_id` 关联审计并汇总实际模型和 Token；审计不保存 Prompt、用户原文、文件正文、工具原始输出、思维链或密钥。

## 常用命令

- `make doctor`：检查 Python、uv、Docker、Compose、端口和配置。
- `make bootstrap`：按照 `uv.lock` 安装开发依赖。
- `make infra-up`：仅启动 PostgreSQL，供本机进程开发使用。
- `make infra-up-knowledge`：启动 PostgreSQL 和 Qdrant。
- `make migrate`：升级业务数据库并初始化 LangGraph 数据库。
- `make seed`：导入本地合成种子数据。
- `make reset-users`：显式清理旧身份模型的用户、私有向量和本地对象；执行前必须完成备份确认。
- `make dev-api`：启动 API热更新。
- `make dev-worker`：启动带数据库 lease/heartbeat 的 Agent Worker。
- `make dev-scheduler`：启动提醒、保留期与删除补偿 Scheduler Worker。
- `make stack-up`：构建并启动 API、两个 Worker、pgvector PostgreSQL 与 Qdrant，执行迁移、LangGraph setup 和合成种子导入。
- `make stack-down`：停止并删除 Docker 全栈容器和网络，保留命名数据卷。
- `make openapi`：重新生成 `openapi.json`。
- `make eval-smoke`：运行本地合成数据的 DeepEval 快速门禁。
- `make eval`：运行完整的确定性 Agent 合约与安全评测。
- `make eval-live`：使用千问与豆包两个真实 Agent Runner 分别执行每题三次评测并保存报告。
- `make ci`：运行格式检查、lint、类型、测试、文档链接和密钥检查。

Agent 评测只使用本地合成数据。DeepEval 的 dotenv 自动加载、遥测和云端报告在命令中均被禁用；`make eval-live` 还要求通过 `EVAL_RUNNER_FACTORIES={"qwen_balanced":"module.path:qwen_factory","doubao_turbo":"module.path:doubao_factory"}` 显式提供两个真实 Agent Runner。命令会输出独立报告，避免把合约自测误报为模型质量基线。

规范与架构入口见 [docs/README.md](docs/README.md)。

模型："doubao_turbo"，"qwen""
