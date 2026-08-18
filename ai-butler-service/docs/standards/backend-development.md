# 后端开发规范

## 架构边界

- FastAPI 保持模块化单体；API、Agent Worker、Scheduler Worker共享领域代码但独立启动。
- 依赖方向固定为 `api/worker -> application -> domain`，基础设施通过接口由 application 注入。
- Router 只负责鉴权、解析、调用应用服务和响应映射，不包含领域事务。
- Domain MUST NOT 依赖 FastAPI、SQLAlchemy、LangGraph 或具体模型供应商。

## Python

- 使用 Python 3.13，遵循 PEP 8，并由 Ruff统一格式与 lint。
- 公共函数、方法和属性 MUST 有类型；严格类型检查不得用无说明的 `Any` 逃逸。
- I/O 路径使用 async；CPU密集工作不得阻塞事件循环。
- Pydantic模型用于边界校验，领域实体和 ORM模型不得直接作为公共 API响应。

## 数据与事务

- PostgreSQL 是用户、会话、计划、任务和确认结果的业务事实来源。
- 事务边界由 application service控制；模型调用和网络等待期间禁止持有行锁。
- 迁移必须可在空库和已有库执行，包含索引、约束以及前滚补救说明。
- 并发领取使用短事务和 `FOR UPDATE SKIP LOCKED`；副作用必须具有业务幂等键。

## Agent

- LangGraph checkpoint是恢复状态，不代替 PostgreSQL业务事实。
- 模型节点只返回版本化结构，确定性代码负责权限、日期、引用和负荷校验。
- API请求协程不得直接执行长时间模型调用；Worker负责运行与恢复。
