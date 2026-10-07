import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * `novelApi.batchMetadata` 的 wire 契约（`POST /novels/batch-metadata`）。
 *
 * 字段名必须与后端一字不差：`scope` / `author_id` / `series_id` / `apply`（请求）
 * 与 `matched` / `applicable` / `skipped` / `affected`（响应）。这条端点的三态是
 * "**键不出现 = 不改**，显式 `null` = 解除关联"，所以"留空"绝不能漏成 `undefined`
 * 之外的形态（例如空串或 0）——拼错字段名不会报错，只会静默不生效。
 *
 * 预演与真执行是同一个端点、同一个响应形状（`apply` 只是开关）。
 */

const clientMock = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn(), patch: vi.fn() }))

vi.mock('../../src/api/client', () => ({
  apiClient: { post: clientMock.post, get: clientMock.get, patch: clientMock.patch },
  buildApiUrl: (p: string) => p,
  apiBaseUrl: '/api',
}))

async function loadApi() {
  vi.resetModules()
  return import('../../src/api/novels')
}

const SCOPE = { mode: 'ids' as const, novel_ids: [1, 2, 3], excluded_ids: [] }

describe('novelApi.batchMetadata', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('POST /novels/batch-metadata：预演（apply:false）原样发出，响应透传', async () => {
    const preview = { matched: 10, applicable: 6, skipped: 4, affected: 0 }
    clientMock.post.mockResolvedValue({ data: preview })
    const { novelApi } = await loadApi()

    const result = await novelApi.batchMetadata({ scope: SCOPE, apply: false })

    expect(clientMock.post).toHaveBeenCalledTimes(1)
    expect(clientMock.post.mock.calls[0][0]).toBe('/novels/batch-metadata')
    expect(clientMock.post.mock.calls[0][1]).toEqual({ scope: SCOPE, apply: false })
    expect(result).toEqual(preview)
  })

  it('真执行：apply:true + author_id / series_id 的正整数值', async () => {
    const applied = { matched: 10, applicable: 6, skipped: 4, affected: 6 }
    clientMock.post.mockResolvedValue({ data: applied })
    const { novelApi } = await loadApi()

    const result = await novelApi.batchMetadata({
      scope: SCOPE,
      author_id: 777,
      series_id: 888,
      apply: true,
    })

    expect(clientMock.post.mock.calls[0][1]).toEqual({
      scope: SCOPE,
      author_id: 777,
      series_id: 888,
      apply: true,
    })
    expect(result).toEqual(applied)
  })

  it('显式 null 原样发出（解除关联的唯一表达）；未提交的键不出现', async () => {
    clientMock.post.mockResolvedValue({
      data: { matched: 1, applicable: 1, skipped: 0, affected: 1 },
    })
    const { novelApi } = await loadApi()

    await novelApi.batchMetadata({ scope: SCOPE, author_id: null, apply: true })

    const body = clientMock.post.mock.calls[0][1] as Record<string, unknown>
    expect(body.author_id).toBeNull()
    // 键不出现 = 不改系列（PUT 式三态里最容易被写成 undefined / 空串的那一格）
    expect('series_id' in body).toBe(false)
    expect(Object.keys(body).sort()).toEqual(['apply', 'author_id', 'scope'])
  })
})
