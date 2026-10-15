import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * `novelApi.updateNovel` / `syncNovel` 的 wire 契约（docs/NOVEL_EDIT_DESIGN.md §5）。
 *
 * - `PATCH /novels/{id}`（baseURL `/api`）；
 * - 请求体**只含变化字段**（PATCH 的"未提交 = 不动"是编辑器不误伤其它字段的
 *   唯一保证）；显式 `null` 表示清空；
 * - 响应透传更新后的完整对象（调用方据此原地替换卡片，不重取列表）；
 * - `POST /novels/{id}/sync`：无请求体，响应与 PATCH 同形状（含 `editable_fields`）。
 */

const clientMock = vi.hoisted(() => ({ patch: vi.fn(), get: vi.fn(), post: vi.fn(), delete: vi.fn() }))

vi.mock('../../src/api/client', () => ({
  apiClient: { patch: clientMock.patch, get: clientMock.get, post: clientMock.post, delete: clientMock.delete },
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
      author_id: 777,
      author_name: '新作者',
      tags: ['a'],
      source: 'local',
      // 字段策略也随响应下发（编辑器据此决定下一次置灰哪些字段）
      editable_fields: [
        'title', 'author_id', 'series_id', 'series_index', 'tags', 'create_time', 'caption',
      ],
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

  it('显式 null 原样发出（PATCH 的"清空"三态之一）；简介清空同理', async () => {
    clientMock.patch.mockResolvedValue({ data: { id: 42 } })
    const { novelApi } = await loadApi()

    await novelApi.updateNovel(42, {
      series_index: null, create_time: null, author_id: null, series_id: null, caption: null,
    })

    expect(clientMock.patch.mock.calls[0][1]).toEqual({
      series_index: null,
      create_time: null,
      author_id: null,
      series_id: null,
      caption: null,
    })
  })
})

describe('novelApi.syncNovel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('POST /novels/{id}/sync，无请求体，响应透传（含 editable_fields）', async () => {
    const refreshed = {
      id: 987654,
      title: '来源新标题',
      author_id: 999,
      author_name: '来源新作者',
      series_index: 7,
      tags: ['来源新标签'],
      source: 'pixiv',
      editable_fields: ['series_index', 'tags', 'create_time'],
    }
    clientMock.post.mockResolvedValue({ data: refreshed })
    const { novelApi } = await loadApi()

    const result = await novelApi.syncNovel(987654)

    expect(clientMock.post).toHaveBeenCalledTimes(1)
    expect(clientMock.post.mock.calls[0][0]).toBe('/novels/987654/sync')
    // 无请求体：同步是"以来源为准"，没有可提交的输入
    expect(clientMock.post.mock.calls[0][1]).toBeUndefined()
    expect(result).toEqual(refreshed)
  })
})

describe('novelApi.lookupAuthor / lookupSeries', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('GET /novels/author/{id}，响应透传（编辑器据此显示解析出的名字）', async () => {
    const profile = {
      author_id: 777, author_name: '新作者', source: 'pixiv', novel_count: 2,
    }
    clientMock.get.mockResolvedValue({ data: profile })
    const { novelApi } = await loadApi()

    const result = await novelApi.lookupAuthor(777)

    expect(clientMock.get).toHaveBeenCalledWith('/novels/author/777')
    expect(result).toEqual(profile)
  })

  it('GET /novels/series/{id}，响应透传（系列与作者同构）', async () => {
    const profile = {
      series_id: 888, series_name: '新系列', source: 'local', novel_count: 5,
    }
    clientMock.get.mockResolvedValue({ data: profile })
    const { novelApi } = await loadApi()

    const result = await novelApi.lookupSeries(888)

    expect(clientMock.get).toHaveBeenCalledWith('/novels/series/888')
    expect(result).toEqual(profile)
  })
})

describe('novelApi.deleteNovel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('DELETE /novels/{id}，调用正确 endpoint', async () => {
    clientMock.delete.mockResolvedValue({})
    const { novelApi } = await loadApi()

    await novelApi.deleteNovel(12345)

    expect(clientMock.delete).toHaveBeenCalledTimes(1)
    expect(clientMock.delete).toHaveBeenCalledWith('/novels/12345')
  })
})

