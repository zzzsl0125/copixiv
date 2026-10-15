import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref, computed } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import Novels from '../../src/views/Novels.vue'
import type { BatchScope, NovelFilters } from '../../src/types'

/**
 * 列表页的批量编辑接线：BatchBar「编辑」→ BatchEditModal → `batch-action`。
 *
 * 这里只钉**宿主那一段**（弹窗组件本身的行为见 BatchEditModal.test.ts）：
 * 按钮打开弹窗、成功上抛的动作形状与既有 operation-success 出口一字不差
 * （`{ type: 'operation-success', payload: { operation: 'update_metadata', result } }`），
 * App 侧据此出 toast 并清选择（见 App-batch-metadata.test.ts）。
 *
 * 真实视图依赖较多，按 Novels-upload-bar.test.ts 的办法 mock 组合式函数与 API。
 */

const h = vi.hoisted(() => ({
  batchMetadata: vi.fn(),
  lookupAuthor: vi.fn(),
  lookupSeries: vi.fn(),
}))

// useIdLookup / formatIdLookupHint 用真实现（弹窗自己的行为），只换掉外部依赖。
vi.mock('../../src/composables', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/composables')>()),
  useToast: () => ({ toasts: [], show: vi.fn(), success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }),
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
    displayNovels: computed(() => []),
    viewLoading: ref(false),
    viewLoadingMore: ref(false),
    viewHasMore: ref(false),
    viewShownCount: ref(0),
    viewTotalCount: ref(0),
    toggleViewSelected: vi.fn(),
    toggleViewExcluded: vi.fn(),
    handleViewLoadMore: vi.fn(),
    patchNovel: vi.fn(),
  }),
  useMasonryLayout: () => ({ columns: ref([[]]), relayout: vi.fn() }),
}))

vi.mock('../../src/api', () => ({
  tagPreferenceApi: { getTagPreferences: vi.fn(async () => []) },
  searchHistoryApi: {
    getSearchHistory: vi.fn(async () => []),
    deleteSearchHistoryItem: vi.fn(async () => undefined),
    clearSearchHistory: vi.fn(async () => undefined),
  },
  novelApi: {
    batchMetadata: h.batchMetadata,
    lookupAuthor: h.lookupAuthor,
    lookupSeries: h.lookupSeries,
    toggleFavourite: vi.fn(),
    toggleSpecialFollow: vi.fn(),
  },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

const FILTERS: NovelFilters = { keyword: '', order_by: 'random', order_direction: 'DESC' }
const SCOPE: BatchScope = { mode: 'ids', novel_ids: [1, 2, 3], excluded_ids: [] }

function mountNovels() {
  return mount(Novels, {
    props: {
      isSidebarOpen: false,
      filters: FILTERS,
      novelsState: {
        novels: [],
        loading: false,
        error: null,
        noMoreData: true,
        hasExcluded: false,
      },
      batchState: {
        mode: true,
        matchedCount: 3,
        matchedTruncated: false,
        countLoading: false,
        selectAllLoading: false,
        selectedCount: 3,
        hasSelection: true,
        hasFilter: true,
        scope: SCOPE,
        isSelected: () => false,
      },
    },
  })
}

describe('列表页批量编辑接线', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    h.batchMetadata.mockImplementation(
      async (params: { apply: boolean }) => (params.apply
        ? { matched: 3, applicable: 2, skipped: 1, affected: 2 }
        : { matched: 3, applicable: 2, skipped: 1, affected: 0 }),
    )
    h.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: '新作者', source: 'pixiv', novel_count: 0,
    })
    h.lookupSeries.mockResolvedValue({
      series_id: 888, series_name: '新系列', source: 'local', novel_count: 0,
    })
    // LoadMore 用 IntersectionObserver 做无限滚动；jsdom 没有它，这里给个空壳。
    vi.stubGlobal('IntersectionObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
    })
  })

  it('点「编辑」打开弹窗，并立即按 apply:false 预演', async () => {
    const wrapper = mountNovels()
    expect(wrapper.find('[data-testid="batch-edit-modal"]').exists()).toBe(false)

    await wrapper.get('[data-testid="batch-edit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="batch-edit-modal"]').exists()).toBe(true)
    expect(h.batchMetadata).toHaveBeenCalledWith({ scope: SCOPE, apply: false })
    expect(wrapper.get('[data-testid="batch-edit-preview"]').text()).toContain('1 篇 pixiv 书跳过')
  })

  it('确认后上抛既有 operation-success 形状（App 侧 toast + 清选择），弹窗关闭', async () => {
    const wrapper = mountNovels()

    await wrapper.get('[data-testid="batch-edit"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('777')
    await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
    await flushPromises()

    expect(h.batchMetadata).toHaveBeenLastCalledWith({ scope: SCOPE, author_id: 777, apply: true })
    const actions = wrapper.emitted('batch-action') ?? []
    expect(actions).toHaveLength(1)
    expect(actions[0][0]).toEqual({
      type: 'operation-success',
      payload: {
        operation: 'update_metadata',
        result: { matched: 3, applicable: 2, skipped: 1, affected: 2 },
      },
    })
    expect(wrapper.find('[data-testid="batch-edit-modal"]').exists()).toBe(false)
  })

  it('关掉弹窗不产生任何动作（取消不是一次修改）', async () => {
    const wrapper = mountNovels()

    await wrapper.get('[data-testid="batch-edit"]').trigger('click')
    await flushPromises()
    // BaseModal 的「取消」是 footer 里唯一的非 submit 按钮
    await wrapper.get('[data-testid="base-modal"] button:not([type="submit"])').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('batch-action')).toBeUndefined()
    expect(wrapper.find('[data-testid="batch-edit-modal"]').exists()).toBe(false)
  })
})
