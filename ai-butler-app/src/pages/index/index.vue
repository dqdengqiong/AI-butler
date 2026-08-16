<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'

import AppSheet from '@/components/AppSheet.vue'
import ChatView from '@/components/ChatView.vue'
import ConversationDrawer from '@/components/ConversationDrawer.vue'
import LoginView from '@/components/LoginView.vue'
import PlansView from '@/components/PlansView.vue'
import { butlerApi, type ApiObject, type CitationResponse } from '@/api/butler'
import { ApiError } from '@/api/client'
import { chooseFile } from '@/platform/files'
import { openSourceLink } from '@/platform/source-links'
import { useAuthStore } from '@/stores/auth'
import { useButlerStore, type AssistantSceneTarget } from '@/stores/butler'
import type {
  AgentShortcutCode,
  ChatItem,
  MainTab,
  PlanViewModel,
  SheetName,
  TaskViewModel,
  UploadedAttachment,
} from '@/types/view-models'

type PlanChatItem = Extract<ChatItem, { kind: 'plan' }>

const auth = useAuthStore()
const butler = useButlerStore()
const { authenticated: loggedIn, user } = storeToRefs(auth)
const {
  plans,
  tasks,
  chatItems,
  agentShortcuts,
  conversations,
  activeConversationId,
  stagedScene,
  stagedSpecialistCode,
} = storeToRefs(butler)

const activeTab = ref<MainTab>('chat')
const activeSheet = ref<SheetName>(null)
const conversationDrawerOpen = ref(false)
const remindersEnabled = ref(true)
const reminderVersion = ref(1)
const attachments = ref<UploadedAttachment[]>([])
const uploading = ref(false)
const editingPlan = ref<PlanChatItem | null>(null)
const sourceDetail = ref<CitationResponse | null>(null)
const materialItems = ref<ApiObject[]>([])
const revisionItems = ref<ApiObject[]>([])
const submittingApprovalIds = new Set<string>()

const displayName = computed(() => user.value?.nickname || '小邓')
const appReady = computed(() => loggedIn.value)
const activeConversation = computed(() =>
  conversations.value.find((item) => item.key === activeConversationId.value),
)
const activeAgentCode = computed(() => {
  if (stagedScene.value?.kind === 'GENERAL') return undefined
  return stagedSpecialistCode.value ?? activeConversation.value?.agentCode
})
const activeAgent = computed(() =>
  agentShortcuts.value.find((agent) => agent.code === activeAgentCode.value),
)
const activeChatItems = computed(() => chatItems.value)
const activeAssistantSubtitle = computed(() => {
  const status = activeConversation.value?.statusLabel
  if (status && status !== '已完成') return status
  return activeAgent.value ? `${activeAgent.value.name}助理在线` : 'AI 管家在线'
})

const demoPlans: PlanViewModel[] = [
  {
    key: 'demo-plan',
    icon: '公',
    title: '公务员备考',
    subtitle: '距阶段目标还有 18 天',
    statusLabel: '进行中',
    progress: 62,
    progressLabel: '13 / 21 项',
    tone: 'blue',
  },
]
const demoTasks: TaskViewModel[] = [
  {
    key: 'demo-task-1',
    planKey: 'demo-plan',
    title: '行测判断推理 30 题',
    planTitle: '公务员备考',
    durationMinutes: 45,
    done: true,
    status: 'DONE',
    tone: 'blue',
  },
  {
    key: 'demo-task-2',
    planKey: 'demo-plan',
    title: '申论材料阅读与提纲',
    planTitle: '公务员备考',
    durationMinutes: 60,
    done: false,
    status: 'TODO',
    tone: 'blue',
  },
  {
    key: 'demo-task-3',
    planKey: 'demo-plan',
    title: '复盘本周错题',
    planTitle: '公务员备考',
    durationMinutes: 30,
    done: false,
    status: 'TODO',
    tone: 'blue',
  },
]
const visiblePlans = computed(() => (plans.value.length ? plans.value : demoPlans))
const visibleTasks = computed(() => (tasks.value.length ? tasks.value : demoTasks))
const sourceTitle = computed(() =>
  sourceDetail.value?.title ? sourceDetail.value.title : '来源详情',
)
const sourceExcerpt = computed(() =>
  typeof sourceDetail.value?.evidence_excerpt === 'string'
    ? sourceDetail.value.evidence_excerpt
    : '当前来源没有可展示的证据片段。',
)
const sourceOrganization = computed(() =>
  typeof sourceDetail.value?.source_organization === 'string'
    ? sourceDetail.value.source_organization
    : typeof sourceDetail.value?.domain === 'string'
      ? sourceDetail.value.domain
      : '来源信息不可用',
)
const sourceTypeLabel = computed(() => {
  if (sourceDetail.value?.source_type === 'PRIVATE_FILE') return '我的资料'
  if (sourceDetail.value?.source_type === 'WEB') return '网页来源'
  return '知识来源'
})
const sourcePublishedAt = computed(() =>
  typeof sourceDetail.value?.published_at === 'string'
    ? sourceDetail.value.published_at.slice(0, 10)
    : '未提供',
)
const sourceRetrievedAt = computed(() =>
  typeof sourceDetail.value?.retrieved_at === 'string'
    ? sourceDetail.value.retrieved_at.replace('T', ' ').slice(0, 19)
    : '未提供',
)
const sourceAccess = computed(() => sourceDetail.value?.access ?? null)

function token(): string {
  if (!auth.accessToken) throw new Error('请先登录')
  return auth.accessToken
}

async function loadPreferences(): Promise<void> {
  const response = await butlerApi.preferences(token())
  reminderVersion.value = typeof response.version === 'number' ? response.version : 1
  const reminder =
    typeof response.task_reminder === 'object' && response.task_reminder !== null
      ? (response.task_reminder as ApiObject)
      : {}
  remindersEnabled.value = reminder.enabled !== false
}

