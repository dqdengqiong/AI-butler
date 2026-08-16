import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  butlerApi,
  type ApiObject,
  type AuthConfigResponse,
  type PhoneVerificationCodeResponse,
} from '@/api/butler'
import { credentialStorage } from '@/platform/storage'

export interface CurrentUser {
  id: string
  nickname: string
  timezone: string
}

function stringField(object: ApiObject, key: string): string {
  const value = object[key]
  if (typeof value !== 'string' || !value) throw new Error(`invalid ${key} response`)
  return value
}

function userField(object: ApiObject): CurrentUser {
  const value = object.user
  if (typeof value !== 'object' || value === null) throw new Error('invalid user response')
  const user = value as ApiObject
  return {
    id: stringField(user, 'id'),
    nickname: typeof user.nickname === 'string' ? user.nickname : '用户',
    timezone: typeof user.timezone === 'string' ? user.timezone : 'Asia/Shanghai',
  }
}

/**
 * 认证状态只在内存持有 access token。应用启动时使用平台存储中的 rotating
 * refresh token 恢复；刷新失败会清理本地凭据并回到登录页。
 */
export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(null)
  const user = ref<CurrentUser | null>(null)
  const restoring = ref(false)
  const authConfig = ref<AuthConfigResponse | null>(null)
  const configLoading = ref(false)
  const configError = ref<string | null>(null)

  const authenticated = computed(() => accessToken.value !== null && user.value !== null)

  function setAccessToken(token: string): void {
    const normalized = token.trim()
    if (!normalized) throw new Error('access token must not be empty')
    accessToken.value = normalized
  }

  function applyTokens(response: ApiObject): void {
    setAccessToken(stringField(response, 'access_token'))
    credentialStorage.setRefreshToken(stringField(response, 'refresh_token'))
    user.value = userField(response)
  }

  async function loadConfig(): Promise<void> {
    configLoading.value = true
    configError.value = null
    try {
      authConfig.value = await butlerApi.authConfig()
    } catch (error) {
      authConfig.value = null
      configError.value = error instanceof Error ? error.message : '登录配置加载失败'
      throw error
    } finally {
      configLoading.value = false
    }
  }

  function consent() {
    const now = new Date().toISOString()
    return {
      terms_version: '2026-08-01',
      privacy_version: '2026-08-01',
      accepted_at: now,
    }
  }

  async function sendPhoneVerificationCode(phone: string): Promise<PhoneVerificationCodeResponse> {
    return butlerApi.sendPhoneVerificationCode({
      schema_version: '1.0',
      phone,
      device_id: credentialStorage.getDeviceId(),
    })
  }

  async function loginWithPhone(
    phone: string,
    challengeId?: string,
    verificationCode?: string,
  ): Promise<void> {
    const deviceId = credentialStorage.getDeviceId()
    const response = await butlerApi.phoneLogin({
      schema_version: '1.0',
      phone,
      device_id: deviceId,
      verification_challenge_id: challengeId,
      verification_code: verificationCode,
      consent: consent(),
    })
    applyTokens(response)
  }

  async function loginWithWechat(loginCode: string, phoneCode: string): Promise<void> {
    const response = await butlerApi.wechatLogin({
      schema_version: '1.0',
      login_code: loginCode,
      phone_code: phoneCode,
      provider: 'WECHAT_MINIAPP',
      device_id: credentialStorage.getDeviceId(),
      consent: consent(),
    })
    applyTokens(response)
  }

  async function restore(): Promise<boolean> {
    const refreshToken = credentialStorage.getRefreshToken()
    if (!refreshToken) return false
    restoring.value = true
    try {
      const response = await butlerApi.refresh({
        schema_version: '1.0',
        refresh_token: refreshToken,
        device_id: credentialStorage.getDeviceId(),
      })
      applyTokens(response)
      return true
    } catch {
      clear()
      return false
    } finally {
      restoring.value = false
    }
  }

  async function logout(): Promise<void> {
    const refreshToken = credentialStorage.getRefreshToken()
    const currentAccess = accessToken.value
    try {
      if (refreshToken && currentAccess) {
        await butlerApi.logout(
          { schema_version: '1.0', refresh_token: refreshToken },
          currentAccess,
        )
      }
    } finally {
      clear()
    }
  }

  function clear(): void {
    accessToken.value = null
    user.value = null
    credentialStorage.clearRefreshToken()
  }

  return {
    accessToken,
    user,
    restoring,
    authConfig,
    configLoading,
    configError,
    authenticated,
    setAccessToken,
    loadConfig,
    sendPhoneVerificationCode,
    loginWithPhone,
    loginWithWechat,
    restore,
    logout,
    clear,
  }
})
