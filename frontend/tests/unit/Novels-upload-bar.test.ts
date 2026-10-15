import { describe, it, expect, vi } from 'vitest'
import { ref, computed } from 'vue'
import { mount } from '@vue/test-utils'
import Novels from '../../src/views/Novels.vue'
import { BAR_SHELL_CLASS } from '../../src/lib/ui'
import type { NovelFilters } from '../../src/types'

/**
 * 上传工具条在列表页的**模式规则**（docs/LIBRARY_DESIGN.md §5）。
 *
 * 背景：批量模式与「我的上传」是正交的（书架里照样能批量删/打标签/导出，
 * 契约 §7-6 还钉了"所见=所批"）。两条栏抢的是同一条 sticky 槽位，所以规则是：
 *
 *   UploadBar 显示 ⟺ activeSection === 'uploads' 且 **非**批量模式
 *
 * 批量模式优先——那时用户在操作已有书，不在收新书；这条测试就是钉住这个优先级，
 * 防止后来者把两条栏叠在一起（或让按钮在批量模式下悬空）。
 *
 * 真实视图（Novels.vue）依赖较多，这里只 mock 组合式函数与 API 模块。
 */

// ``useIdLookup`` / ``formatIdLookupHint`` 用**真**实现：它们是弹窗里的纯逻辑
// （防抖 / 过期响应 / hint 文案），mock 掉就等于把被测行为替换成空壳。
vi.mock('../../src/composables', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/composables')>()),
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }),
  // BatchDownloadModal（列表页常驻的子组件）会读 useSystem
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
    displayNovels: computed(() => []),
    viewLoading: ref(false),
    viewLoadingMore: ref(false),
    viewHasMore: ref(false),
    viewShownCount: ref(0),
    viewTotalCount: ref(0),
    toggleViewSelected: vi.fn(),
    toggleViewExcluded: vi.fn(),
    handleViewLoadMore: vi.fn(),
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
  novelApi: { toggleFavourite: vi.fn(), toggleSpecialFollow: vi.fn() },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

const FILTERS: NovelFilters = {
  keyword: '',
  order_by: 'random',
  order_direction: 'DESC',
}

const NOVELS_STATE = {
  novels: [],
  loading: false,
  error: null,
  noMoreData: false,
  hasExcluded: false,
}

const BATCH_OFF = {
  mode: false,
  matchedCount: 0,
  matchedTruncated: false,
  countLoading: false,
  selectAllLoading: false,
  selectedCount: 0,
  hasSelection: false,
  hasFilter: false,
  scope: null,
  isSelected: () => false,
}

const BATCH_ON = { ...BATCH_OFF, mode: true }

function mountNovels(overrides: Record<string, unknown> = {}) {
  return mount(Novels, {
    props: {
      isSidebarOpen: false,
      filters: { ...FILTERS },
      novelsState: { ...NOVELS_STATE },
      batchState: { ...BATCH_OFF },
      activeSection: null,
      ...overrides,
    },
  })
}

describe('上传工具条的模式规则', () => {
  it('「我的上传」书架：显示上传栏', () => {
    const wrapper = mountNovels({ activeSection: 'uploads' })

    const bar = wrapper.get('[data-testid="uploads-bar"]')
    // 与批量栏同一套外壳样式（lib/ui.ts 单源）
    expect(bar.classes()).toContain(BAR_SHELL_CLASS.split(' ')[0])
    expect(wrapper.find('[data-testid="batch-bar"]').exists()).toBe(false)
  })

  it('批量模式下让位给批量栏：上传栏不渲染（两条栏不叠）', () => {
    const wrapper = mountNovels({ activeSection: 'uploads', batchState: { ...BATCH_ON } })

    expect(wrapper.find('[data-testid="uploads-bar"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="batch-bar"]').exists()).toBe(true)
  })

  it('其他书架（小说列表 / 收藏 / 特别关注）不出现上传栏', () => {
    for (const section of ['novels', 'favourites', 'special_follow', null]) {
      const wrapper = mountNovels({ activeSection: section })
      expect(wrapper.find('[data-testid="uploads-bar"]').exists()).toBe(false)
    }
  })

  it('点击上传按钮 → 上抛 novels-command{type:upload}（App 据此开弹窗）', async () => {
    const wrapper = mountNovels({ activeSection: 'uploads' })

    await wrapper.get('[data-testid="uploads-bar-upload"]').trigger('click')

    expect(wrapper.emitted('novels-command')).toEqual([[{ type: 'upload' }]])
  })
})
