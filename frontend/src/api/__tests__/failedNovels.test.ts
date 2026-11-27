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

  it('resetCount sends failed_id path param and resetAll succeeds on HTTP 204', async () => {
    mocks.POST.mockResolvedValue({
      data: undefined,
      error: undefined,
      response: new Response(null, { status: 204 }),
    })

    await expect(failedNovelApi.resetCount(42)).resolves.toBeUndefined()
    await expect(failedNovelApi.resetAll()).resolves.toBeUndefined()
    expect(mocks.POST).toHaveBeenNthCalledWith(1, '/api/failed-novels/{failed_id}/reset-count', {
      params: { path: { failed_id: 42 } },
    })
    expect(mocks.POST).toHaveBeenNthCalledWith(2, '/api/failed-novels/reset-count')
  })

  it('retry sends body { ids } and returns task metadata', async () => {
    mocks.POST.mockResolvedValue({
      data: { task_id: 77, matched: 2 },
      error: undefined,
      response: new Response(null, { status: 200 }),
    })

    await expect(failedNovelApi.retry([10, 20])).resolves.toEqual({ task_id: 77, matched: 2 })
    expect(mocks.POST).toHaveBeenCalledWith('/api/failed-novels/retry', {
      body: { ids: [10, 20] },
    })
  })

  it('resetCount, resetAll, and retry throw ApiError on error response', async () => {
    mocks.POST.mockResolvedValue({
      data: undefined,
      error: { detail: '重置失败测试' },
      response: new Response(null, { status: 500 }),
    })

    await expect(failedNovelApi.resetCount(42)).rejects.toThrowError(ApiError)
    await expect(failedNovelApi.resetAll()).rejects.toThrow('重置失败测试')
    await expect(failedNovelApi.retry([10])).rejects.toThrowError(ApiError)
  })
})