onMounted(async () => {
  if (await auth.restore()) {
    await Promise.all([butler.load(token()), loadPreferences()])
  }
})

async function onAuthenticated(): Promise<void> {
  await Promise.all([butler.load(token()), loadPreferences()])
}

function navigate(target: MainTab): void {
  activeTab.value = target
}

/**
 * 从输入框快捷栏进入专业 Agent。
 *
 * 未开放能力只提示；已开放能力先进入本地欢迎态，用户发送第一条消息时才落库。
 * 若该助理有挂起任务，则优先打开最近一次未完成会话。
 */
function assistantStatus(agentCode: AgentShortcutCode | null): string | null {
  return (
    conversations.value.find((item) => {
      const matches =
        agentCode === null ? item.agentCode === undefined : item.agentCode === agentCode
      return matches && item.statusLabel !== '已完成'
    })?.statusLabel ?? null
  )
}

function isAssistantCurrent(agentCode: AgentShortcutCode | null): boolean {
  if (stagedScene.value) {
    return stagedScene.value.kind === 'GENERAL'
      ? agentCode === null
      : stagedScene.value.specialistCode === agentCode
  }
  return agentCode === null
    ? activeConversation.value?.agentCode === undefined
    : activeConversation.value?.agentCode === agentCode
}

async function selectAssistant(agentCode: AgentShortcutCode | null): Promise<void> {
  const target: AssistantSceneTarget =
    agentCode === null ? { kind: 'GENERAL' } : { kind: 'SPECIALIST', specialistCode: agentCode }
  const label =
    agentCode === null
      ? 'AI 管家'
      : `${agentShortcuts.value.find((item) => item.code === agentCode)?.name ?? '专业'}助理`
  try {
    let result = await butler.switchAssistantScene(target, token())
    if (result === 'CONFIRMATION_REQUIRED') {
      const confirmed = await confirmSwitch(
        '停止当前处理并切换？',
        `当前任务仍在处理中，切换到${label}会停止这次处理。`,
        '停止并切换',
      )
      if (!confirmed) return
      result = await butler.switchAssistantScene(target, token(), { cancelExecuting: true })
    }
    if (result === 'CONFIRMATION_REQUIRED') return
    activeSheet.value = null
    activeTab.value = 'chat'
    attachments.value = []
    editingPlan.value = null
    if (result === 'RESUMABLE') {
      uni.showToast({ title: `已恢复${label}的未完成任务`, icon: 'none' })
    }
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '切换失败', icon: 'none' })
  }
}

async function activateAgent(agentCode: AgentShortcutCode): Promise<void> {
  const agent = agentShortcuts.value.find((item) => item.code === agentCode)
  if (!agent) return
  if (agent.availability === 'COMING_SOON') {
    uni.showToast({ title: `${agent.name}助理即将开放`, icon: 'none' })
    return
  }
  await selectAssistant(agent.code)
}

function confirmSwitch(title: string, content: string, confirmText: string): Promise<boolean> {
  return new Promise((resolve) => {
    uni.showModal({
      title,
      content,
      confirmText,
      cancelText: '继续当前话题',
      success: (result) => resolve(result.confirm),
      fail: () => resolve(false),
    })
  })
}

async function selectConversation(conversationKey: string): Promise<void> {
  try {
    await butler.loadConversation(conversationKey, token())
    conversationDrawerOpen.value = false
    activeTab.value = 'chat'
    attachments.value = []
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '对话加载失败', icon: 'none' })
  }
}

function deleteConversation(conversationKey: string): void {
  const conversation = conversations.value.find((item) => item.key === conversationKey)
  if (!conversation?.archived) return
  uni.showModal({
    title: '删除历史对话？',
    content: `“${conversation.title}”将从历史记录中移除，删除后无法恢复。`,
    confirmText: '删除',
    confirmColor: '#d44b55',
    success(result) {
      if (!result.confirm) return
      void butler
        .deleteConversation(conversationKey, token())
        .then(() => uni.showToast({ title: '历史对话已删除', icon: 'success' }))
        .catch((error: unknown) => {
          uni.showToast({
            title: error instanceof Error ? error.message : '删除失败',
            icon: 'none',
          })
        })
    },
  })
}

async function completeTask(taskKey: string): Promise<void> {
  const task = visibleTasks.value.find((item) => item.key === taskKey)
  if (!task) return

  if (task.done) {
    uni.showToast({ title: '已完成任务暂不支持撤销', icon: 'none' })
    return
  }

  try {
    await butler.completeTask(taskKey, token())
    uni.showToast({ title: '完成记录已提交', icon: 'success' })
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '提交失败', icon: 'none' })
  }
}

async function requestAdjustment(): Promise<void> {
  activeTab.value = 'chat'
  await sendMessage('今天时间不够，帮我减少一点公务员备考任务。')
}

function findChatItem(itemKey: string): ChatItem | undefined {
  return activeChatItems.value.find((item) => item.key === itemKey)
}

function selectOption(itemKey: string, optionIndex: number): void {
  const item = findChatItem(itemKey)
  if (item?.kind === 'selection' && !item.submitted) item.selected = optionIndex
}

async function submitSelection(itemKey: string): Promise<void> {
  const item = findChatItem(itemKey)
  if (item?.kind !== 'selection' || item.submitted) return
  const optionId = item.optionIds?.[item.selected]
  if (!item.cardId || !optionId) return
  item.submitted = true
  try {
    await butler.sendMessage('', token(), { cardId: item.cardId, optionId })
  } catch (error) {
    // 服务端未接受前恢复卡片可操作状态，避免一次网络错误永久锁住当前中断。
    item.submitted = false
    uni.showToast({ title: error instanceof Error ? error.message : '提交失败', icon: 'none' })
  }
}

