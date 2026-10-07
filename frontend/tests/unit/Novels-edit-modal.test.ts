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
  syncNovel: vi.fn(),
  lookupAuthor: vi.fn(),
  lookupSeries: vi.fn(),
  patchNovel: vi.fn(),
}))

// ``useIdLookup`` / ``formatIdLookupHint`` 用**真**实现：它们是弹窗里的纯逻辑
// （防抖 / 过期响应 / hint 文案），mock 掉就等于把被测行为替换成空壳。
vi.mock('../../src/composables', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/composables')>()),
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
    syncNovel: h.syncNovel,
    lookupAuthor: h.lookupAuthor,
    lookupSeries: h.lookupSeries,
    toggleFavourite: vi.fn(),
    toggleSpecialFollow: vi.fn(),
    downloadUrl: vi.fn(() => '/api/novels/1/download?format=txt'),
  },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

const FILTERS: NovelFilters = { keyword: '', order_by: 'random', order_direction: 'DESC' }

// 后端下发的字段策略（Novel.editable_fields）：本地书七个字段（含 2026-09-22
// 放开的 caption）。
const LOCAL_EDITABLE = [
  'title', 'author_id', 'series_id', 'series_index', 'tags', 'create_time', 'caption',
]

const LOCAL_NOVEL: Novel = {
  id: -999000001,
  title: '本地书名',
  author_id: null,                  // 本地书：上传时 author_id / series_id 都是 NULL
  author_name: '未知作者',
  series_id: null,
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
  editable_fields: LOCAL_EDITABLE,
}

/** pixiv 书：title / author_id / series_id 只读（title / series_id 会被来源覆盖，author_id 是身份以来源为准）。 */
const PIXIV_NOVEL: Novel = {
  ...LOCAL_NOVEL,
  id: 987654,
  title: 'pixiv 标题',
  author_id: 555,
  author_name: 'pixiv 作者',
  source: 'pixiv',
  editable_fields: ['series_index', 'tags', 'create_time'],
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
  { authorId }: { authorId: string },
) {
  await wrapper.get('[data-testid="novel-edit"]').trigger('click')
  await wrapper.get('[data-testid="edit-author-id"]').setValue(authorId)
  await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
  await flushPromises()
}

describe('Novels.vue 元数据编辑接线', () => {
  beforeEach(() => {
    h.updateNovel.mockReset()
    h.syncNovel.mockReset()
    h.lookupAuthor.mockReset()
    h.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: '新作者', source: 'pixiv', novel_count: 0,
    })
    h.lookupSeries.mockResolvedValue({
      series_id: 888, series_name: '新系列', source: 'local', novel_count: 0,
    })
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
    // 作者一栏是 ID：本地书为空（还没挂到任何 pixiv 作者名下）
    expect((wrapper.get('[data-testid="edit-author-id"]').element as HTMLInputElement).value)
      .toBe('')
    expect(h.updateNovel).not.toHaveBeenCalled()
  })

  it('保存后：主列表卡片显示解析出的新作者、集合视图副本被 patch、弹窗关闭', async () => {
    const updated = { ...LOCAL_NOVEL, author_id: 777, author_name: '新作者' }
    h.updateNovel.mockResolvedValue(updated)
    const wrapper = mountNovels()

    await openAndSave(wrapper, { authorId: '777' })

    expect(h.updateNovel).toHaveBeenCalledTimes(1)
    expect(h.updateNovel.mock.calls[0][0]).toBe(LOCAL_NOVEL.id)
    // 只提交 id：名字由服务端解析后回写（客户端提交 author_name 会 422）
    expect(h.updateNovel.mock.calls[0][1]).toEqual({ author_id: 777 })
    // 集合视图那两份副本：本页负责
    expect(h.patchNovel).toHaveBeenCalledWith(updated)
    // 主列表：模拟的 App 侧 patch 生效 → 卡片显示新作者
    expect(wrapper.text()).toContain('新作者')
    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(false)
  })

  it('pixiv 书的弹窗里 title / author_id / series_id 置灰，并显示「同步」（本地书没有）', async () => {
    const wrapper = mountNovels(PIXIV_NOVEL)

    await wrapper.get('[data-testid="novel-edit"]').trigger('click')

    // 置灰看服务端下发的 editable_fields（卡片对象带着它一路传下来）
    expect(wrapper.get('[data-testid="edit-title"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="edit-author-id"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="edit-series-id"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="edit-source-badge"]').text()).toBe('pixiv')
    // 同步按钮只在 pixiv 书上出现
    expect(wrapper.find('[data-testid="edit-sync"]').exists()).toBe(true)
  })

  it('本地书：三个字段可编辑，且没有「同步」按钮（没有来源可同步）', async () => {
    const wrapper = mountNovels(LOCAL_NOVEL)

    await wrapper.get('[data-testid="novel-edit"]').trigger('click')

    expect(wrapper.get('[data-testid="edit-title"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="edit-author-id"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="edit-sync"]').exists()).toBe(false)
  })

  it('「同步」把响应送回卡片与集合视图，弹窗留着并换成新初值', async () => {
    const refreshed: Novel = {
      ...PIXIV_NOVEL,
      author_name: '来源新作者',
      tags: ['来源新标签'],
      series_index: 7,
    }
    h.syncNovel.mockResolvedValue(refreshed)
    const wrapper = mountNovels(PIXIV_NOVEL)

    await wrapper.get('[data-testid="novel-edit"]').trigger('click')
    await wrapper.get('[data-testid="edit-sync"]').trigger('click')
    await flushPromises()

    expect(h.syncNovel).toHaveBeenCalledWith(987654)
    // 集合视图副本（本页负责）与主列表（模拟的 App patch）都换成响应值
    expect(h.patchNovel).toHaveBeenCalledWith(refreshed)
    expect(wrapper.text()).toContain('来源新作者')
    // 弹窗不关，且表单初值已刷新 → 与响应无差异，保存按钮回到禁用
    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(true)
    expect((wrapper.get('[data-testid="edit-series-index"]').element as HTMLInputElement).value)
      .toBe('7')
    expect(
      wrapper.get('[data-testid="base-modal"] button[type="submit"]').attributes('disabled'),
    ).toBeDefined()
  })
})
