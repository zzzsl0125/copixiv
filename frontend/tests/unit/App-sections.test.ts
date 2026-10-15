import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { reactive, h, defineComponent } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import App from '../../src/App.vue'
import { SECTIONS } from '../../src/config'
import type { NovelFilters } from '../../src/types'

/**
 * 「我的上传」section 状态机（docs/LIBRARY_DESIGN.md §5）。
 *
 * 契约：section 的全部事实（keyword / 阈值 / 默认排序）来自 SECTIONS 表，
 * 四条入口路径必须状态一致：
 *   ① 点击侧边栏 ② URL 直达（?keyword=source:local;）③ 点「重置」④ 切走再切回。
 * 关键回归：本地书 like=0，默认 min_like=500 会让书架恒空——任何路径都不能
 * 让 uploads 带着 500/3000 的阈值去搜索。
 *
 * 这里 mock 掉 composables（与 App-config-fallback.test.ts 同法），
 * 用真实的 reactive filters 观察 App 的赋值行为；路由用内存 history。
 */

const mocks = vi.hoisted(() => ({
  loadNovels: vi.fn(),
  fetchConfig: vi.fn(),
  handleSearch: vi.fn(),
}))

function createFilters(): NovelFilters {
  const params = new URLSearchParams(window.location.search)
  return reactive<NovelFilters>({
    keyword: params.get('keyword') || '',
    order_by: params.get('order_by') || 'random',
    order_direction: params.get('order_direction') || 'DESC',
    min_like: params.get('min_like') ? Number(params.get('min_like')) : undefined,
    min_text: params.get('min_text') ? Number(params.get('min_text')) : undefined,
  })
}

