import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  POST: vi.fn(),
}))

vi.mock('../client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../client')>()
  return {
    ...actual,
    apiClient: {
      GET: vi.fn(),
      POST: mocks.POST,
    },
  }
})

import { ApiError } from '../errors'
import { failedNovelApi } from '../failedNovels'

describe('failedNovelApi', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('resetCount and resetAll succeed on HTTP 204 with undefined data', async () => {
    mocks.POST.mockResolvedValue({
      data: undefined,
      error: undefined,
      response: new Response(null, { status: 204 }),
    })

    await expect(failedNovelApi.resetCount(42)).resolves.toBeUndefined()
    await expect(failedNovelApi.resetAll()).resolves.toBeUndefined()
    expect(mocks.POST).toHaveBeenCalledTimes(2)
  })

  it('resetCount and resetAll throw ApiError on error response', async () => {
    mocks.POST.mockResolvedValue({
      data: undefined,
      error: { detail: '重置失败测试' },
      response: new Response(null, { status: 500 }),
    })

    await expect(failedNovelApi.resetCount(42)).rejects.toThrowError(ApiError)
    await expect(failedNovelApi.resetAll()).rejects.toThrow('重置失败测试')
  })
})
