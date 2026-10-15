import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { reactive, h, defineComponent } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import App from '../../src/App.vue'
import type { NovelFilters } from '../../src/types'

/**
 * 批量编辑作者 / 系列在 App 侧的收尾（`operation-success` → toast → 清选择 → 原地刷新）。
 *
 * 这里钉的核心是一句话：**跳过数必须进 toast**。批量编辑只作用于本地书，pixiv 书
 * 一律跳过（服务端报数），"选了 10 篇只改了 6 篇"不说清跳过 4 篇就会被读成丢了 4 篇。
 * 顺带钉住既有三种操作（delete / add_tags / remove_tags）的文案没被这次改动碰坏
 * ——`update_metadata` 的响应多两个字段，处理它们的入口是同一个函数。
 *
 * mock 组合式函数 + 路由替身（与 App-sections.test.ts 同法）。
 */

const mocks = vi.hoisted(() => ({
  handleSearch: vi.fn(),
  clearSelection: vi.fn(),
  toastSuccess: vi.fn(),
  toastInfo: vi.fn(),
  fetchConfig: vi.fn(),
}))

function createFilters(): NovelFilters {
  return reactive<NovelFilters>({
    keyword: '',
    order_by: 'random',
    order_direction: 'DESC',
    min_like: undefined,
    min_text: undefined,
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
    loadNovels: vi.fn(),
    handleSearch: mocks.handleSearch,
    handleLoadMore: vi.fn(),
    handleCardSearch: vi.fn(),
  }),
  useSystem: () => ({
    systemConfig: { value: null },
    loading: { value: false },
    error: { value: null },
    fetchConfig: mocks.fetchConfig.mockResolvedValue(null),
  }),
  useToast: () => ({
    toasts: [],
    show: vi.fn(),
    success: mocks.toastSuccess,
    error: vi.fn(),
    info: mocks.toastInfo,
    warning: vi.fn(),
  }),
  useBatchMode: () => ({
    isBatchMode: { value: true },
    matchedCount: { value: 10 },
    matchedTruncated: { value: false },
    countLoading: { value: false },
    selectAllLoading: { value: false },
    selectedCount: { value: 10 },
    hasSelection: { value: true },
    hasFilter: { value: true },
    scope: { value: { mode: 'ids', novel_ids: [1, 2, 3], excluded_ids: [] } },
    enter: vi.fn(),
    exit: vi.fn(),
    toggleCard: vi.fn(),
    isCardSelected: vi.fn(),
    selectAllMatched: vi.fn(),
    clearSelection: mocks.clearSelection,
    clearSelectionInScope: vi.fn(),
  }),
}))

const SidebarStub = defineComponent({
  name: 'Sidebar',
  emits: ['search', 'close', 'update:filters', 'reset-to-defaults', 'toggle-batch-mode'],
  setup() {
    return () => h('div', { 'data-testid': 'sidebar-stub' })
  },
})

/** 路由出口替身：把 Novels.vue 上抛的 batch-action 原样送给 App。 */
const RouterViewStub = defineComponent({
  name: 'RouterView',
  emits: ['novels-command', 'batch-action'],
  setup(_props, { emit }) {
    return () => h('div', { 'data-testid': 'router-view-stub' }, [
      h('button', {
        'data-testid': 'batch-success-update-metadata',
        onClick: () => emit('batch-action', {
          type: 'operation-success',
          payload: {
            operation: 'update_metadata',
            result: { matched: 10, applicable: 6, skipped: 4, affected: 6 },
          },
        }),
      }, 'update_metadata'),
      h('button', {
        'data-testid': 'batch-success-update-metadata-no-skip',
        onClick: () => emit('batch-action', {
          type: 'operation-success',
          payload: {
            operation: 'update_metadata',
            result: { matched: 6, applicable: 6, skipped: 0, affected: 6 },
          },
        }),
      }, 'update_metadata-no-skip'),
      h('button', {
        'data-testid': 'batch-success-delete',
        onClick: () => emit('batch-action', {
          type: 'operation-success',
          payload: { operation: 'delete', result: { matched: 3, affected: 3 } },
        }),
      }, 'delete'),
    ])
  },
})

const UploadModalStub = defineComponent({
  name: 'UploadModal',
  emits: ['close', 'uploaded', 'error'],
  setup() {
    return () => h('div', { 'data-testid': 'upload-modal-stub' })
  },
})

async function mountApp() {
  vi.stubGlobal('location', { href: 'http://localhost/', search: '', pathname: '/' })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: { template: '<div />' } }],
  })
  await router.push('/')
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
  return wrapper
}

describe('批量编辑作者 / 系列的成功收尾', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('toast 说清跳过数，并清空选择 + 原地刷新列表', async () => {
    const wrapper = await mountApp()

    await wrapper.get('[data-testid="batch-success-update-metadata"]').trigger('click')
    await flushPromises()

    expect(mocks.toastSuccess).toHaveBeenCalledWith('已修改 6 篇的作者/系列（跳过 4 篇 pixiv 书）')
    expect(mocks.clearSelection).toHaveBeenCalledTimes(1)
    expect(mocks.handleSearch).toHaveBeenCalledWith(undefined, { updateUrl: false })
  })

  it('一篇都没跳过时不带那句括号（不留「跳过 0 篇」的噪音）', async () => {
    const wrapper = await mountApp()

    await wrapper.get('[data-testid="batch-success-update-metadata-no-skip"]').trigger('click')
    await flushPromises()

    expect(mocks.toastSuccess).toHaveBeenCalledWith('已修改 6 篇的作者/系列')
  })

  it('既有操作的文案不受影响（同一个入口仍在按 operation 分派）', async () => {
    const wrapper = await mountApp()

    await wrapper.get('[data-testid="batch-success-delete"]').trigger('click')
    await flushPromises()

    expect(mocks.toastSuccess).toHaveBeenCalledWith('已删除 3 篇小说')
    expect(mocks.clearSelection).toHaveBeenCalledTimes(1)
  })
})
