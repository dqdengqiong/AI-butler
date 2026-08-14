import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { butlerApi, type ApiObject } from '@/api/butler'
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
    nickname: typeof user.nickname === 'string' ? user.nickname : '微信用户',
    timezone: typeof user.timezone === 'string' ? user.timezone : 'Asia/Shanghai',
  }
}

function loginCode(deviceId: string): Promise<string> {
  return new Promise((resolve, reject) => {
    // #ifdef MP-WEIXIN
    uni.login({
      provider: 'weixin',
      success: (result) => resolve(result.code),
      fail: () => reject(new Error('微信登录授权失败')),
    })
    // #endif
    // #ifndef MP-WEIXIN
    resolve(`h5-mock-${deviceId}`)
    // #endif
  })
}

/**
 * 认证状态只在内存持有 access token。应用启动时使用平台存储中的 rotating
 * refresh token 恢复；刷新失败会清理本地凭据并回到登录页。
 */
export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(null)
  const user = ref<CurrentUser | null>(null)
  const restoring = ref(false)

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

  async function login(): Promise<void> {
    const deviceId = credentialStorage.getDeviceId()
    const code = await loginCode(deviceId)
    const now = new Date().toISOString()
    const response = await butlerApi.login({
      schema_version: '1.0',
      login_code: code,
      provider: 'WECHAT_MINIAPP',
      device_id: deviceId,
      consent: {
        terms_version: '2026-08-01',
        privacy_version: '2026-08-01',
        accepted_at: now,
      },
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
    authenticated,
    setAccessToken,
    login,
    restore,
    logout,
    clear,
  }
})
