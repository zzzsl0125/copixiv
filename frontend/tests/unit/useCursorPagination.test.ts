import { describe, it, expect, vi } from 'vitest'
import { nextTick } from 'vue'
import { useCursorPagination } from '../../src/composables/useCursorPagination'

/** Pins the frontend ↔ backend cursor contract: fetcher returns
 * `{items, cursor}`; a truthy cursor means "more pages", null ends paging. */
describe('useCursorPagination', () => {
  it('loads the first page and tracks the cursor', async () => {
    const fetcher = vi.fn().mockResolvedValue({
      items: [{ id: 1 }, { id: 2 }],
      cursor: { id: 2 },
    })
    const { items, cursor, noMoreData, loadData } = useCursorPagination(fetcher)

    await loadData()

    expect(items.value).toHaveLength(2)
    expect(cursor.value).toEqual({ id: 2 })
    expect(noMoreData.value).toBe(false)
    expect(fetcher).toHaveBeenCalledWith(undefined)
  })

  it('appends on load-more and ends when cursor is null', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce({ items: [{ id: 1 }], cursor: { id: 1 } })
      .mockResolvedValueOnce({ items: [{ id: 2 }], cursor: null })

    const { items, cursor, noMoreData, loadData, handleLoadMore } =
      useCursorPagination<{ id: number }>(fetcher)

    await loadData()
    await handleLoadMore()

    expect(items.value.map((i) => i.id)).toEqual([1, 2])
    expect(cursor.value).toBeNull()
    expect(noMoreData.value).toBe(true)
    // load-more passes the previous cursor back
    expect(fetcher).toHaveBeenLastCalledWith({ id: 1 })
  })

  it('does not fetch again once noMoreData is set', async () => {
    const fetcher = vi.fn().mockResolvedValue({ items: [], cursor: null })
    const { handleLoadMore, loadData } = useCursorPagination(fetcher)

    await loadData()
    await handleLoadMore()
    await handleLoadMore()

    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('reset clears items and cursor', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce({ items: [{ id: 1 }], cursor: { id: 1 } })
      .mockResolvedValue({ items: [{ id: 9 }], cursor: null })

    const { items, cursor, loadData, reset, handleLoadMore } =
      useCursorPagination<{ id: number }>(fetcher)
    await loadData()

    reset()
    await nextTick()
    expect(items.value).toEqual([])
    expect(cursor.value).toBeNull()

    await handleLoadMore()
    expect(items.value.map((i) => i.id)).toEqual([9])
  })

  it('surfaces fetch errors without throwing', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('network down'))
    const { error, loadData } = useCursorPagination(fetcher)

    await loadData()

    expect(error.value).toMatch(/network down/)
  })

  it('reloads from the beginning when a reset arrives while a fetch is in flight', async () => {
    let resolveFirst!: (value: { items: { id: number }[]; cursor: unknown }) => void
    const fetcher = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<{ items: { id: number }[]; cursor: unknown }>((resolve) => {
            resolveFirst = resolve
          }),
      )
      .mockResolvedValueOnce({ items: [{ id: 2 }], cursor: null })

    const { items, loadData, reset } = useCursorPagination<{ id: number }>(fetcher)

    const firstLoad = loadData()
    reset()
    await loadData() // pre-fix: swallowed by the loading guard

    resolveFirst({ items: [{ id: 1 }], cursor: { id: 1 } })
    await firstLoad

    await vi.waitFor(() => {
      expect(fetcher).toHaveBeenCalledTimes(2)
      expect(items.value.map((i) => i.id)).toEqual([2])
    })
  })

  /**
   * 列表上限（`DEFAULT_MAX_ITEMS`）。
   *
   * 为什么要有这组断言：`LoadMore.vue` 是哨兵式自动加载（`rootMargin: 100px`，
   * 滚到底就自动再取），没有上限时"一直往下滚"能把列表堆到上千条。实测（真实
   * Firefox）600 张卡 = 21,497 个 DOM 节点、跨列断点重排时主线程单帧卡 1,289ms。
   * 上限就是给这个数字一个硬边界。
   */
  describe('列表上限（maxItems）', () => {
    it('越过上限时从头部裁掉，长度恒定等于上限', async () => {
      const fetcher = vi
        .fn()
        .mockResolvedValueOnce({ items: [1, 2, 3], cursor: { id: 3 } })
        .mockResolvedValueOnce({ items: [4, 5, 6], cursor: { id: 6 } })
      const { items, handleLoadMore, loadData } = useCursorPagination<number>(
        fetcher, { maxItems: 4 },
      )

      await loadData()
      expect(items.value).toEqual([1, 2, 3])

      await handleLoadMore()
      // 3 + 3 = 6，上限 4 → 丢掉最老的 2 条：只剩 4 条，且丢的是**头部**
      expect(items.value).toHaveLength(4)
      expect(items.value).toEqual([3, 4, 5, 6])
      // 头部：最老的 1、2 被裁掉
      expect(items.value[0]).toBe(3)
      // 尾部：最新的 6 原样保留，裁剪绝不从尾部删
      expect(items.value[items.value.length - 1]).toBe(6)
    })

    it('没越界时一条都不裁，头部原样保留', async () => {
      const fetcher = vi
        .fn()
        .mockResolvedValueOnce({ items: [1, 2], cursor: { id: 2 } })
        .mockResolvedValueOnce({ items: [3], cursor: { id: 3 } })
      const { items, loadData, handleLoadMore } = useCursorPagination<number>(
        fetcher, { maxItems: 10 },
      )

      await loadData()
      await handleLoadMore()

      // 总共 3 条 < 上限 10，一条都不该丢：头部仍是第 1 条
      expect(items.value).toHaveLength(3)
      expect(items.value).toEqual([1, 2, 3])
      expect(items.value[0]).toBe(1)
    })

    it('裁剪不动 cursor：下一页仍从正确的游标取', async () => {
      const fetcher = vi
        .fn()
        .mockResolvedValueOnce({ items: [1, 2], cursor: { id: 2 } })
        .mockResolvedValueOnce({ items: [3, 4], cursor: { id: 4 } })
        .mockResolvedValueOnce({ items: [5], cursor: null })
      const { handleLoadMore, loadData, items } = useCursorPagination<number>(
        fetcher, { maxItems: 3 },
      )

      await loadData()
      await handleLoadMore()
      // 现在 items = [2, 3, 4]（裁掉 1）
      expect(items.value).toEqual([2, 3, 4])

      await handleLoadMore()
      // cursor 必须还是 4（不是被裁后的任何东西）
      expect(fetcher).toHaveBeenLastCalledWith({ id: 4 })
      // [2,3,4] + [5] = 4 条，上限 3 → 裁掉最老的 1 条（那只剩 2）
      expect(items.value).toEqual([3, 4, 5])
    })

    it('没配 maxItems 时行为与旧实现完全一致（无限累积）', async () => {
      const fetcher = vi
        .fn()
        .mockResolvedValueOnce({ items: [1, 2], cursor: { id: 2 } })
        .mockResolvedValueOnce({ items: [3, 4], cursor: { id: 4 } })
      const { items, loadData, handleLoadMore } = useCursorPagination<number>(fetcher)

      await loadData()
      await handleLoadMore()

      expect(items.value).toEqual([1, 2, 3, 4])
    })

    it('reset() 清掉裁剪后的状态，重新从第一页开始', async () => {
      const fetcher = vi
        .fn()
        .mockResolvedValueOnce({ items: [1, 2, 3], cursor: { id: 3 } })
        .mockResolvedValueOnce({ items: [4, 5, 6], cursor: { id: 6 } })
        .mockResolvedValueOnce({ items: [7, 8], cursor: null })
      const { items, cursor, loadData, handleLoadMore, reset } = useCursorPagination<number>(
        fetcher, { maxItems: 4 },
      )

      await loadData()
      await handleLoadMore()
      // 裁剪后：头部被裁、列表有界、cursor 仍指向尾部之后
      expect(items.value).toEqual([3, 4, 5, 6])
      expect(cursor.value).toEqual({ id: 6 })

      reset()
      // 裁剪留下的痕迹（有界列表与游标）一起清掉，回到未加载状态
      expect(items.value).toEqual([])
      expect(cursor.value).toBeNull()

      // 重置后继续 load-more 从第一页重新取，不被上一次的裁剪状态影响
      await handleLoadMore()
      expect(fetcher).toHaveBeenLastCalledWith(null)
      expect(items.value).toEqual([7, 8])
    })
  })
})
