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
      useCursorPagination(fetcher)

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
      useCursorPagination(fetcher)
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
})