vi.mock('../../src/composables', () => ({
  useNovels: () => ({
    novels: { value: [] },
    loading: { value: false },
    error: { value: null },
    noMoreData: { value: false },
    hasExcluded: { value: false },
    filters: createFilters(),
    loadNovels: mocks.loadNovels,
    handleSearch: mocks.handleSearch,
    handleLoadMore: vi.fn(),
    handleCardSearch: vi.fn(),
  }),
  useSystem: () => {
    const systemConfig = { value: null as unknown }
    return {
      systemConfig,
      loading: { value: false },
      error: { value: null },
      fetchConfig: mocks.fetchConfig.mockImplementation(async () => {
        systemConfig.value = {
          batch_download_naming: '{id}-{title}',
          exclude_blocked_tag_novels: true,
        }
        return systemConfig.value
      }),
    }
  },
  useToast: () => ({
    toasts: [],
    show: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
  useBatchMode: () => ({
    isBatchMode: { value: false },
    matchedCount: { value: 0 },
    matchedTruncated: { value: false },
    countLoading: { value: false },
    selectAllLoading: { value: false },
    selectedCount: { value: 0 },
    hasSelection: { value: false },
    hasFilter: { value: false },
    scope: { value: null },
    enter: vi.fn(),
    exit: vi.fn(),
    toggleCard: vi.fn(),
    isCardSelected: vi.fn(),
    selectAllMatched: vi.fn(),
    clearSelection: vi.fn(),
    clearSelectionInScope: vi.fn(),
  }),
}))

/** Sidebar / RouterView / UploadModal 替身：捕获 App 下发的 props、暴露 App 的监听器。 */
const SidebarStub = defineComponent({
  name: 'Sidebar',
  props: {
    activeSection: { type: String, default: null },
    filters: { type: Object, required: true },
    configLoadedAndApplied: { type: Boolean, default: false },
  },
  // 侧边栏不再有 upload 事件：上传入口在列表顶部的 UploadBar（见 RouterViewStub）。
  emits: ['search', 'close', 'update:filters', 'reset-to-defaults', 'toggle-batch-mode'],
  setup(_props, { emit }) {
    return () =>
      h('div', { 'data-testid': 'sidebar-stub' }, [
        h('button', { 'data-testid': 'side-uploads', onClick: () => emit('search', SECTIONS.uploads.keyword, 'uploads') }, '我的上传'),
        h('button', { 'data-testid': 'side-favourites', onClick: () => emit('search', SECTIONS.favourites.keyword, 'favourites') }, '我的收藏'),
        h('button', { 'data-testid': 'side-special-follow', onClick: () => emit('search', SECTIONS.special_follow.keyword, 'special_follow') }, '特别关注'),
        h('button', { 'data-testid': 'side-reset', onClick: () => emit('reset-to-defaults') }, '重置'),
      ])
  },
})

/**
 * 路由出口替身：真实视图（Novels.vue）里 UploadBar 的按钮走
 * `novels-command {type:'upload'}` 上抛，这里用同一个命令模拟那条链路，
 * 从而在 App 层钉住「命令 → 开弹窗」这一段。
 */
const RouterViewStub = defineComponent({
  name: 'RouterView',
  emits: ['novels-command'],
  setup(_props, { emit }) {
    return () =>
      h('div', { 'data-testid': 'router-view-stub' }, [
        h('button', { 'data-testid': 'route-cmd-upload', onClick: () => emit('novels-command', { type: 'upload' }) }, '上传'),
      ])
  },
})

const UploadModalStub = defineComponent({
  name: 'UploadModal',
  props: { isOpen: { type: Boolean, default: false } },
  emits: ['close', 'uploaded', 'error'],
  setup(props, { emit }) {
    return () =>
      h('div', { 'data-testid': 'upload-modal-stub', 'data-open': String(props.isOpen) }, [
        h('button', { 'data-testid': 'modal-uploaded', onClick: () => emit('uploaded', { created: 1, duplicate: 0, failed: 0 }) }, 'ok'),
      ])
  },
})

async function mountApp(path = '/', search = '') {
  // App 的 URL 回填逻辑读 window.location.search（节流 replaceState 之外唯一依赖）。
  // pathname 必须与 path 一致，否则 router.push 会把 path 写进 location.pathname，
  // 覆盖掉这里的 search（用例拿到的一直是首屏状态）。
  vi.stubGlobal('location', {
    href: `http://localhost${path}${search}`,
    search,
    pathname: path,
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/tasks', component: { template: '<div />' } },
    ],
  })
  await router.push(`${path}${search}`)
  await router.isReady()

  const wrapper = mount(App, {
    global: {
      plugins: [router],
      stubs: {
        Sidebar: SidebarStub,
        UploadModal: UploadModalStub,
        ToastContainer: true,
        RouterView: RouterViewStub,
      },
    },
  })
  await flushPromises()
  return {
    wrapper,
    router,
    sidebar: () => wrapper.getComponent(SidebarStub),
    filters: () => wrapper.getComponent(SidebarStub).props('filters') as NovelFilters,
    /** 侧边栏拿到的 activeSection（null = 不高亮任何项）。 */
    section: () => wrapper.getComponent(SidebarStub).props('activeSection') as string | null,
  }
}

describe('「我的上传」section 状态机', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.fetchConfig.mockClear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('① 点击侧边栏「我的上传」：keyword + 阈值 0/0 + id DESC 排序', async () => {
    const app = await mountApp('/')
    // 首屏是小说列表：套默认阈值
    expect(app.filters().min_like).toBe(SECTIONS.novels.min_like)

    await app.wrapper.get('[data-testid="side-uploads"]').trigger('click')
    await flushPromises()

    expect(app.section()).toBe('uploads')
    expect(app.filters().keyword).toBe('source:local;')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)
    // §3.1：id 递增，id DESC = 最新上传在前；随机排序会让「刚传的书」找不到
    expect(app.filters().order_by).toBe('id')
    expect(app.filters().order_direction).toBe('DESC')
    // 一次点击 = 一次列表请求。赋 filters.keyword 会触发 App 的 keyword watcher，
    // 若点击路径与 watcher 各自 applySection 一次，这里就是 2（重复请求 + 重复 reset）。
    expect(mocks.handleSearch).toHaveBeenCalledTimes(1)
  })

  it('② URL 直达 ?keyword=source:local;：section 高亮 + 阈值归零（书架不为空）', async () => {
    const app = await mountApp('/', '?keyword=source%3Alocal%3B')

    expect(app.section()).toBe('uploads')
    expect(app.filters().keyword).toBe('source:local;')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)
  })

  it('② URL 直达显式给了阈值时尊重 URL（不覆盖用户意图）', async () => {
    const app = await mountApp('/', '?keyword=source%3Alocal%3B&min_like=10&min_text=20')

    expect(app.filters().min_like).toBe(10)
    expect(app.filters().min_text).toBe(20)
  })

  it('② URL 直达「我的收藏」也套该 section 的阈值（书架态 0/0，不是小说列表的 500/3000）', async () => {
    const app = await mountApp('/', '?keyword=is_favourite%3Atrue%3B')

    expect(app.section()).toBe('favourites')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)
  })

  it('③ 点「重置」：回到小说列表（keyword 清空 + 默认阈值 + 随机排序）', async () => {
    const app = await mountApp('/')
    await app.wrapper.get('[data-testid="side-uploads"]').trigger('click')

    await app.wrapper.get('[data-testid="side-reset"]').trigger('click')
    await flushPromises()

    expect(app.section()).toBe('novels')
    expect(app.filters().keyword).toBe('')
    expect(app.filters().min_like).toBe(SECTIONS.novels.min_like)
    expect(app.filters().min_text).toBe(SECTIONS.novels.min_text)
    expect(app.filters().order_by).toBe('random')
  })

  it('④ 切走再切回：收藏 → 上传 → 收藏，阈值/排序每次都跟 section 走', async () => {
    const app = await mountApp('/')

    await app.wrapper.get('[data-testid="side-favourites"]').trigger('click')
    await flushPromises()
    expect(app.section()).toBe('favourites')
    // 书架态一律 0/0（绝对值：表再漂一次就红）
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)

    await app.wrapper.get('[data-testid="side-uploads"]').trigger('click')
    await flushPromises()
    expect(app.section()).toBe('uploads')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)
    expect(app.filters().order_by).toBe('id')

    await app.wrapper.get('[data-testid="side-favourites"]').trigger('click')
    await flushPromises()
    expect(app.section()).toBe('favourites')
    expect(app.filters().keyword).toBe('is_favourite:true;')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)
  })

  /**
   * 回归（32a13bb 引入、此处钉死）：书架 = 「我的清单」，点开不得带门槛。
   * 旧链路经 useNovels.applySearchOrdering() 归零，收成 SECTIONS 表时只给
   * uploads 写了 0——收藏/特别关注照抄 DEFAULT_MIN_*（500/3000），
   * 「我的收藏」点开时筛选条上的「500 / 3.0k」总是亮的，特别关注还静默
   * 藏掉 1395/4371 篇。这里用**绝对值**断言，表再漂一次就红。
   */
  it('⑤ 点击「我的收藏」/「特别关注」：阈值同样归零（与「我的上传」同构）', async () => {
    const app = await mountApp('/')
    // 首屏小说列表是浏览态：门槛生效（默认 500/3000）
    expect(app.filters().min_like).toBe(500)
    expect(app.filters().min_text).toBe(3000)

    await app.wrapper.get('[data-testid="side-favourites"]').trigger('click')
    await flushPromises()
    expect(app.section()).toBe('favourites')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)
    expect(app.filters().order_by).toBe('id')
    expect(app.filters().order_direction).toBe('DESC')

    await app.wrapper.get('[data-testid="side-special-follow"]').trigger('click')
    await flushPromises()
    expect(app.section()).toBe('special_follow')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)
    expect(app.filters().order_by).toBe('id')
    expect(app.filters().order_direction).toBe('DESC')
  })

  it('⑤ URL 直达「特别关注」：同样归零（不是小说列表的 500/3000）', async () => {
    const app = await mountApp('/', '?keyword=is_special_follow%3Atrue%3B')

    expect(app.section()).toBe('special_follow')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)
  })

  it('keyword 被外部改写（搜索历史回填 / 后退前进）时 section 跟着变', async () => {
    const app = await mountApp('/')
    expect(app.section()).toBe('novels')

    // useNovels.onPopState 直接改 filters.keyword，不经过 App 的 handler
    app.filters().keyword = 'is_special_follow:true;'
    await flushPromises()
    expect(app.section()).toBe('special_follow')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)

    // 自由搜索：无 section 归属（侧边栏不高亮）
    app.filters().keyword = 'author_id:42;'
    await flushPromises()
    expect(app.section()).toBe(null)

    // 收回空关键词 = 小说列表
    app.filters().keyword = ''
    await flushPromises()
    expect(app.section()).toBe('novels')
  })

  it('搜索历史回填 source:local;：section 与阈值一并就位', async () => {
    const app = await mountApp('/')
    expect(app.section()).toBe('novels')

    app.filters().keyword = SECTIONS.uploads.keyword
    await flushPromises()

    expect(app.section()).toBe('uploads')
    expect(app.filters().min_like).toBe(0)
    expect(app.filters().min_text).toBe(0)
    expect(app.filters().order_by).toBe('id')
  })

  it('自由搜索（非 section keyword）不被误判成 section，也不改阈值', async () => {
    const app = await mountApp('/', '?keyword=author_id%3A42%3B')

    expect(app.section()).toBe(null)
    // 自由搜索不属于任何 section → 回落小说列表默认阈值（既有行为）
    expect(app.filters().min_like).toBe(SECTIONS.novels.min_like)
  })
})

