import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { QueryClient, VUE_QUERY_CLIENT } from '@tanstack/vue-query'
import { queryKeys } from '../../api/queryKeys'

const apiMocks = vi.hoisted(() => ({
  batchMetadata: vi.fn(),
  lookupAuthor: vi.fn(),
  lookupSeries: vi.fn(),
  getSearchHistory: vi.fn(),
  deleteSearchHistoryItem: vi.fn(),
  clearSearchHistory: vi.fn(),
}))

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    novelApi: {
      ...actual.novelApi,
      batchMetadata: apiMocks.batchMetadata,
      lookupAuthor: apiMocks.lookupAuthor,
      lookupSeries: apiMocks.lookupSeries,
    },
    searchHistoryApi: {
      getSearchHistory: apiMocks.getSearchHistory,
      deleteSearchHistoryItem: apiMocks.deleteSearchHistoryItem,
      clearSearchHistory: apiMocks.clearSearchHistory,
    },
  }
})

vi.mock('../../composables', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../composables')>()
  return {
    ...actual,
    useToast: () => ({
      error: vi.fn(),
      success: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
    }),
  }
})

import BatchEditModal from '../features/BatchEditModal.vue'
import BatchDeleteModal from '../features/BatchDeleteModal.vue'
import NovelHeader from '../features/NovelHeader.vue'

describe('Phase 1C regression fixes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.getSearchHistory.mockResolvedValue([])
  })

  it('BatchEditModal disables submit button when preview applicable === 0', async () => {
    apiMocks.batchMetadata.mockResolvedValue({
      matched: 2,
      applicable: 0,
      skipped: 2,
      updated: 0,
    })

    const wrapper = mount(BatchEditModal, {
      props: {
        isOpen: true,
        scope: { novel_ids: [1, 2] },
        scopeLabel: '已勾选的 2 篇',
      },
    })

    await flushPromises()
    await nextTick()

    // Even after toggling unlinkAuthor so hasChanges=true, submit stays disabled when applicable === 0
    await wrapper.get('[data-testid="batch-edit-author-unlink"]').setValue(true)
    await nextTick()

    const submitBtn = wrapper.get('button[type="submit"]')
    expect((submitBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('BatchDeleteModal renders grammatical Chinese confirmation copy', () => {
    const wrapper = mount(BatchDeleteModal, {
      props: {
        isOpen: true,
        scope: { novel_ids: [1, 2] },
        scopeLabel: '已勾选的 2 篇',
      },
    })

    expect(wrapper.text()).toContain('将删除 已勾选的 2 篇小说及其本地文件。')
    expect(wrapper.text()).not.toContain('，共 影响')
  })

  it('NovelHeader invalidates searchHistory query cache when search is submitted', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const wrapper = mount(NovelHeader, {
      props: {
        filters: { keyword: '', order_by: 'random', order_direction: 'DESC' },
      },
      global: {
        provide: {
          [VUE_QUERY_CLIENT]: queryClient,
        },
      },
    })

    const input = wrapper.get('input[aria-label="搜索小说"]')
    await input.setValue('奇幻')
    await input.trigger('keyup.enter')
    await nextTick()

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.searchHistory() })
    expect(wrapper.emitted('search')).toEqual([['奇幻']])

    queryClient.clear()
  })
})