async function approvePlan(item: PlanChatItem): Promise<void> {
  if (item.status !== 'pending' || submittingApprovalIds.has(item.approvalId)) return
  submittingApprovalIds.add(item.approvalId)
  try {
    await butler.approvePlan(item, 'APPROVE', token())
  } catch (error) {
    if (
      error instanceof ApiError &&
      error.code === 'OTHER_CONVERSATION_RUNNING' &&
      (await confirmSwitch('另一项任务正在处理', '停止当前处理并确认这份计划吗？', '停止并确认'))
    ) {
      await butler.approvePlan(item, 'APPROVE', token(), undefined, 'CANCEL_OTHER')
      return
    }
    uni.showToast({ title: error instanceof Error ? error.message : '审批失败', icon: 'none' })
  } finally {
    submittingApprovalIds.delete(item.approvalId)
  }
}

function editPlan(item: PlanChatItem): void {
  if (item.status !== 'pending') return
  item.status = 'editing'
  editingPlan.value = item
  uni.showToast({ title: '请在输入框继续说明', icon: 'none' })
}

async function sendMessage(content: string): Promise<void> {
  const normalized = content.trim()
  const clientMessageId = `message-${Date.now()}-${Math.random().toString(36).slice(2)}`
  try {
    if (editingPlan.value) {
      await butler.approvePlan(editingPlan.value, 'EDIT', token(), normalized)
      editingPlan.value = null
    } else {
      const wasStagedWelcome = stagedScene.value !== null
      const response = await butler.sendMessage(
        normalized || '请处理我添加的资料',
        token(),
        undefined,
        attachments.value.map((item) => item.id),
        { clientMessageId },
      )
      if (response.transition.kind === 'CREATED' && !wasStagedWelcome) {
        uni.showToast({ title: '已为你整理为新话题', icon: 'none' })
      }
    }
    attachments.value = []
  } catch (error) {
    if (error instanceof ApiError && error.code === 'TOPIC_SWITCH_CONFIRMATION_REQUIRED') {
      const confirmed = await confirmSwitch(
        '开始新话题？',
        '我可以暂存当前话题，并从这条消息开始整理为新话题。',
        '开始新话题',
      )
      if (confirmed) {
        const response = await butler.sendMessage(
          normalized || '请处理我添加的资料',
          token(),
          undefined,
          attachments.value.map((item) => item.id),
          {
            clientMessageId,
            contextPolicy: 'ARCHIVE_AND_START',
            executionPolicy: 'CANCEL_OTHER',
          },
        )
        attachments.value = []
        if (response.transition.kind === 'CREATED') {
          uni.showToast({ title: '已为你整理为新话题', icon: 'none' })
        }
        return
      }
      return
    }
    if (error instanceof ApiError && error.code === 'OTHER_CONVERSATION_RUNNING') {
      const confirmed = await confirmSwitch(
        '另一项任务正在处理',
        '是否停止当前处理并切换到这里？',
        '停止并切换',
      )
      if (confirmed) {
        await butler.sendMessage(
          normalized || '请处理我添加的资料',
          token(),
          undefined,
          attachments.value.map((item) => item.id),
          { clientMessageId, executionPolicy: 'CANCEL_OTHER' },
        )
        attachments.value = []
      }
      return
    }
    uni.showToast({ title: error instanceof Error ? error.message : '发送失败', icon: 'none' })
  }
}

async function chooseAttachment(): Promise<void> {
  if (uploading.value) return
  uploading.value = true
  uni.showLoading({ title: '正在安全上传' })
  try {
    const selected = await chooseFile()
    const intent = await butlerApi.createUpload(
      {
        schema_version: '1.0',
        purpose: 'CHAT_ATTACHMENT',
        filename: selected.name,
        declared_mime_type: selected.mimeType,
        size_bytes: selected.bytes.byteLength,
        sha256: selected.sha256,
      },
      token(),
    )
    const file =
      typeof intent.file === 'object' && intent.file !== null ? (intent.file as ApiObject) : null
    const upload =
      typeof intent.upload === 'object' && intent.upload !== null
        ? (intent.upload as ApiObject)
        : null
    if (!file || !upload || typeof file.id !== 'string' || typeof upload.url !== 'string') {
      throw new Error('上传意图响应无效')
    }
    const headers =
      typeof upload.headers === 'object' && upload.headers !== null
        ? Object.fromEntries(
            Object.entries(upload.headers).filter(
              (entry): entry is [string, string] => typeof entry[1] === 'string',
            ),
          )
        : {}
    await butlerApi.putUpload(upload.url, headers, selected.bytes)
    await butlerApi.completeUpload(
      file.id,
      { schema_version: '1.0', sha256: selected.sha256 },
      token(),
    )
    attachments.value.push({ id: file.id, name: selected.name })
    activeSheet.value = null
    uni.showToast({ title: '文件已安全上传，正在建立检索索引', icon: 'none' })
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '上传失败', icon: 'none' })
  } finally {
    uploading.value = false
    uni.hideLoading()
  }
}

function removeAttachment(fileId: string): void {
  attachments.value = attachments.value.filter((item) => item.id !== fileId)
}

async function updateReminders(enabled: boolean): Promise<void> {
  const previous = remindersEnabled.value
  remindersEnabled.value = enabled
  try {
    const response = await butlerApi.updatePreferences(
      {
        expected_version: reminderVersion.value,
        task_reminder: { enabled, channels: ['IN_APP'], advance_minutes: 15 },
      },
      token(),
    )
    reminderVersion.value =
      typeof response.version === 'number' ? response.version : reminderVersion.value
  } catch (error) {
    remindersEnabled.value = previous
    uni.showToast({ title: error instanceof Error ? error.message : '设置保存失败', icon: 'none' })
  }
}

