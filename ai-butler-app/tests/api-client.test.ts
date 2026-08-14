import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, request } from '@/api/client'

interface MockRequestOptions {
  header: Record<string, string>
  success: (response: { statusCode: number; data: unknown }) => void
  fail: (error: { errMsg: string }) => void
}

describe('API client', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('returns successful data and attaches authentication', async () => {
    const requestMock = vi.fn((options: MockRequestOptions) => {
      expect(options.header.Authorization).toBe('Bearer access-token')
      options.success({ statusCode: 200, data: { status: 'ok' } })
    })
    vi.stubGlobal('uni', { request: requestMock })
    await expect(
      request<{ status: string }>({ path: '/health/live', accessToken: 'access-token' }),
    ).resolves.toEqual({ status: 'ok' })
  })

  it('maps non-success responses to ApiError', async () => {
    vi.stubGlobal('uni', {
      request(options: MockRequestOptions) {
        options.success({ statusCode: 403, data: { code: 'FORBIDDEN' } })
      },
    })
    await expect(request({ path: '/private' })).rejects.toEqual(
      new ApiError(403, { code: 'FORBIDDEN' }),
    )
  })

  it('maps transport failures without exposing request data', async () => {
    vi.stubGlobal('uni', {
      request(options: MockRequestOptions) {
        options.fail({ errMsg: 'network unavailable' })
      },
    })
    await expect(request({ path: '/health/live' })).rejects.toThrow('network unavailable')
  })
})
