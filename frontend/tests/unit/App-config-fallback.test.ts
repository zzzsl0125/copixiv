import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import App from '../../src/App.vue'

const mocks = vi.hoisted(() => ({
  loadNovels: vi.fn(),
  fetchConfig: vi.fn(),
}))

vi.mock('../../src/composables', () => ({
  useNovels: () => ({
    novels: { value: [] },
    loading: { value: false },
    error: { value: null },
    noMoreData: { value: false },
    filters: {
      keyword: '',
      order_by: 'random',
      order_direction: 'DESC',
      min_like: undefined,
      min_text: undefined,
    },
    loadNovels: mocks.loadNovels,
    handleSearch: vi.fn(),
    handleLoadMore: vi.fn(),
    handleCardSearch: vi.fn(),
  }),
  useSystem: () => ({
    systemConfig: { value: null },
    loading: { value: false },
    error: { value: null },
    fetchConfig: mocks.fetchConfig,
  }),
  useToast: () => ({
    toasts: [],
    show: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
}))

describe('App bootstrap resilience', () => {
  it('loads novels even when /api/system/config fails', async () => {
    mocks.fetchConfig.mockResolvedValue(null)

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()

    mount(App, {
      global: {
        plugins: [router],
        stubs: {
          Sidebar: true,
          ToastContainer: true,
          RouterView: true,
        },
      },
    })
    await flushPromises()

    expect(mocks.fetchConfig).toHaveBeenCalledTimes(1)
    expect(mocks.loadNovels).toHaveBeenCalledTimes(1)
  })
})
