<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'

import ChatView from '@/components/ChatView.vue'
import ConversationDrawer from '@/components/ConversationDrawer.vue'
import LoginView from '@/components/LoginView.vue'
import PlansView from '@/components/PlansView.vue'
import AssistantPickerSheet from '@/components/page/AssistantPickerSheet.vue'
import AttachmentPickerSheet from '@/components/page/AttachmentPickerSheet.vue'
import MainTopbar from '@/components/page/MainTopbar.vue'
import MaterialsSheet from '@/components/page/MaterialsSheet.vue'
import PlanHistorySheet from '@/components/page/PlanHistorySheet.vue'
import SettingsSheet from '@/components/page/SettingsSheet.vue'
import SourceDetailSheet from '@/components/page/SourceDetailSheet.vue'
import { usePageChat } from '@/composables/usePageChat'
import { usePageResources } from '@/composables/usePageResources'
import { useAuthStore } from '@/stores/auth'
import { useButlerStore, type AssistantSceneTarget } from '@/stores/butler'
import type {
  AgentShortcutCode,
  MainTab,
  PlanViewModel,
  SheetName,
  TaskViewModel,
} from '@/types/view-models'

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

function token(): string {
  if (!auth.accessToken) throw new Error('请先登录')
  return auth.accessToken
}

const resources = usePageResources(activeTab, activeSheet, token)
const {
  attachments,
  chooseAttachment,
  deleteAccount,
  loadPreferences,
  logout,
  materialItems,
  openHistory,
  openMaterials,
  openSource,
  openSourceOriginal,
  remindersEnabled,
  removeAttachment,
  revisionItems,
  selectMaterial,
  sourceExcerpt,
  sourceOrganization,
  sourcePublishedAt,
  sourceRetrievedAt,
  sourceTitle,
  sourceTypeLabel,
  updateReminders,
} = resources
const { approvePlan, editPlan, editingPlan, selectOption, sendMessage, submitSelection } =
  usePageChat(token, attachments)

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

onMounted(async () => {
  if (await auth.restore()) await Promise.all([butler.load(token()), loadPreferences()])
})

async function onAuthenticated(): Promise<void> {
  await Promise.all([butler.load(token()), loadPreferences()])
}

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
    if (result === 'RESUMABLE') uni.showToast({ title: `已恢复${label}的未完成任务`, icon: 'none' })
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
        .catch((error: unknown) =>
          uni.showToast({
            title: error instanceof Error ? error.message : '删除失败',
            icon: 'none',
          }),
        )
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
</script>

<template>
  <LoginView v-if="!appReady" @authenticated="onAuthenticated" />
  <view v-else class="app-shell">
    <MainTopbar
      :active-tab="activeTab"
      :active-agent="activeAgent"
      :assistant-subtitle="activeAssistantSubtitle"
      :assistants-open="activeSheet === 'assistants'"
      @navigate="(tab) => (activeTab = tab)"
      @open-drawer="conversationDrawerOpen = true"
      @open-assistants="activeSheet = 'assistants'"
      @open-settings="activeSheet = 'settings'"
    />
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
        :items="chatItems"
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
    <AssistantPickerSheet
      :open="activeSheet === 'assistants'"
      :agents="agentShortcuts"
      :is-current="isAssistantCurrent"
      :status-for="assistantStatus"
      @close="activeSheet = null"
      @select="selectAssistant"
    />
    <AttachmentPickerSheet
      :open="activeSheet === 'attachments'"
      @close="activeSheet = null"
      @choose="chooseAttachment"
      @open-materials="openMaterials"
    />
    <MaterialsSheet
      :open="activeSheet === 'materials'"
      :items="materialItems"
      @close="activeSheet = null"
      @select="selectMaterial"
    />
    <PlanHistorySheet
      :open="activeSheet === 'history'"
      :items="revisionItems"
      @close="activeSheet = null"
    />
    <SourceDetailSheet
      :open="activeSheet === 'source'"
      :title="sourceTitle"
      :excerpt="sourceExcerpt"
      :organization="sourceOrganization"
      :type-label="sourceTypeLabel"
      :published-at="sourcePublishedAt"
      :retrieved-at="sourceRetrievedAt"
      @close="activeSheet = null"
      @open-original="openSourceOriginal"
    />
    <SettingsSheet
      :open="activeSheet === 'settings'"
      :logged-in="loggedIn"
      :reminders-enabled="remindersEnabled"
      @close="activeSheet = null"
      @open-materials="openMaterials"
      @open-history="openHistory"
      @update-reminders="updateReminders"
      @delete-account="deleteAccount"
      @logout="logout"
    />
  </view>
</template>

<style scoped>
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
.main-view {
  padding: 24rpx 30rpx calc(40rpx + env(safe-area-inset-bottom));
}
.main-view.chat-main {
  padding: 0 22rpx;
}
@media (min-width: 700px) {
  .app-shell {
    min-height: 900px;
  }
}
</style>