function deleteAccount(): void {
  uni.showModal({
    title: '永久注销账号？',
    content: '服务端将撤销会话，并异步删除账号、消息、计划和文件。此操作不可恢复。',
    confirmText: '永久注销',
    confirmColor: '#d44b55',
    success(result) {
      if (!result.confirm) return
      void butlerApi.deleteAccount(token()).then(() => {
        auth.clear()
        butler.reset()
        activeSheet.value = null
      })
    },
  })
}

function logout(): void {
  uni.showModal({
    title: '退出当前账号？',
    content: '将撤销当前设备的刷新会话。',
    confirmText: '退出',
    success(result) {
      if (!result.confirm) return
      void auth.logout().finally(() => {
        butler.reset()
        activeSheet.value = null
        activeTab.value = 'chat'
      })
    },
  })
}

async function openSource(citationId: string): Promise<void> {
  if (!citationId) return
  try {
    sourceDetail.value = await butlerApi.citation(citationId, token())
    activeSheet.value = 'source'
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '来源加载失败', icon: 'none' })
  }
}

async function openMaterials(): Promise<void> {
  try {
    const response = await butlerApi.files(token())
    materialItems.value = Array.isArray(response.items)
      ? response.items.filter(
          (item): item is ApiObject => typeof item === 'object' && item !== null,
        )
      : []
    activeSheet.value = 'materials'
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '资料加载失败', icon: 'none' })
  }
}

function selectMaterial(item: ApiObject): void {
  if (typeof item.id !== 'string' || typeof item.original_filename !== 'string') return
  if (item.knowledge_status !== 'READY') {
    uni.showToast({ title: '资料完成索引后才能用于检索', icon: 'none' })
    return
  }
  if (!attachments.value.some((attachment) => attachment.id === item.id)) {
    attachments.value.push({ id: item.id, name: item.original_filename })
  }
  activeSheet.value = null
  activeTab.value = 'chat'
}

async function openSourceOriginal(): Promise<void> {
  const access = sourceAccess.value
  const accessType = access?.type
  const url = access?.url
  if (typeof url !== 'string' || (accessType !== 'EXTERNAL_URL' && accessType !== 'SIGNED_FILE')) {
    uni.showToast({ title: '当前来源没有可打开的原文', icon: 'none' })
    return
  }
  try {
    await openSourceLink(url, accessType)
    if (accessType === 'EXTERNAL_URL') {
      // #ifndef H5
      uni.showToast({ title: '来源地址已复制', icon: 'none' })
      // #endif
    }
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '来源打开失败', icon: 'none' })
  }
}

async function openHistory(): Promise<void> {
  const planId = plans.value[0]?.key
  if (!planId) {
    revisionItems.value = []
    activeSheet.value = 'history'
    return
  }
  try {
    const response = await butlerApi.revisions(planId, token())
    revisionItems.value = Array.isArray(response.items)
      ? response.items.filter(
          (item): item is ApiObject => typeof item === 'object' && item !== null,
        )
      : []
    activeSheet.value = 'history'
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '版本加载失败', icon: 'none' })
  }
}
</script>

