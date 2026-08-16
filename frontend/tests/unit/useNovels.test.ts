import { describe, it, expect, vi, beforeEach } from 'vitest'
import { defineComponent, nextTick } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { useNovels } from '../../src/composables/useNovels'

const novelApiMock = vi.hoisted(() => ({
  getNovels: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  novelApi: novelApiMock,
}))

/** Mount a throwaway component so onMounted/popstate wiring runs for real. */
function mountUseNovels(url = '/') {
  window.history.replaceState({}, '', url)
  const wrapper = mount(
    defineComponent({
      setup() {
        const state = useNovels()
        return { state }
      },
      template: '<div />',
    }),
  )
  return wrapper.vm.state as ReturnType<typeof useNovels>
}

describe('useNovels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    novelApiMock.getNovels.mockResolvedValue({ novels: [{ id: 1 }], cursor: null })
  })

  it('parses filters from the URL on setup', () => {
    const state = mountUseNovels('/?keyword=R-18&order_by=like&min_like=10')

    expect(state.filters.keyword).toBe('R-18')
    expect(state.filters.order_by).toBe('like')
    expect(state.filters.min_like).toBe(10)
    expect(state.filters.min_text).toBeUndefined()
  })

  it('maps the backend {novels, cursor} shape through the fetcher', async () => {
    const state = mountUseNovels()

    await state.loadNovels()

    expect(novelApiMock.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({ per_page: 30, order_by: 'random', order_direction: 'DESC' }),
    )
    expect(state.novels.value).toEqual([{ id: 1 }])
    expect(state.noMoreData.value).toBe(true)
  })

  it('orders by id DESC for author_id searches', async () => {
    const state = mountUseNovels()

    state.handleSearch('author_id:12345;', { setOrdering: true })
    await flushPromises()

    expect(state.filters.order_by).toBe('id')
    expect(state.filters.order_direction).toBe('DESC')
    expect(state.filters.min_like).toBe(0)
    expect(novelApiMock.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({
        keyword: 'author_id:12345;',
        order_by: 'id',
        order_direction: 'DESC',
      }),
    )
  })

  it('orders by id ASC for series searches and id DESC for favourites', async () => {
    const state = mountUseNovels()

    state.handleSearch('series_id:9;', { setOrdering: true })
    await flushPromises()
    expect(state.filters.order_by).toBe('id')
    expect(state.filters.order_direction).toBe('ASC')

    state.handleSearch('is_favourite:true;', { setOrdering: true })
    await flushPromises()
    expect(state.filters.order_direction).toBe('DESC')
  })

  it('falls back to like-DESC ordering for plain keywords', async () => {
    const state = mountUseNovels()

    state.handleSearch('猫', { setOrdering: true })
    await flushPromises()

    expect(state.filters.order_by).toBe('like')
    expect(state.filters.order_direction).toBe('DESC')
  })

  it('treats bare 7+ digit keywords as novel ids', async () => {
    const state = mountUseNovels()

    state.handleSearch('1285180', { setOrdering: true })
    await flushPromises()

    expect(state.filters.order_by).toBe('id')
    expect(novelApiMock.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({ keyword: '1285180' }),
    )
  })

  it('handleCardSearch maps author/series/tag to wire field names and quotes spaced values', async () => {
    const state = mountUseNovels()

    state.handleCardSearch('author', '张三')
    await flushPromises()
    expect(state.filters.keyword).toBe('author_id:张三;')
    expect(novelApiMock.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({ keyword: 'author_id:张三;' }),
    )

    state.handleCardSearch('tag', 'Fate Grand Order')
    await flushPromises()
    expect(state.filters.keyword).toBe('tags:"Fate Grand Order";')
  })

  it('resets filters and reloads from page 1 on search', async () => {
    novelApiMock.getNovels
      .mockResolvedValueOnce({ novels: [{ id: 1 }], cursor: { id: 1 } })
      .mockResolvedValueOnce({ novels: [{ id: 2 }], cursor: null })
    const state = mountUseNovels()

    await state.loadNovels()
    state.handleLoadMore() // handleLoadMore does not return the promise
    await flushPromises()
    expect(state.novels.value.map((n) => n.id)).toEqual([1, 2])

    state.handleSearch('新词')
    await flushPromises()
    expect(state.novels.value.map((n) => n.id)).toEqual([1])
    expect(state.filters.keyword).toBe('新词')
  })

  it('resetFilters restores the default ordering', () => {
    const state = mountUseNovels('/?keyword=R-18')

    state.resetFilters()

    expect(state.filters.keyword).toBe('')
    expect(state.filters.order_by).toBe('random')
    expect(state.filters.order_direction).toBe('DESC')
  })

  it('popstate re-applies URL filters when the tab is visible', async () => {
    const state = mountUseNovels()
    await state.loadNovels()
    novelApiMock.getNovels.mockClear()

    window.history.replaceState({}, '', '/?keyword=pop词&order_by=like')
    window.dispatchEvent(new Event('popstate'))
    await nextTick()
    await flushPromises()

    expect(state.filters.keyword).toBe('pop词')
    expect(state.filters.order_by).toBe('like')
    expect(novelApiMock.getNovels).toHaveBeenCalled()
  })
})
