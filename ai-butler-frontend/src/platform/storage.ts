const REFRESH_TOKEN_KEY = 'ai-butler.refresh-token.v1'
const DEVICE_ID_KEY = 'ai-butler.device-id.v1'

function randomId(): string {
  const random = Math.random().toString(36).slice(2)
  return `device-${Date.now().toString(36)}-${random}`
}

/**
 * 只持久化恢复登录所需的 refresh token 与设备标识。
 * access token 和 SSE ticket 始终只在内存中存在，且不得进入日志或错误上报。
 */
export const credentialStorage = {
  getRefreshToken(): string | null {
    const value = uni.getStorageSync(REFRESH_TOKEN_KEY)
    return typeof value === 'string' && value ? value : null
  },
  setRefreshToken(value: string): void {
    uni.setStorageSync(REFRESH_TOKEN_KEY, value)
  },
  clearRefreshToken(): void {
    uni.removeStorageSync(REFRESH_TOKEN_KEY)
  },
  getDeviceId(): string {
    const current = uni.getStorageSync(DEVICE_ID_KEY)
    if (typeof current === 'string' && current) return current
    const created = randomId()
    uni.setStorageSync(DEVICE_ID_KEY, created)
    return created
  },
}
