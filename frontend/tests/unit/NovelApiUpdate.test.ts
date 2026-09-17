import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * `novelApi.updateNovel` 的 wire 契约（docs/NOVEL_EDIT_DESIGN.md §5）。
 *
 * - `PATCH /novels/{id}`（baseURL `/api`）；
 * - 请求体**只含变化字段**（PATCH 的"未提交 = 不动"是编辑器不误伤其它字段的
 *   唯一保证）；显式 `null` 表示清空；
 * - 响应透传更新后的完整对象（调用方据此原地替换卡片，不重取列表）。
 */

const clientMock = vi.hoisted(() => ({ patch: vi.fn() }))

vi.mock('../../src/api/client', () => ({
  apiClient: { patch: clientMock.patch },
  buildApiUrl: (p: string) => p,
  apiBaseUrl: '/api',
}))

async function loadApi() {
  vi.resetModules()
  return import('../../src/api/novels')
}

describe('novelApi.updateNovel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('PATCH /novels/{id}，只提交变化字段，响应透传', async () => {
    const updated = {
      id: -999000001,
      title: '新书名',
      author_name: '新作者',
      tags: ['a'],
      source: 'local',
    }
    clientMock.patch.mockResolvedValue({ data: updated })
    const { novelApi } = await loadApi()

    const result = await novelApi.updateNovel(-999000001, {
      title: '新书名',
      tags: ['a'],
    })

    expect(clientMock.patch).toHaveBeenCalledTimes(1)
    expect(clientMock.patch.mock.calls[0][0]).toBe('/novels/-999000001')
    expect(clientMock.patch.mock.calls[0][1]).toEqual({ title: '新书名', tags: ['a'] })
    expect(result).toEqual(updated)
  })

  it('显式 null 原样发出（PATCH 的"清空"三态之一）', async () => {
    clientMock.patch.mockResolvedValue({ data: { id: 42 } })
    const { novelApi } = await loadApi()

    await novelApi.updateNovel(42, { series_name: null, create_time: null })

    expect(clientMock.patch.mock.calls[0][1]).toEqual({
      series_name: null,
      create_time: null,
    })
  })
})
