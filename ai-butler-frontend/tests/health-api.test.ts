import { beforeEach, describe, expect, it, vi } from 'vitest'

import { request } from '@/api/client'
import { getLive, getReady } from '@/api/health'

vi.mock('@/api/client', () => ({ request: vi.fn() }))

describe('health API', () => {
  beforeEach(() => vi.mocked(request).mockReset())

  it('requests the liveness endpoint', async () => {
    vi.mocked(request).mockResolvedValue({ status: 'ok' })
    await expect(getLive()).resolves.toEqual({ status: 'ok' })
    expect(request).toHaveBeenCalledWith({ path: '/health/live' })
  })

  it('requests the readiness endpoint', async () => {
    vi.mocked(request).mockResolvedValue({ status: 'ready', checks: { postgres: 'up' } })
    await expect(getReady()).resolves.toEqual({ status: 'ready', checks: { postgres: 'up' } })
    expect(request).toHaveBeenCalledWith({ path: '/health/ready' })
  })
})