<template>
  <LoginView v-if="!appReady" @authenticated="onAuthenticated" />

  <view v-else class="app-shell">
    <view class="topbar">
      <button
        class="topbar-icon menu-button"
        :aria-label="activeTab === 'plans' ? '返回聊天' : '打开历史对话'"
        @click="activeTab === 'plans' ? navigate('chat') : (conversationDrawerOpen = true)"
      >
        {{ activeTab === 'plans' ? '‹' : '☰' }}
      </button>
      <view v-if="activeTab === 'plans'" class="topbar-title">
        <text class="page-title">计划</text>
        <text class="page-subtitle">目标、任务与进度</text>
      </view>
      <button
        v-else
        class="topbar-title assistant-switch-trigger"
        aria-label="切换 AI 管家或专业助理"
        :aria-expanded="activeSheet === 'assistants'"
        @click="activeSheet = 'assistants'"
      >
        <view class="assistant-title-row">
          <text class="assistant-title-icon">{{ activeAgent?.icon ?? '✦' }}</text>
          <text class="page-title">{{ activeAgent ? `${activeAgent.name}助理` : 'AI 管家' }}</text>
          <text class="assistant-chevron">⌄</text>
        </view>
        <text class="page-subtitle">{{ activeAssistantSubtitle }}</text>
      </button>
      <view class="topbar-actions">
        <button
          class="topbar-icon"
          :class="{ active: activeTab === 'plans' }"
          aria-label="打开计划"
          @click="navigate(activeTab === 'plans' ? 'chat' : 'plans')"
        >
          <text class="header-icon">✓</text><text class="header-label">计划</text>
        </button>
        <button class="topbar-icon" aria-label="打开设置" @click="activeSheet = 'settings'">
          <text class="header-icon">⚙</text><text class="header-label">设置</text>
        </button>
      </view>
    </view>

    <view class="main-view" :class="{ 'chat-main': activeTab === 'chat' }">
      <PlansView
        v-if="activeTab === 'plans'"
        :plans="visiblePlans"
        :tasks="visibleTasks"
        @complete-task="completeTask"
        @request-adjustment="requestAdjustment"
      />
      <ChatView
        v-else
        :items="activeChatItems"
        :attachments="attachments"
        :user-name="displayName"
        :agent-shortcuts="agentShortcuts"
        :active-agent-code="activeAgentCode"
        @send="sendMessage"
        @open-attachments="activeSheet = 'attachments'"
        @remove-attachment="removeAttachment"
        @select-option="selectOption"
        @submit-selection="submitSelection"
        @approve-plan="approvePlan"
        @edit-plan="editPlan"
        @open-source="openSource"
        @select-agent="activateAgent"
      />
    </view>

    <ConversationDrawer
      :open="conversationDrawerOpen"
      :user-name="displayName"
      :active-key="activeConversationId ?? ''"
      :conversations="conversations"
      :agent-shortcuts="agentShortcuts"
      @close="conversationDrawerOpen = false"
      @select="selectConversation"
      @delete="deleteConversation"
      @open-materials="openMaterials"
    />

    <AppSheet
      :open="activeSheet === 'assistants'"
      eyebrow="当前场景"
      title="切换助理"
      @close="activeSheet = null"
    >
      <view class="assistant-picker">
        <text class="assistant-group-label">通用</text>
        <button
          class="assistant-option"
          :class="{ current: isAssistantCurrent(null) }"
          :aria-pressed="isAssistantCurrent(null)"
          @click="selectAssistant(null)"
        >
          <text class="assistant-option-icon general">✦</text>
          <view class="assistant-option-copy">
            <text class="assistant-option-name">AI 管家</text>
            <text class="assistant-option-description">处理日常问题、资料和跨领域计划</text>
          </view>
          <text v-if="isAssistantCurrent(null)" class="assistant-option-state current">当前</text>
          <text v-else-if="assistantStatus(null)" class="assistant-option-state pending">{{
            assistantStatus(null)
          }}</text>
          <text v-else class="assistant-option-arrow">›</text>
        </button>

        <text class="assistant-group-label specialists">专业助理</text>
        <button
          v-for="agent in agentShortcuts"
          :key="agent.code"
          class="assistant-option"
          :class="{
            current: isAssistantCurrent(agent.code),
            unavailable: agent.availability === 'COMING_SOON',
          }"
          :disabled="agent.availability === 'COMING_SOON'"
          :aria-pressed="isAssistantCurrent(agent.code)"
          @click="selectAssistant(agent.code)"
        >
          <text class="assistant-option-icon">{{ agent.icon }}</text>
          <view class="assistant-option-copy">
            <text class="assistant-option-name">{{ agent.name }}助理</text>
            <text class="assistant-option-description">{{ agent.description }}</text>
          </view>
          <text v-if="isAssistantCurrent(agent.code)" class="assistant-option-state current"
            >当前</text
          >
          <text
            v-else-if="agent.availability === 'COMING_SOON'"
            class="assistant-option-state unavailable"
            >即将开放</text
          >
          <text v-else-if="assistantStatus(agent.code)" class="assistant-option-state pending">{{
            assistantStatus(agent.code)
          }}</text>
          <text v-else class="assistant-option-arrow">›</text>
        </button>
      </view>
    </AppSheet>

    <AppSheet
      :open="activeSheet === 'attachments'"
      eyebrow="添加到当前对话"
      title="给管家补充上下文"
      @close="activeSheet = null"
    >
      <view class="attachment-grid">
        <button @click="chooseAttachment">
          <text class="attachment-icon">文</text><text class="sheet-option-title">上传文件</text
          ><text class="sheet-option-copy">简历、公告、表格和文档</text>
        </button>
        <button @click="chooseAttachment">
          <text class="attachment-icon purple">图</text
          ><text class="sheet-option-title">选择图片</text
          ><text class="sheet-option-copy">题目、课程或岗位截图</text>
        </button>
        <button @click="chooseAttachment">
          <text class="attachment-icon orange">扫</text
          ><text class="sheet-option-title">拍照 / 扫描</text
          ><text class="sheet-option-copy">错题、通知或学习记录</text>
        </button>
        <button @click="openMaterials">
          <text class="attachment-icon green">库</text
          ><text class="sheet-option-title">从资料库选择</text
          ><text class="sheet-option-copy">使用已入库的私有资料</text>
        </button>
      </view>
      <view class="sheet-note">文件需完成私有上传与安全扫描后，才能作为 file_id 随消息发送。</view>
    </AppSheet>

    <AppSheet
      :open="activeSheet === 'materials'"
      eyebrow="私有资料"
      title="我的资料"
      @close="activeSheet = null"
    >
      <view v-if="materialItems.length" class="settings-list">
        <button
          v-for="item in materialItems"
          :key="String(item.id)"
          class="setting-row"
          @click="selectMaterial(item)"
        >
          <view>
            <text>{{ item.original_filename }}</text>
            <text>{{ item.mime_type }} · {{ item.size_bytes }} B</text>
          </view>
          <text>{{
            item.knowledge_status === 'READY'
              ? '可检索'
              : item.knowledge_status === 'FAILED'
                ? '入库失败'
                : '处理中'
          }}</text>
        </button>
      </view>
      <view v-else class="sheet-note">暂无学习资料，可在聊天页上传。</view>
    </AppSheet>

    <AppSheet
      :open="activeSheet === 'history'"
      eyebrow="审批留痕"
      title="计划版本"
      @close="activeSheet = null"
    >
      <view v-if="revisionItems.length" class="settings-list">
        <view v-for="item in revisionItems" :key="String(item.id)" class="setting-row">
          <view>
            <text>版本 {{ item.revision }}</text>
            <text>{{ item.objective_summary }}</text>
          </view>
          <text>{{ item.status }}</text>
        </view>
      </view>
      <view v-else class="sheet-note">暂无计划版本。</view>
    </AppSheet>

    <AppSheet
      :open="activeSheet === 'source'"
      eyebrow="引用来源"
      :title="sourceTitle"
      @close="activeSheet = null"
    >
      <view class="source-meta">
        <text class="official-badge">{{ sourceTypeLabel }}</text
        ><text>{{ sourceOrganization }}</text>
      </view>
      <view class="source-detail">
        <text class="source-heading">证据片段</text>
        <text class="source-copy">{{ sourceExcerpt }}</text>
      </view>
      <view class="source-detail">
        <text class="source-heading">来源信息</text>
        <view class="audit-row"
          ><text>来源类型</text><text>{{ sourceTypeLabel }}</text></view
        >
        <view class="audit-row"
          ><text>发布时间</text><text>{{ sourcePublishedAt }}</text></view
        >
        <view class="audit-row"
          ><text>检索时间</text><text>{{ sourceRetrievedAt }}</text></view
        >
      </view>
      <button class="sheet-primary" @click="openSourceOriginal">查看资料原文</button>
    </AppSheet>

    <AppSheet
      :open="activeSheet === 'settings'"
      eyebrow="偏好与隐私"
      title="设置"
      tall
      @close="activeSheet = null"
    >
      <view class="settings-list">
        <button class="setting-row setting-button" @click="openMaterials">
          <view><text>我的资料</text><text>管理聊天可调用的私有资料</text></view>
          <text class="setting-arrow">›</text>
        </button>
        <button class="setting-row setting-button" @click="openHistory">
          <view><text>计划版本</text><text>查看调整记录与审批留痕</text></view>
          <text class="setting-arrow">›</text>
        </button>
        <view class="setting-row">
          <view><text>任务提醒</text><text>计划开始前提醒</text></view>
          <switch
            :checked="remindersEnabled"
            color="#596bff"
            @change="updateReminders(!remindersEnabled)"
          />
        </view>
        <view class="setting-row">
          <view><text>计划变更需确认</text><text>安全策略，无法关闭</text></view>
          <switch checked disabled color="#596bff" />
        </view>
      </view>
      <view v-if="loggedIn" class="danger-zone">
        <button @click="deleteAccount">注销账号</button>
        <button class="logout" @click="logout">退出登录</button>
        <text>账号注销会撤销会话，并进入服务端异步删除流程。</text>
      </view>
    </AppSheet>
  </view>
