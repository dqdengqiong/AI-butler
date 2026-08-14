import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useAuthStore } from '@/stores/auth'

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('uni', {
      getStorageSync: vi.fn(),
      removeStorageSync: vi.fn(),
      setStorageSync: vi.fn(),
    })
  })

  it('normalizes and clears an access token', () => {
    const auth = useAuthStore()
    auth.setAccessToken(' token-value ')
    expect(auth.accessToken).toBe('token-value')
    expect(auth.authenticated).toBe(false)
    auth.clear()
    expect(auth.authenticated).toBe(false)
  })

  it('rejects an empty token', () => {
    expect(() => useAuthStore().setAccessToken('   ')).toThrow('must not be empty')
  })
})
