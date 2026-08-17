import { computed, ref, type Ref } from 'vue'
import { storeToRefs } from 'pinia'

import { butlerApi, type ApiObject, type CitationResponse } from '@/api/butler'
import { chooseFile } from '@/platform/files'
import { openSourceLink } from '@/platform/source-links'
import { useAuthStore } from '@/stores/auth'
import { useButlerStore } from '@/stores/butler'
import type { MainTab, SheetName, UploadedAttachment } from '@/types/view-models'

/** 管理上传、资料、引用和账号设置等资源型页面状态。 */
export function usePageResources(
  activeTab: Ref<MainTab>,
  activeSheet: Ref<SheetName>,
  token: () => string,
) {
  const auth = useAuthStore()
  const butler = useButlerStore()
  const { plans } = storeToRefs(butler)
  const remindersEnabled = ref(true)
  const reminderVersion = ref(1)
  const attachments = ref<UploadedAttachment[]>([])
  const uploading = ref(false)
  const sourceDetail = ref<CitationResponse | null>(null)
  const materialItems = ref<ApiObject[]>([])
  const revisionItems = ref<ApiObject[]>([])

  const sourceTitle = computed(() => sourceDetail.value?.title || '来源详情')
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

  async function loadPreferences(): Promise<void> {
    const response = await butlerApi.preferences(token())
    reminderVersion.value = typeof response.version === 'number' ? response.version : 1
    const reminder =
      typeof response.task_reminder === 'object' && response.task_reminder !== null
        ? (response.task_reminder as ApiObject)
        : {}
    remindersEnabled.value = reminder.enabled !== false
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
      uni.showToast({
        title: error instanceof Error ? error.message : '设置保存失败',
        icon: 'none',
      })
    }
  }

  async function openSource(citationId: string): Promise<void> {
    if (!citationId) return
    try {
      sourceDetail.value = await butlerApi.citation(citationId, token())
      activeSheet.value = 'source'
    } catch (error) {
      uni.showToast({
        title: error instanceof Error ? error.message : '来源加载失败',
        icon: 'none',
      })
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
      uni.showToast({
        title: error instanceof Error ? error.message : '资料加载失败',
        icon: 'none',
      })
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
    const access = sourceDetail.value?.access
    if (
      typeof access?.url !== 'string' ||
      (access.type !== 'EXTERNAL_URL' && access.type !== 'SIGNED_FILE')
    ) {
      uni.showToast({ title: '当前来源没有可打开的原文', icon: 'none' })
      return
    }
    try {
      await openSourceLink(access.url, access.type)
      if (access.type === 'EXTERNAL_URL') {
        // #ifndef H5
        uni.showToast({ title: '来源地址已复制', icon: 'none' })
        // #endif
      }
    } catch (error) {
      uni.showToast({
        title: error instanceof Error ? error.message : '来源打开失败',
        icon: 'none',
      })
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
      uni.showToast({
        title: error instanceof Error ? error.message : '版本加载失败',
        icon: 'none',
      })
    }
  }

  function removeAttachment(fileId: string): void {
    attachments.value = attachments.value.filter((item) => item.id !== fileId)
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

  return {
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
  }
}