</template>

<style scoped>
.auth-screen {
  box-sizing: border-box;
  display: flex;
  min-height: 100vh;
  align-items: center;
  justify-content: center;
  padding: 48rpx 32rpx;
  background:
    radial-gradient(circle at 18% 10%, rgba(89, 107, 255, 0.23), transparent 28%),
    radial-gradient(circle at 88% 84%, rgba(78, 197, 160, 0.24), transparent 30%), #eef1f7;
}

.auth-card {
  position: relative;
  box-sizing: border-box;
  width: min(410px, 100%);
  padding: 62rpx 42rpx 38rpx;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.96);
  border: 1px solid rgba(255, 255, 255, 0.85);
  border-radius: 48rpx;
  box-shadow: 0 42rpx 100rpx rgba(25, 34, 61, 0.17);
}

.auth-orbit {
  position: absolute;
  width: 180rpx;
  height: 180rpx;
  border: 1px solid rgba(89, 107, 255, 0.12);
  border-radius: 50%;
}

.orbit-one {
  top: -90rpx;
  right: -30rpx;
}

.orbit-two {
  top: -50rpx;
  right: 10rpx;
  width: 90rpx;
  height: 90rpx;
}

.auth-brand {
  display: flex;
  align-items: center;
  text-align: center;
  flex-direction: column;
}

.auth-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 126rpx;
  height: 126rpx;
  color: #fff;
  font-size: 40rpx;
  font-weight: 900;
  letter-spacing: -2rpx;
  background: linear-gradient(135deg, #596bff, #4ec5a0);
  border-radius: 42rpx;
  box-shadow: 0 22rpx 44rpx rgba(89, 107, 255, 0.25);
}

.auth-kicker {
  margin-top: 32rpx;
  color: #596bff;
  font-size: 18rpx;
  font-weight: 800;
  letter-spacing: 4rpx;
}

.auth-title {
  margin-top: 12rpx;
  color: #182036;
  font-size: 42rpx;
  font-weight: 790;
}

.auth-description {
  max-width: 510rpx;
  margin-top: 18rpx;
  color: #727b91;
  font-size: 23rpx;
  line-height: 1.7;
}

.feature-list {
  display: grid;
  gap: 16rpx;
  margin: 40rpx 0;
  padding: 26rpx;
  background: #f7f8fb;
  border-radius: 28rpx;
}

.feature-list > view {
  display: flex;
  align-items: center;
  gap: 18rpx;
  color: #3b455d;
  font-size: 22rpx;
}

.feature-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 48rpx;
  height: 48rpx;
  color: #596bff;
  font-size: 18rpx;
  font-weight: 750;
  background: #e9ecff;
  border-radius: 16rpx;
}

.feature-icon.mint {
  color: #23896a;
  background: #e5f7f1;
}

.feature-icon.orange {
  color: #cc7c23;
  background: #fff1e0;
}

.wechat-button {
  display: flex;
  min-height: 88rpx;
  align-items: center;
  justify-content: center;
  gap: 14rpx;
  color: #fff;
  font-size: 25rpx;
  font-weight: 750;
  line-height: 88rpx;
  background: #17b35b;
  border: 0;
  border-radius: 26rpx;
  box-shadow: 0 16rpx 30rpx rgba(23, 179, 91, 0.2);
}

.wechat-button::after {
  border: 0;
}

.wechat-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40rpx;
  height: 40rpx;
  color: #17a354;
  font-size: 17rpx;
  background: #fff;
  border-radius: 50%;
}

.agreement-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10rpx;
  margin-top: 24rpx;
  color: #7b8497;
  font-size: 18rpx;
}

.agreement-check {
  width: 28rpx;
  height: 28rpx;
  color: #fff;
  font-size: 18rpx;
  line-height: 28rpx;
  text-align: center;
  border: 1px solid #c9cfdb;
  border-radius: 8rpx;
}

.agreement-check.checked {
  background: #596bff;
  border-color: #596bff;
}

.auth-note {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10rpx;
  margin-top: 26rpx;
  color: #8b93a3;
  font-size: 17rpx;
}

