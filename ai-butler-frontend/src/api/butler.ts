import type { components } from './generated/schema'
import { request } from './client'

type LoginRequest = components['schemas']['WechatLoginRequest']
type RefreshRequest = components['schemas']['RefreshRequest']
type LogoutRequest = components['schemas']['LogoutRequest']
type SendMessageRequest = components['schemas']['SendMessageRequest']
type CreateConversationRequest = components['schemas']['CreateConversationRequest']
type ApprovalRequest = components['schemas']['ApprovalDecisionRequest']
type TaskExecutionRequest = components['schemas']['TaskExecutionRequest']
type PreferencesRequest = components['schemas']['PreferencesRequest']
type UploadIntentRequest = components['schemas']['UploadIntentRequest']
type CompleteUploadRequest = components['schemas']['CompleteUploadRequest']
export type CitationResponse = components['schemas']['CitationResponseV1']
export type AgentDefinitionListResponse = components['schemas']['AgentDefinitionListResponse']
export type AgentDefinitionResponse = components['schemas']['AgentDefinitionResponse']
export type ConversationResponse = components['schemas']['ConversationResponse']
export type ConversationListResponse = components['schemas']['ConversationListResponse']
export type MessageListResponse = components['schemas']['MessageListResponse']
export type MessageResponse = components['schemas']['MessageResponse']
export type SendMessageResponse = components['schemas']['SendMessageResponse']

export type ApiObject = Record<string, unknown>

function idempotencyKey(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

export const butlerApi = {
  login(payload: LoginRequest): Promise<ApiObject> {
    return request({
      path: '/v1/auth/wechat/login',
      method: 'POST',
      data: payload,
      headers: { 'Idempotency-Key': idempotencyKey('login') },
    })
  },
  refresh(payload: RefreshRequest): Promise<ApiObject> {
    return request({ path: '/v1/auth/refresh', method: 'POST', data: payload })
  },
  logout(payload: LogoutRequest, accessToken: string): Promise<void> {
    return request({ path: '/v1/auth/logout', method: 'POST', data: payload, accessToken })
  },
  me(accessToken: string): Promise<ApiObject> {
    return request({ path: '/v1/me', accessToken })
  },
  dashboard(accessToken: string): Promise<ApiObject> {
    return request({ path: '/v1/dashboard', accessToken })
  },
  plans(accessToken: string): Promise<ApiObject> {
    return request({ path: '/v1/plans', accessToken })
  },
  revisions(planId: string, accessToken: string): Promise<ApiObject> {
    return request({ path: `/v1/plans/${encodeURIComponent(planId)}/revisions`, accessToken })
  },
  tasks(accessToken: string): Promise<ApiObject> {
    return request({ path: '/v1/tasks', accessToken })
  },
  executeTask(
    taskId: string,
    payload: TaskExecutionRequest,
    accessToken: string,
  ): Promise<ApiObject> {
    return request({
      path: `/v1/tasks/${encodeURIComponent(taskId)}/executions`,
      method: 'POST',
      data: payload,
      accessToken,
    })
  },
  agentDefinitions(accessToken: string): Promise<AgentDefinitionListResponse> {
    return request({ path: '/v1/agent-definitions', accessToken })
  },
  conversations(accessToken: string, cursor?: string): Promise<ConversationListResponse> {
    const query = cursor ? `?limit=50&cursor=${encodeURIComponent(cursor)}` : '?limit=50'
    return request({ path: `/v1/conversations${query}`, accessToken })
  },
  createConversation(
    payload: CreateConversationRequest,
    accessToken: string,
  ): Promise<ConversationResponse> {
    return request({ path: '/v1/conversations', method: 'POST', data: payload, accessToken })
  },
  conversation(conversationId: string, accessToken: string): Promise<ConversationResponse> {
    return request({
      path: `/v1/conversations/${encodeURIComponent(conversationId)}`,
      accessToken,
    })
  },
  messages(
    conversationId: string,
    accessToken: string,
    cursor?: string,
  ): Promise<MessageListResponse> {
    const query = cursor ? `?limit=50&cursor=${encodeURIComponent(cursor)}` : '?limit=50'
    return request({
      path: `/v1/conversations/${encodeURIComponent(conversationId)}/messages${query}`,
      accessToken,
    })
  },
  sendMessage(
    conversationId: string,
    payload: SendMessageRequest,
    accessToken: string,
  ): Promise<SendMessageResponse> {
    return request({
      path: `/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
      method: 'POST',
      data: payload,
      accessToken,
    })
  },
  run(runId: string, accessToken: string): Promise<ApiObject> {
    return request({ path: `/v1/agent-runs/${encodeURIComponent(runId)}`, accessToken })
  },
  streamTicket(runId: string, accessToken: string): Promise<ApiObject> {
    return request({
      path: `/v1/agent-runs/${encodeURIComponent(runId)}/stream-ticket`,
      method: 'POST',
      accessToken,
    })
  },
  approve(approvalId: string, payload: ApprovalRequest, accessToken: string): Promise<ApiObject> {
    return request({
      path: `/v1/approvals/${encodeURIComponent(approvalId)}/decisions`,
      method: 'POST',
      data: payload,
      accessToken,
    })
  },
  preferences(accessToken: string): Promise<ApiObject> {
    return request({ path: '/v1/me/preferences', accessToken })
  },
  updatePreferences(payload: PreferencesRequest, accessToken: string): Promise<ApiObject> {
    return request({ path: '/v1/me/preferences', method: 'PATCH', data: payload, accessToken })
  },
  deleteAccount(accessToken: string): Promise<ApiObject> {
    return request({
      path: '/v1/me',
      method: 'DELETE',
      data: { schema_version: '1.0', confirmation: 'DELETE_MY_ACCOUNT' },
      accessToken,
      headers: { 'Idempotency-Key': idempotencyKey('delete-account') },
    })
  },
  files(accessToken: string): Promise<ApiObject> {
    return request({ path: '/v1/files', accessToken })
  },
  createUpload(payload: UploadIntentRequest, accessToken: string): Promise<ApiObject> {
    return request({
      path: '/v1/files/upload-intents',
      method: 'POST',
      data: payload,
      accessToken,
      headers: { 'Idempotency-Key': idempotencyKey('upload') },
    })
  },
  completeUpload(fileId: string, payload: CompleteUploadRequest, accessToken: string) {
    return request<ApiObject>({
      path: `/v1/files/${encodeURIComponent(fileId)}/complete`,
      method: 'POST',
      data: payload,
      accessToken,
      headers: { 'Idempotency-Key': idempotencyKey('upload-complete') },
    })
  },
  putUpload(url: string, headers: Record<string, string>, bytes: Uint8Array): Promise<void> {
    return new Promise((resolve, reject) => {
      uni.request({
        url,
        method: 'PUT',
        header: headers,
        data: bytes.buffer,
        success(response) {
          if (response.statusCode >= 200 && response.statusCode < 300) resolve()
          else reject(new Error(`文件上传失败（${response.statusCode}）`))
        },
        fail: (error) => reject(new Error(error.errMsg || '文件上传失败')),
      })
    })
  },
  citation(citationId: string, accessToken: string): Promise<CitationResponse> {
    return request<CitationResponse>({
      path: `/v1/citations/${encodeURIComponent(citationId)}`,
      accessToken,
    })
  },
}