describe('上传弹窗联动', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('视图侧发 novels-command{upload} → 打开弹窗（侧边栏已不挂上传按钮）', async () => {
    const app = await mountApp('/')
    expect(app.wrapper.get('[data-testid="upload-modal-stub"]').attributes('data-open')).toBe('false')
    // 侧边栏行内不再有上传钮
    expect(app.wrapper.find('[data-testid="side-upload-trigger"]').exists()).toBe(false)

    await app.wrapper.get('[data-testid="route-cmd-upload"]').trigger('click')
    expect(app.wrapper.get('[data-testid="upload-modal-stub"]').attributes('data-open')).toBe('true')
  })

  it('在「我的上传」书架里上传成功：重新加载当前 section（新书出现）', async () => {
    const app = await mountApp('/')
    await app.wrapper.get('[data-testid="side-uploads"]').trigger('click')
    await flushPromises()
    mocks.handleSearch.mockClear()

    await app.wrapper.get('[data-testid="modal-uploaded"]').trigger('click')
    await flushPromises()

    expect(mocks.handleSearch).toHaveBeenCalledWith(undefined, { setOrdering: false })
    expect(app.section()).toBe('uploads')
    expect(app.filters().min_like).toBe(0)
  })

  it('不在书架时上传成功：不打断当前视图（不改 keyword）', async () => {
    const app = await mountApp('/')
    app.filters().keyword = 'author_id:42;'
    await flushPromises()
    mocks.handleSearch.mockClear()

    await app.wrapper.get('[data-testid="modal-uploaded"]').trigger('click')
    await flushPromises()

    expect(app.filters().keyword).toBe('author_id:42;')
    expect(mocks.handleSearch).not.toHaveBeenCalled()
  })
})