.auth-note > text:first-child {
  width: 25rpx;
  height: 25rpx;
  color: #596bff;
  line-height: 25rpx;
  text-align: center;
  background: #eef0ff;
  border-radius: 50%;
}

.app-shell {
  position: relative;
  box-sizing: border-box;
  width: 100%;
  max-width: 430px;
  min-height: 100vh;
  margin: 0 auto;
  overflow-x: hidden;
  background:
    radial-gradient(circle at 15% 8%, rgba(184, 171, 255, 0.28), transparent 25%),
    radial-gradient(circle at 90% 82%, rgba(214, 207, 255, 0.34), transparent 27%), #f4f2ff;
  box-shadow: 0 0 90rpx rgba(43, 35, 79, 0.14);
}

.topbar {
  position: sticky;
  z-index: 15;
  top: 0;
  display: flex;
  align-items: center;
  gap: 12rpx;
  padding: calc(22rpx + env(safe-area-inset-top)) 22rpx 18rpx;
  background: rgba(244, 242, 255, 0.88);
  border-bottom: 1px solid rgba(100, 82, 174, 0.08);
  backdrop-filter: blur(18px);
}

.topbar-title {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 3rpx;
}

.assistant-switch-trigger {
  align-items: flex-start;
  justify-content: center;
  min-height: 72rpx;
  margin: 0;
  padding: 0 8rpx;
  line-height: 1.2;
  text-align: left;
  background: transparent;
  border: 0;
  border-radius: 18rpx;
}

.assistant-switch-trigger:active {
  background: rgba(101, 86, 232, 0.08);
}

.assistant-switch-trigger::after {
  border: 0;
}

.assistant-title-row {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8rpx;
}

