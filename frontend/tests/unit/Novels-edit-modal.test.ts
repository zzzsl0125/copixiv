import { describe, it, expect, vi, beforeEach } from 'vitest'
import { computed, reactive, ref } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import Novels from '../../src/views/Novels.vue'
import type { Novel, NovelFilters } from '../../src/types'

/**
 * 卡片「编辑」→ 弹窗 → 保存 → 列表回写 的接线（docs/NOVEL_EDIT_DESIGN.md §6.1/§6.3）。
 *
 * 这里同时钉住"两份副本都要更新"的分工：
 * - 集合视图（查看已选 / 被排除）的副本由本页 `patchNovel` 负责；
 * - 主列表由 App 负责（`novel-updated`）——测试里用一个监听器模拟
 *   `App.vue::handleNovelUpdated` 的原地 patch，于是断言"卡片真的显示新值"。
 *
 * 真实视图依赖较多，按 `Novels-upload-bar.test.ts` 的办法 mock 组合式函数与 API；
 * 区别是 masonry 这里返回真实卡片（一列），否则看不到卡片的操作条。
 */

const h = vi.hoisted(() => ({
  novels: [] as Novel[],
  updateNovel: vi.fn(),
  patchNovel: vi.fn(),
}))

vi.mock('../../src/composables', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }),
  useSystem: () => ({
    systemConfig: ref(null),
    loading: ref(false),
    error: ref(null),
    fetchConfig: vi.fn(),
  }),
  useCollectionViews: () => ({
    viewingSelected: ref(false),
    viewingExcluded: ref(false),
    selectedView: { novels: ref([]), hasMore: ref(false), loadMore: vi.fn(), removeId: vi.fn() },
    excludedView: { novels: ref([]), hasMore: ref(false), loadMore: vi.fn(), removeId: vi.fn() },
    displayNovels: computed(() => h.novels),
    viewLoading: ref(false),
    viewLoadingMore: ref(false),
    viewHasMore: ref(false),
    viewShownCount: ref(0),
    viewTotalCount: ref(0),
    toggleViewSelected: vi.fn(),
    toggleViewExcluded: vi.fn(),
    handleViewLoadMore: vi.fn(),
    patchNovel: h.patchNovel,
  }),
  // 一列装下全部卡片，测试里才看得见操作条（真实实现按列高分布）
  useMasonryLayout: () => ({ columns: computed(() => [h.novels]), relayout: vi.fn() }),
}))

vi.mock('../../src/api', () => ({
  tagPreferenceApi: { getTagPreferences: vi.fn(async () => []) },
  searchHistoryApi: {
    getSearchHistory: vi.fn(async () => []),
    deleteSearchHistoryItem: vi.fn(async () => undefined),
    clearSearchHistory: vi.fn(async () => undefined),
  },
  novelApi: {
    updateNovel: h.updateNovel,
    toggleFavourite: vi.fn(),
    toggleSpecialFollow: vi.fn(),
    downloadUrl: vi.fn(() => '/api/novels/1/download?format=txt'),
  },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

const FILTERS: NovelFilters = { keyword: '', order_by: 'random', order_direction: 'DESC' }

const LOCAL_NOVEL: Novel = {
  id: -999000001,
  title: '本地书名',
  author_id: undefined as unknown as number,
  author_name: '未知作者',
  like: 0,
  view: 0,
  text: 1234,
  caption: '简介',
  create_time: '2026-09-01T00:00:00+08:00',
  has_epub: 3,
  tags: ['原有标签'],
  is_favourite: 0,
  is_special_follow: 0,
  source: 'local',
}

/** 模拟 App.vue::handleNovelUpdated —— 响应值原地替换主列表里的那一本。 */
function appLikePatch(updated: Novel) {
  const index = h.novels.findIndex((n) => n.id === updated.id)
  if (index >= 0) h.novels[index] = updated
}

function mountNovels(novel: Novel = LOCAL_NOVEL) {
  h.novels = reactive<Novel[]>([{ ...novel }])
  return mount(Novels, {
    props: {
      isSidebarOpen: false,
      filters: FILTERS,
      novelsState: {
        novels: h.novels,
        loading: false,
        error: null,
        noMoreData: true,
        hasExcluded: false,
      },
      onNovelUpdated: appLikePatch,
    },
  })
}

async function openAndSave(
  wrapper: ReturnType<typeof mountNovels>,
  { author }: { author: string },
) {
  await wrapper.get('[data-testid="novel-edit"]').trigger('click')
  await wrapper.get('[data-testid="edit-author"]').setValue(author)
  await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
  await flushPromises()
}

describe('Novels.vue 元数据编辑接线', () => {
  beforeEach(() => {
    h.updateNovel.mockReset()
    h.patchNovel.mockReset()
    // LoadMore 用 IntersectionObserver 做无限滚动；jsdom 没有它，这里给个空壳。
    vi.stubGlobal('IntersectionObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
    })
  })

  it('点卡片「编辑」打开弹窗（初值来自卡片手里的对象，不再发请求）', async () => {
    const wrapper = mountNovels()
    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(false)

    await wrapper.get('[data-testid="novel-edit"]').trigger('click')

    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(true)
    expect((wrapper.get('[data-testid="edit-author"]').element as HTMLInputElement).value)
      .toBe('未知作者')
    expect(h.updateNovel).not.toHaveBeenCalled()
  })

  it('保存后：主列表卡片显示新值、集合视图副本被 patch、弹窗关闭', async () => {
    const updated = { ...LOCAL_NOVEL, author_name: '新作者' }
    h.updateNovel.mockResolvedValue(updated)
    const wrapper = mountNovels()

    await openAndSave(wrapper, { author: '新作者' })

    expect(h.updateNovel).toHaveBeenCalledTimes(1)
    expect(h.updateNovel.mock.calls[0][0]).toBe(LOCAL_NOVEL.id)
    expect(h.updateNovel.mock.calls[0][1]).toEqual({ author_name: '新作者' })
    // 集合视图那两份副本：本页负责
    expect(h.patchNovel).toHaveBeenCalledWith(updated)
    // 主列表：模拟的 App 侧 patch 生效 → 卡片显示新作者
    expect(wrapper.text()).toContain('新作者')
    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(false)
  })

  it('pixiv 书的弹窗里标题/系列置灰（卡片对象带着 source 一路传下来）', async () => {
    const wrapper = mountNovels({ ...LOCAL_NOVEL, id: 987654, source: 'pixiv' })

    await wrapper.get('[data-testid="novel-edit"]').trigger('click')

    expect(wrapper.get('[data-testid="edit-title"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="edit-series"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="edit-source-badge"]').text()).toBe('pixiv')
  })
})
