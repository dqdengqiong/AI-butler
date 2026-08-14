import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  login: vi.fn(),
  refresh: vi.fn(),
  logout: vi.fn(),
}))
vi.mock('@/api/butler', () => ({ butlerApi: api }))

import { useAuthStore } from '@/stores/auth'

const response = {
  access_token: 'access-token',
  refresh_token: 'session.secret',
  user: { id: 'user-id', nickname: '小邓', timezone: 'Asia/Shanghai' },
}

describe('auth lifecycle', () => {
  const storage = new Map<string, string>()

  beforeEach(() => {
    setActivePinia(createPinia())
    storage.clear()
    api.login.mockReset().mockResolvedValue(response)
    api.refresh.mockReset().mockResolvedValue(response)
    api.logout.mockReset().mockResolvedValue(undefined)
    vi.stubGlobal('uni', {
      login: ({ success }: { success: (value: { code: string }) => void }) =>
        success({ code: 'wx-code' }),
      getStorageSync: (key: string) => storage.get(key),
      setStorageSync: (key: string, value: string) => storage.set(key, value),
      removeStorageSync: (key: string) => storage.delete(key),
    })
  })

  it('logs in, persists only refresh credentials and logs out', async () => {
    const auth = useAuthStore()
    await auth.login()
    expect(auth.authenticated).toBe(true)
    expect(auth.user?.nickname).toBe('小邓')
    expect([...storage.values()]).not.toContain('access-token')
    await auth.logout()
    expect(api.logout).toHaveBeenCalled()
    expect(auth.authenticated).toBe(false)
  })

  it('restores a rotating session and clears invalid credentials', async () => {
    storage.set('ai-butler.refresh-token.v1', 'old.secret')
    const auth = useAuthStore()
    await expect(auth.restore()).resolves.toBe(true)
    expect(api.refresh).toHaveBeenCalled()

    api.refresh.mockRejectedValueOnce(new Error('reused'))
    await expect(auth.restore()).resolves.toBe(false)
    expect(auth.accessToken).toBeNull()
  })

  it('does not call refresh or logout without persisted credentials', async () => {
    const auth = useAuthStore()
    await expect(auth.restore()).resolves.toBe(false)
    await auth.logout()
    expect(api.refresh).not.toHaveBeenCalled()
    expect(api.logout).not.toHaveBeenCalled()
  })
})
