import { describe, it, expect, vi, beforeEach } from 'vitest'
import { defineComponent, nextTick } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { useNovels } from '../../src/composables/useNovels'

const novelApiMock = vi.hoisted(() => ({
  getNovels: vi.fn(),
  countNovels: vi.fn(),
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
    novelApiMock.getNovels.mockResolvedValue({ novels: [{ id: 1 }], cursor: null, has_excluded: false })
    novelApiMock.countNovels.mockResolvedValue({ total: 1, excluded: 0 })
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

  it('pushes one history entry per distinct search URL', async () => {
    const state = mountUseNovels('/')
    const entriesBefore = window.history.length

    state.handleSearch('第一词')
    await flushPromises()
    expect(window.location.search).toContain('keyword=')
    expect(window.history.length).toBe(entriesBefore + 1)

    state.handleSearch('第二词')
    await flushPromises()
    expect(window.history.length).toBe(entriesBefore + 2)
  })

  it('does not push a duplicate entry when the search URL is unchanged', async () => {
    const state = mountUseNovels('/')
    const entriesBefore = window.history.length

    state.handleSearch('重复词')
    await flushPromises()
    state.handleSearch('重复词')
    await flushPromises()

    expect(window.history.length).toBe(entriesBefore + 1)
  })

  it('restores the previous search when navigating back', async () => {
    const state = mountUseNovels('/')
    state.handleSearch('第一词')
    await flushPromises()
    const firstUrl = window.location.href

    state.handleSearch('第二词')
    await flushPromises()
    novelApiMock.getNovels.mockClear()

    // Browser Back: the session history returns to the first search URL.
    window.history.replaceState({}, '', firstUrl)
    window.dispatchEvent(new Event('popstate'))
    await nextTick()
    await flushPromises()

    expect(state.filters.keyword).toBe('第一词')
    expect(novelApiMock.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({ keyword: '第一词' }),
    )
  })

  it('excludes blocked tags by default and no longer fetches counts after searches', async () => {
    const state = mountUseNovels('/?keyword=猫')

    await state.loadNovels()
    await flushPromises()

    expect(novelApiMock.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({ exclude_blocked: true }),
    )
    // The ExclusionBar is now a lazy "查看被隐藏的小说" action (its data
    // comes from /blocked-ids on click) — a search must not fire /count.
    expect(novelApiMock.countNovels).not.toHaveBeenCalled()
  })

  it('never fetches counts on random browse (no keyword)', async () => {
    const state = mountUseNovels('/')

    await state.loadNovels()
    await flushPromises()

    expect(novelApiMock.countNovels).not.toHaveBeenCalled()
  })

  it('exposes hasExcluded from the first page response', async () => {
    novelApiMock.getNovels.mockResolvedValue({
      novels: [{ id: 1 }], cursor: null, has_excluded: true,
    })
    const state = mountUseNovels('/?keyword=猫')

    await state.loadNovels()
    await flushPromises()

    expect(state.hasExcluded.value).toBe(true)
  })

  it('does not overwrite hasExcluded on load-more', async () => {
    novelApiMock.getNovels
      .mockResolvedValueOnce({
        novels: [{ id: 1 }], cursor: { id: 1 }, has_excluded: true,
      })
      .mockResolvedValueOnce({
        novels: [{ id: 2 }], cursor: null, has_excluded: false,
      })
    const state = mountUseNovels('/?keyword=猫')

    await state.loadNovels()
    expect(state.hasExcluded.value).toBe(true)

    state.handleLoadMore() // handleLoadMore does not return the promise
    await flushPromises()
    // load-more 响应即使返回 false 也不覆盖首屏 true
    expect(state.hasExcluded.value).toBe(true)
  })

  /**
   * 双请求回归。
   *
   * 实测（真实 Firefox + BiDi 数 `/api/novels/` 请求）：在搜索框里键入
   * `is_special_follow:true;` 会发 **2 次 URL 完全相同**的请求，两次串行
   * （23ms + 21ms），第二次的结果覆盖第一次——白屏时间翻倍。
   *
   * 根因在 App 层：`handleSearch` 同步赋值 keyword 并取数，而 keyword watcher
   * 在 flush 时才跑，看到同一个 keyword 又走了一遍 `applySection` → `handleSearch`。
   *
   * `lastFetchedKeyword` 就是「这个 keyword 已经取过数了」的记号，watcher 据此
   * 只补 section 的阈值/排序，不再取数。这条用例钉住那个记号本身。
   */
  describe('lastFetchedKeyword：区分「取过数的 keyword」与「外部改写」', () => {
    it('handleSearch 之后被记为该 keyword 已取数', async () => {
      const state = mountUseNovels('/')
      await flushPromises()

      expect(state.lastFetchedKeyword.value).toBeNull()
      await state.handleSearch('is_special_follow:true;', { setOrdering: true })
      await flushPromises()

      expect(state.lastFetchedKeyword.value).toBe('is_special_follow:true;')
    })

    it('只赋 filters.keyword、不调 handleSearch 时记号不变（侧边栏那条路径）', async () => {
      const state = mountUseNovels('/')
      await flushPromises()

      // 侧边栏点击 = 只赋 keyword，靠 watcher 取数。此时记号不该被设置，
      // 否则 watcher 会误以为「已经取过」而跳过取数 → 点了书架什么都不加载。
      state.filters.keyword = 'is_special_follow:true;'
      await flushPromises()

      expect(state.lastFetchedKeyword.value).toBeNull()
    })

    it('每次 handleSearch 都刷新记号（连续搜两个词）', async () => {
      const state = mountUseNovels('/')
      await flushPromises()

      await state.handleSearch('R-18')
      await flushPromises()
      expect(state.lastFetchedKeyword.value).toBe('R-18')

      await state.handleSearch('猫')
      await flushPromises()
      expect(state.lastFetchedKeyword.value).toBe('猫')
    })
  })

  /**
   * 同一串 keyword 走哪条入口，都必须得到**同一个列表**。
   *
   * 实测发现的真实缺陷（真实 Firefox + BiDi）：点侧边栏「我的上传」发的是
   * `order_by=id, order_direction=DESC`；但把 `source:local;` 敲进搜索框，
   * 发的是 `order_by=like, order_direction=DESC` —— 因为
   * `applySearchOrdering` 是一串手写的 `type === '...'` 分支，`source` 漏了。
   *
   * 后果不是"慢"，是**同一个书架两条入口给出不同顺序的列表**，而且阈值也不同。
   * 修法是让排序/阈值直接读 `SECTIONS` 表（与侧边栏同源），而不是在
   * `useNovels` 里再猜一遍。
   */
  describe('书架 keyword 的排序与阈值：搜索框入口必须与侧边栏同源', () => {
    const cases: Array<[string, string, string]> = [
      ['source:local;', 'id', 'DESC'],
      ['is_special_follow:true;', 'id', 'DESC'],
      ['is_favourite:true;', 'id', 'DESC'],
    ]

    for (const [keyword, orderBy, direction] of cases) {
      it(`「${keyword}」→ order_by=${orderBy} ${direction}、阈值归零`, async () => {
        const state = mountUseNovels('/')
        await flushPromises()

        await state.handleSearch(keyword, { setOrdering: true })
        await flushPromises()

        expect(state.filters.order_by).toBe(orderBy)
        expect(state.filters.order_direction).toBe(direction)
        // 书架是「我的完整清单」，门槛必须 0/0（LIBRARY_DESIGN §5，
        // 历史回归 32a13bb：照抄 500/3000 藏掉 1395 篇）
        expect(state.filters.min_like).toBe(0)
        expect(state.filters.min_text).toBe(0)
      })
    }

    it('自由搜索（非书架 keyword）仍然落到 like DESC + 阈值归零', async () => {
      const state = mountUseNovels('/')
      await flushPromises()

      await state.handleSearch('猫', { setOrdering: true })
      await flushPromises()

      expect(state.filters.order_by).toBe('like')
      expect(state.filters.order_direction).toBe('DESC')
      expect(state.filters.min_like).toBe(0)
    })

    it('作者下钻仍按 id DESC（这条本来就有，不许被表改动碰坏）', async () => {
      const state = mountUseNovels('/')
      await flushPromises()

      await state.handleSearch('author_id:12345;', { setOrdering: true })
      await flushPromises()

      expect(state.filters.order_by).toBe('id')
      expect(state.filters.order_direction).toBe('DESC')
    })
  })
})
