import { describe, it, expect, vi, beforeEach } from 'vitest'
import { usePagedNovelIdView } from '../../src/composables/usePagedNovelIdView'

const novelApiMock = vi.hoisted(() => ({
  getNovelsByIds: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  novelApi: novelApiMock,
}))

const PAGES = 30

describe('usePagedNovelIdView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('loads the first page on start and paginates on loadMore', async () => {
    novelApiMock.getNovelsByIds.mockImplementation(async (ids: number[]) => ({
      novels: ids.slice(0, 2).map((id) => ({ id })),
      truncated: false,
    }))

    const view = usePagedNovelIdView()
    const ids = [1, 2, 3]
    await view.start(ids)

    expect(novelApiMock.getNovelsByIds).toHaveBeenCalledWith([1, 2, 3])
    expect(view.novels.value.map((n) => n.id)).toEqual([1, 2])
    expect(view.hasMore.value).toBe(false)
    expect(view.totalIds.value).toBe(3)
  })

  it('pages in chunks of 30 ids', async () => {
    novelApiMock.getNovelsByIds.mockImplementation(async (ids: number[]) => ({
      novels: ids.slice(0, 1).map((id) => ({ id })),
      truncated: false,
    }))

    const view = usePagedNovelIdView()
    const ids = Array.from({ length: PAGES + 7 }, (_, i) => i + 1)
    await view.start(ids)

    expect(novelApiMock.getNovelsByIds).toHaveBeenCalledTimes(1)
    expect(novelApiMock.getNovelsByIds).toHaveBeenCalledWith(
      ids.slice(0, PAGES),
    )
    expect(view.hasMore.value).toBe(true)

    await view.loadMore()
    expect(novelApiMock.getNovelsByIds).toHaveBeenCalledTimes(2)
    expect(novelApiMock.getNovelsByIds).toHaveBeenLastCalledWith(
      ids.slice(PAGES),
    )
    expect(view.hasMore.value).toBe(false)
  })

  it('reset clears everything', async () => {
    novelApiMock.getNovelsByIds.mockResolvedValue({
      novels: [{ id: 1 }],
      truncated: false,
    })
    const view = usePagedNovelIdView()
    await view.start([1, 2])

    view.reset()
    expect(view.ids.value).toEqual([])
    expect(view.novels.value).toEqual([])
    expect(view.totalIds.value).toBe(0)
    expect(view.hasMore.value).toBe(false)
  })

  it('start with empty list is a no-op', async () => {
    const view = usePagedNovelIdView()
    await view.start([])
    expect(novelApiMock.getNovelsByIds).not.toHaveBeenCalled()
    expect(view.loading.value).toBe(false)
  })
})
