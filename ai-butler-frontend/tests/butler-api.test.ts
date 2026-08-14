import { beforeEach, describe, expect, it, vi } from 'vitest'

const requestMock = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
vi.mock('@/api/client', () => ({ request: requestMock }))

import { butlerApi } from '@/api/butler'

describe('business API facade', () => {
  beforeEach(() => requestMock.mockClear())

  it('covers authenticated read and mutation endpoints', async () => {
    const token = 'access'
    const calls = [
      butlerApi.login({} as never),
      butlerApi.refresh({} as never),
      butlerApi.logout({} as never, token),
      butlerApi.me(token),
      butlerApi.dashboard(token),
      butlerApi.plans(token),
      butlerApi.tasks(token),
      butlerApi.executeTask('task/id', {} as never, token),
      butlerApi.agentDefinitions(token),
      butlerApi.conversations(token),
      butlerApi.createConversation({} as never, token),
      butlerApi.conversation('conversation/id', token),
      butlerApi.messages('conversation/id', token),
      butlerApi.sendMessage('conversation/id', {} as never, token),
      butlerApi.run('run/id', token),
      butlerApi.streamTicket('run/id', token),
      butlerApi.approve('approval/id', {} as never, token),
      butlerApi.preferences(token),
      butlerApi.updatePreferences({} as never, token),
      butlerApi.deleteAccount(token),
      butlerApi.files(token),
      butlerApi.createUpload({} as never, token),
      butlerApi.completeUpload('file/id', {} as never, token),
      butlerApi.citation('citation/id', token),
    ]
    await Promise.all(calls)
    expect(requestMock).toHaveBeenCalledTimes(calls.length)
    expect(requestMock).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/v1/me/preferences', method: 'PATCH' }),
    )
    expect(requestMock).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/v1/me', method: 'DELETE' }),
    )
    expect(requestMock).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/v1/citations/citation%2Fid' }),
    )
    expect(requestMock).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/v1/conversations/conversation%2Fid/messages?limit=50' }),
    )
  })

  it('uploads raw bytes and rejects failed upload status', async () => {
    const request = vi.fn().mockImplementationOnce(({ success }) => success({ statusCode: 204 }))
    vi.stubGlobal('uni', { request })
    await expect(
      butlerApi.putUpload(
        'https://upload.example/item',
        { 'Content-Type': 'text/plain' },
        new Uint8Array([1]),
      ),
    ).resolves.toBeUndefined()

    request.mockImplementationOnce(({ success }) => success({ statusCode: 500 }))
    await expect(
      butlerApi.putUpload('https://upload.example/item', {}, new Uint8Array([1])),
    ).rejects.toThrow('500')

    request.mockImplementationOnce(({ fail }) => fail({ errMsg: 'offline' }))
    await expect(
      butlerApi.putUpload('https://upload.example/item', {}, new Uint8Array([1])),
    ).rejects.toThrow('offline')
  })
})
