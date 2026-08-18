# AI 个人管家接口设计

## 1. 普通消息

`POST /v1/messages` 接收自然语言、附件和会话路由参数。快捷输入与手工输入使用同一接口，不携带强制 intent。

响应的 `execution_mode` 只有 `START`。每次成功接收都会创建新 run；客户端通过事件流观察 run 直到终态。

## 2. 计划预览

计划创建与调整只使用 `POST /v1/messages`。Router 识别意图后，公共工具从当前消息和近期普通消息中提取完整要求；信息不足返回普通追问，完整时返回 `PlanPreviewCardV1`。不存在计划表单或独立预览创建 API。

## 3. 确认预览

`POST /v1/plan-previews/{message_id}/confirm`

请求头必须包含 `Idempotency-Key`，请求体包含 `preview_hash`。成功返回 `PlanConfirmationResponseV1`，包括 plan、revision、goal、task 和 notification 引用。

常见冲突：

- 预览不属于当前用户或不存在；
- 预览已过期、已被替代或已失效；
- hash 不匹配；
- 日期或负荷校验失败；
- 调整计划的基线版本已经变化。

所有冲突都在计划写入之前失败，或者随事务整体回滚。

## 4. 计划删除

`DELETE /v1/plans/{plan_id}` 必须携带 `Idempotency-Key`。接口软删除当前用户计划，取消未完成任务与提醒并停止滚动排期；成功和同请求重复删除均返回 `204`，不提供恢复接口。

## 5. 聊天卡片

- `PlanPreviewCardV1`：只读计划预览，可通过自然语言修改或确认。
- `SourceCard`：展示回答或预览的来源摘要。
- `StatusCard`：展示执行进度或终态错误。

客户端遇到未知卡片版本时只读降级，不推断写操作。

## 6. API 契约

`openapi.json` 是后端生成物，前端的 `schema.d.ts` 和 `contract-lock.json` 必须从它同步。接口和 schema 修改后必须运行 OpenAPI 检查及前端契约检查。