.assistant-title-icon {
  display: flex;
  flex: 0 0 auto;
  width: 34rpx;
  height: 34rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 18rpx;
  font-weight: 800;
  background: linear-gradient(145deg, #6d5be5, #9d91f8);
  border-radius: 11rpx;
}

.assistant-chevron {
  flex: 0 0 auto;
  color: #817b94;
  font-size: 23rpx;
  transform: translateY(-2rpx);
}

.page-title {
  overflow: hidden;
  color: #29263b;
  font-size: 28rpx;
  font-weight: 760;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.page-subtitle {
  color: #918ca1;
  font-size: 17rpx;
}

.topbar-actions {
  display: flex;
  flex: 0 0 auto;
  gap: 7rpx;
}

.topbar-icon {
  display: flex;
  width: 72rpx;
  height: 72rpx;
  align-items: center;
  justify-content: center;
  margin: 0;
  padding: 0;
  color: #555066;
  line-height: 1.1;
  background: rgba(255, 255, 255, 0.7);
  border: 1px solid rgba(97, 78, 174, 0.1);
  border-radius: 23rpx;
  flex-direction: column;
}

.topbar-icon.active {
  color: #6556e8;
  background: #eae6ff;
}

.menu-button {
  flex: 0 0 auto;
  color: #343044;
  font-size: 31rpx;
  font-weight: 750;
  background: transparent;
  border-color: transparent;
}

.header-icon {
  font-size: 22rpx;
  font-weight: 800;
}

.header-label {
  margin-top: 3rpx;
  font-size: 14rpx;
  font-weight: 650;
}

.topbar-icon::after,
.tab-button::after,
.attachment-grid button::after,
.assistant-option::after,
.sheet-primary::after,
.danger-zone button::after {
  border: 0;
}

.assistant-picker {
  display: flex;
  margin-top: 28rpx;
  flex-direction: column;
}

.assistant-group-label {
  margin: 0 5rpx 10rpx;
  color: #918ca1;
  font-size: 18rpx;
  font-weight: 700;
}

.assistant-group-label.specialists {
  margin-top: 26rpx;
}

.assistant-option {
  display: flex;
  min-height: 108rpx;
  align-items: center;
  gap: 18rpx;
  margin: 0 0 12rpx;
  padding: 17rpx 18rpx;
  color: #302d42;
  line-height: 1.25;
  text-align: left;
  background: #f7f6fc;
  border: 1px solid #ebe8f4;
  border-radius: 25rpx;
}

.assistant-option.current {
  background: #efecff;
  border-color: #c8bfff;
}

.assistant-option.unavailable {
  opacity: 0.62;
}

.assistant-option-icon {
  display: flex;
  flex: 0 0 auto;
  width: 68rpx;
  height: 68rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 28rpx;
  font-weight: 800;
  background: linear-gradient(145deg, #7666ed, #a79cf8);
  border-radius: 22rpx;
  box-shadow: 0 8rpx 20rpx rgba(86, 67, 190, 0.16);
}

.assistant-option-icon.general {
  background: linear-gradient(145deg, #4e68dc, #8498f4);
}

.assistant-option-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 7rpx;
}

.assistant-option-name {
  overflow: hidden;
  color: #302d42;
  font-size: 23rpx;
  font-weight: 750;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.assistant-option-description {
  overflow: hidden;
  color: #898498;
  font-size: 18rpx;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.assistant-option-state {
  flex: 0 0 auto;
  padding: 8rpx 12rpx;
  font-size: 16rpx;
  font-weight: 700;
  border-radius: 999rpx;
}

.assistant-option-state.current {
  color: #5d4fd5;
  background: #ded8ff;
}

.assistant-option-state.pending {
  color: #9a651e;
  background: #fff0d8;
}

.assistant-option-state.unavailable {
  color: #777284;
  background: #ebe9ef;
}

.assistant-option-arrow {
  flex: 0 0 auto;
  color: #aaa5b6;
  font-size: 34rpx;
}

.main-view {
  padding: 24rpx 30rpx calc(40rpx + env(safe-area-inset-bottom));
}

.main-view.chat-main {
  padding-top: 0;
  padding-right: 22rpx;
  padding-bottom: 0;
  padding-left: 22rpx;
}

.tabbar {
  position: fixed;
  z-index: 40;
  bottom: calc(14rpx + env(safe-area-inset-bottom));
  left: 50%;
  display: grid;
  box-sizing: border-box;
  grid-template-columns: repeat(4, 1fr);
  width: min(406px, calc(100% - 40rpx));
  height: 108rpx;
  padding: 10rpx 14rpx;
  background: rgba(255, 255, 255, 0.97);
  border: 1px solid #e2e6ee;
  border-radius: 38rpx;
  box-shadow: 0 24rpx 60rpx rgba(25, 34, 61, 0.18);
  transform: translateX(-50%);
  backdrop-filter: blur(20px);
}

.tab-button {
  display: flex;
  min-height: auto;
  align-items: center;
  justify-content: center;
  gap: 2rpx;
  margin: 0;
  padding: 0;
  color: #81899b;
  line-height: 1.2;
  background: transparent;
  border: 0;
  border-radius: 26rpx;
  flex-direction: column;
}

.tab-button.active {
  color: #596bff;
  background: #eef0ff;
}

.tab-icon {
  font-size: 30rpx;
}

.tab-label {
  font-size: 17rpx;
}

.tab-button.central .tab-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 54rpx;
  height: 54rpx;
  margin-top: -20rpx;
  color: #fff;
  font-size: 26rpx;
  background: #596bff;
  border-radius: 50%;
  box-shadow: 0 12rpx 26rpx rgba(89, 107, 255, 0.35);
}

.attachment-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16rpx;
  margin-top: 30rpx;
}

.attachment-grid button {
  display: flex;
  min-height: 180rpx;
  align-items: flex-start;
  margin: 0;
  padding: 24rpx;
  line-height: 1.35;
  text-align: left;
  background: #f8f9fc;
  border: 1px solid #e2e6ef;
  border-radius: 28rpx;
  flex-direction: column;
}

.attachment-icon {
  width: 56rpx;
  height: 56rpx;
  color: #596bff;
  font-size: 20rpx;
  font-weight: 750;
  line-height: 56rpx;
  text-align: center;
  background: #eef0ff;
  border-radius: 18rpx;
}

.attachment-icon.purple {
  color: #986ee8;
  background: #f3edff;
}

.attachment-icon.orange {
  color: #d98427;
  background: #fff2e3;
}

.attachment-icon.green {
  color: #2f9773;
  background: #e8f7f2;
}

.sheet-option-title {
  margin-top: 14rpx;
  color: #283148;
  font-size: 22rpx;
  font-weight: 700;
}

.sheet-option-copy {
  margin-top: 7rpx;
  color: #7b8497;
  font-size: 18rpx;
}

.sheet-note {
  margin-top: 22rpx;
  padding: 20rpx;
  color: #757e91;
  font-size: 19rpx;
  line-height: 1.6;
  background: #f2f4f8;
  border-radius: 20rpx;
}

.source-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12rpx;
  margin: 28rpx 0 20rpx;
  color: #737c90;
  font-size: 19rpx;
}

.official-badge {
  padding: 8rpx 14rpx;
  color: #168764;
  background: #e9f8f2;
  border-radius: 999rpx;
}

.source-detail {
  margin-bottom: 16rpx;
  padding: 23rpx;
  background: #f6f7fa;
  border-radius: 24rpx;
}

.source-heading,
.source-copy {
  display: block;
}

.source-heading {
  font-size: 22rpx;
  font-weight: 720;
}

.source-copy {
  margin-top: 13rpx;
  color: #596277;
  font-size: 20rpx;
  line-height: 1.65;
}

.audit-row {
  display: flex;
  justify-content: space-between;
  padding: 16rpx 0;
  font-size: 20rpx;
  border-bottom: 1px solid #e3e6ec;
}

.audit-row:last-child {
  border-bottom: 0;
}

.audit-row > text:last-child {
  color: #168764;
  font-weight: 700;
}

.sheet-primary {
  min-height: 82rpx;
  color: #fff;
  font-size: 23rpx;
  font-weight: 720;
  line-height: 82rpx;
  background: #596bff;
  border: 0;
  border-radius: 24rpx;
}

.settings-list {
  margin-top: 30rpx;
}

.setting-row {
  display: flex;
  min-height: 114rpx;
  align-items: center;
  justify-content: space-between;
  gap: 22rpx;
  border-bottom: 1px solid #e6e9ef;
}

.setting-button {
  width: 100%;
  margin: 0;
  padding: 0;
  line-height: 1.25;
  text-align: left;
  background: transparent;
  border-radius: 0;
}

.setting-button::after {
  border: 0;
}

.setting-arrow {
  color: #aaa5b5;
  font-size: 34rpx;
}

.setting-row > view {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 8rpx;
}

.setting-row > view > text:first-child {
  font-size: 23rpx;
  font-weight: 680;
}

.setting-row > view > text:last-child {
  color: #7c8496;
  font-size: 18rpx;
}

.danger-zone {
  display: grid;
  gap: 14rpx;
  margin-top: 34rpx;
}

.danger-zone button {
  min-height: 76rpx;
  color: #d84b55;
  font-size: 22rpx;
  line-height: 76rpx;
  background: #fff0f1;
  border: 0;
  border-radius: 22rpx;
}

.danger-zone button.logout {
  color: #384157;
  background: #f0f2f6;
}

.danger-zone > text {
  color: #858da0;
  font-size: 18rpx;
  line-height: 1.55;
  text-align: center;
}

@media (min-width: 700px) {
  .auth-screen {
    padding: 48rpx;
  }

  .app-shell {
    min-height: 900px;
  }
}
</style>
