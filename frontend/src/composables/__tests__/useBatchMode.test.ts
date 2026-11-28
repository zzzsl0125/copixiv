import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, reactive, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { DEFAULT_PAGE_SIZE } from '../../config'
import type { Novel, NovelFilters } from '../../types'
import { flushPromises, mountWithQuery } from '../../test-utils/queryWrapper'

const apiMocks = vi.hoisted(() => ({
  countNovels: vi.fn(),
  getNovelIds: vi.fn(),
  matchNovelIds: vi.fn(),
  sortNovelIds: vi.fn(),
  getNovelsByIds: vi.fn(),
}))

const toastMocks = vi.hoisted(() => ({
  show: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
}))

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    novelApi: {
      ...actual.novelApi,
      countNovels: apiMocks.countNovels,
      getNovelIds: apiMocks.getNovelIds,
      matchNovelIds: apiMocks.matchNovelIds,
      sortNovelIds: apiMocks.sortNovelIds,
      getNovelsByIds: apiMocks.getNovelsByIds,
    },
  }
})

vi.mock('../useToast', () => ({
  useToast: () => toastMocks,
}))

import { useBatchMode } from '../useBatchMode'
import { useNovelShellState, useProvideNovelShellState } from '../useNovelShellState'

function makeNovel(id: number, overrides: Partial<Novel> = {}): Novel {
  return {
    id,
    title: `Novel ${id}`,
    author_id: 1,
    author_name: 'Author',
    series_id: null,
    series_name: null,
    series_index: null,
    like: 100,
    view: 200,
    text: 1000,
    has_epub: 0,
    create_time: null,
    source: 'pixiv',
    can_sync: false,
    is_favourite: false,
    is_special_follow: false,
    is_series: false,
    chapter_count: 1,
    tags: [],
    editable_fields: [],
    ...overrides,
  }
}

function makeFilters(overrides: Partial<NovelFilters> = {}): NovelFilters {
  return reactive<NovelFilters>({
    keyword: '',
    min_like: 0,
    min_text: 0,
    order_by: 'id',
    order_direction: 'DESC',
    ...overrides,
  })
}

describe('useBatchMode (Vue Query & selected collection view)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('scrollTo', vi.fn())
    apiMocks.countNovels.mockResolvedValue({ total: 12, truncated: false })
    apiMocks.getNovelIds.mockResolvedValue({ ids: [2, 3], total: 2 })
    apiMocks.matchNovelIds.mockResolvedValue({ matching_ids: [1, 3] })
    apiMocks.sortNovelIds.mockImplementation(async (ids: number[]) => ({
      ids,
      total: ids.length,
    }))
    apiMocks.getNovelsByIds.mockImplementation(async (ids: number[]) => ({
      novels: ids.map((id) => makeNovel(id)),
    }))
  })

  it('gates count query on batch mode and manages union/scoped selection', async () => {
    const filters = makeFilters()

    const { result: batch, wrapper } = await mountWithQuery(() => useBatchMode(filters))
    expect(batch.isBatchMode.value).toBe(false)
    expect(batch.scope.value).toBeNull()
    expect(apiMocks.countNovels).not.toHaveBeenCalled()

    batch.enter()
    await nextTick()
    await flushPromises(5)

    expect(apiMocks.countNovels).toHaveBeenCalledWith(
      {
        keyword: '',
        min_like: 0,
        min_text: 0,
        exclude_blocked: undefined,
      },
      expect.any(AbortSignal),
    )
    expect(batch.matchedCount.value).toBe(12)
    expect(batch.matchedTruncated.value).toBe(false)

    // Card toggle & remove
    batch.toggleCard(1)
    batch.toggleCard(2)
    expect(batch.isCardSelected(1)).toBe(true)
    expect(batch.selectedCount.value).toBe(2)
    expect(batch.hasSelection.value).toBe(true)
    expect(batch.scope.value).toEqual({
      novel_ids: [1, 2],
    })
    batch.removeCard(2)
    expect(batch.selectedIds.value).toEqual(new Set([1]))

    // Unfiltered clear wipes all without hitting matchNovelIds
    const clearAllRes = await batch.clearSelectionInScope()
    expect(clearAllRes).toEqual({ removed: 1, remaining: 0 })
    expect(apiMocks.matchNovelIds).not.toHaveBeenCalled()

    // Accumulate picks across filter changes and union via selectAllMatched
    batch.toggleCard(1)
    filters.keyword = 'fantasy'
    await nextTick()
    await flushPromises(5)

    const selectRes = await batch.selectAllMatched()
    expect(selectRes).toEqual({ added: 2, total: 2 })
    expect(batch.selectedIds.value).toEqual(new Set([1, 2, 3]))

    // Filtered clear removes only matching_ids [1, 3], preserving 2
    const scopedRes = await batch.clearSelectionInScope()
    expect(apiMocks.matchNovelIds).toHaveBeenCalledWith({
      novel_ids: [1, 2, 3],
      keyword: 'fantasy',
      min_like: 0,
      min_text: 0,
      exclude_blocked: undefined,
    })
    expect(scopedRes).toEqual({ removed: 2, remaining: 1 })
    expect(batch.selectedIds.value).toEqual(new Set([2]))

    // Exit clears selection
    batch.exit()
    expect(batch.isBatchMode.value).toBe(false)
    expect(batch.selectedCount.value).toBe(0)
    expect(batch.scope.value).toBeNull()

    wrapper.unmount()
  })

  it('keeps viewLoading true while sortNovelIds is pending and guards stale sort responses with seq', async () => {
    let resolveFirstSort!: (val: { ids: number[]; total: number }) => void
    let resolveSecondSort!: (val: { ids: number[]; total: number }) => void

    apiMocks.sortNovelIds
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirstSort = resolve
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecondSort = resolve
          }),
      )

    apiMocks.getNovelsByIds.mockResolvedValue({ novels: [makeNovel(2), makeNovel(1)] })

    const filters = ref<NovelFilters>({
      keyword: '',
      order_by: 'like',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    })
    const afterLayout = vi.fn()

    const { result: batch, wrapper } = await mountWithQuery(() =>
      useBatchMode(filters, { afterLayout }),
    )
    batch.enter()
    batch.toggleCard(1)
    batch.toggleCard(2)
    await nextTick()

    const togglePromise = batch.toggleViewSelected()
    await nextTick()

    // While sortNovelIds is in-flight, viewLoading must be true (prevents EmptyState / 0/0 flash)
    expect(batch.viewingSelected.value).toBe(true)
    expect(batch.viewLoading.value).toBe(true)

    // Trigger a second sort before the first resolves
    filters.value = { ...filters.value, order_direction: 'ASC' }
    await nextTick()

    // Resolve the second (newer) sort first, then the stale first sort
    resolveSecondSort({ ids: [2, 1], total: 2 })
    await flushPromises(5)

    resolveFirstSort({ ids: [1, 2], total: 2 })
    await togglePromise
    await nextTick()

    expect(apiMocks.getNovelsByIds).toHaveBeenCalledTimes(1)
    expect(apiMocks.getNovelsByIds).toHaveBeenCalledWith([2, 1])
    expect(batch.viewLoading.value).toBe(false)
    expect(batch.selectedNovels.value.map((n) => n.id)).toEqual([2, 1])
    expect(afterLayout).toHaveBeenCalled()

    wrapper.unmount()
  })

  it('discards stale getNovelsByIds responses when view is re-sorted or closed mid-flight', async () => {
    let resolveFirst!: (val: { novels: Novel[] }) => void
    let resolveSecond!: (val: { novels: Novel[] }) => void
    let resolveThird!: (val: { novels: Novel[] }) => void

    apiMocks.getNovelsByIds
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecond = resolve
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveThird = resolve
          }),
      )

    const filters = makeFilters({ order_by: 'id', order_direction: 'DESC' })
    const { result: batch, wrapper } = await mountWithQuery(() => useBatchMode(filters))

    batch.enter()
    batch.toggleCard(1)
    batch.toggleCard(2)
    await nextTick()

    // First load starts with [2, 1]; before it resolves, flip direction to ASC ([1, 2])
    const firstToggle = batch.toggleViewSelected()
    await nextTick()
    expect(apiMocks.getNovelsByIds).toHaveBeenCalledWith([2, 1])

    filters.order_direction = 'ASC'
    await nextTick()
    expect(apiMocks.getNovelsByIds).toHaveBeenCalledWith([1, 2])

    resolveSecond({ novels: [makeNovel(1), makeNovel(2)] })
    await flushPromises(5)

    resolveFirst({ novels: [makeNovel(2), makeNovel(1)] })
    await firstToggle
    await nextTick()

    expect(batch.selectedNovels.value.map((n) => n.id)).toEqual([1, 2])

    // Closing mid-flight also discards late response
    filters.order_direction = 'DESC'
    await nextTick()
    expect(batch.viewLoading.value).toBe(true)

    await batch.toggleViewSelected() // closes view
    expect(batch.viewingSelected.value).toBe(false)

    resolveThird({ novels: [makeNovel(2), makeNovel(1)] })
    await flushPromises(5)

    expect(batch.viewingSelected.value).toBe(false)
    expect(batch.selectedNovels.value).toEqual([])

    wrapper.unmount()
  })

  it('paginates selected novels by DEFAULT_PAGE_SIZE, loads more, and surfaces loadMore errors', async () => {
    const total = DEFAULT_PAGE_SIZE + 5
    const ids = Array.from({ length: total }, (_, i) => i + 1)
    const filters = makeFilters({ order_by: 'random' })

    const { result: batch, wrapper } = await mountWithQuery(() => useBatchMode(filters))
    batch.enter()
    for (const id of ids) batch.toggleCard(id)
    await nextTick()

    await batch.toggleViewSelected()
    expect(apiMocks.sortNovelIds).not.toHaveBeenCalled()
    expect(apiMocks.getNovelsByIds).toHaveBeenCalledTimes(1)
    expect(apiMocks.getNovelsByIds).toHaveBeenLastCalledWith(ids.slice(0, DEFAULT_PAGE_SIZE))
    expect(batch.viewShownCount.value).toBe(DEFAULT_PAGE_SIZE)
    expect(batch.viewTotalCount.value).toBe(total)
    expect(batch.viewHasMore.value).toBe(true)

    // Load more error notifies via toast and leaves viewHasMore true for retry
    apiMocks.getNovelsByIds.mockRejectedValueOnce(new Error('network timeout'))
    await batch.loadMoreSelected()
    expect(toastMocks.error).toHaveBeenCalledWith('network timeout')
    expect(batch.viewLoadingMore.value).toBe(false)
    expect(batch.viewHasMore.value).toBe(true)

    // Retry loadMoreSelected succeeds and reaches end
    await batch.loadMoreSelected()
    expect(apiMocks.getNovelsByIds).toHaveBeenLastCalledWith(ids.slice(DEFAULT_PAGE_SIZE))
    expect(batch.viewShownCount.value).toBe(total)
    expect(batch.viewHasMore.value).toBe(false)

    // Extra loadMoreSelected call when no more data is a no-op
    const callsBefore = apiMocks.getNovelsByIds.mock.calls.length
    await batch.loadMoreSelected()
    expect(apiMocks.getNovelsByIds).toHaveBeenCalledTimes(callsBefore)

    wrapper.unmount()
  })

  it('removes deselected/deleted cards from view, auto-loads next page when empty, and closes when exhausted', async () => {
    const total = DEFAULT_PAGE_SIZE + 1
    const ids = Array.from({ length: total }, (_, i) => i + 1)
    const filters = makeFilters({ order_by: 'random' })
    const afterLayout = vi.fn()

    const { result: batch, wrapper } = await mountWithQuery(() =>
      useBatchMode(filters, { afterLayout }),
    )
    batch.enter()
    for (const id of ids) batch.toggleCard(id)
    await nextTick()

    await batch.toggleViewSelected()
    expect(batch.viewShownCount.value).toBe(DEFAULT_PAGE_SIZE)
    expect(batch.viewHasMore.value).toBe(true)

    // Remove all but 1 card on page 1 (via toggleCard & removeCard)
    for (let id = 1; id < DEFAULT_PAGE_SIZE; id++) {
      batch.toggleCard(id)
    }
    expect(batch.viewShownCount.value).toBe(1)
    expect(batch.viewTotalCount.value).toBe(2)
    expect(batch.viewHasMore.value).toBe(true)

    // Removing the last visible card on page 1 auto-triggers loadMoreSelected for page 2
    batch.removeCard(DEFAULT_PAGE_SIZE)
    await flushPromises(5)
    await nextTick()

    expect(apiMocks.getNovelsByIds).toHaveBeenCalledTimes(2)
    expect(apiMocks.getNovelsByIds).toHaveBeenLastCalledWith([total])
    expect(batch.viewShownCount.value).toBe(1)
    expect(batch.viewTotalCount.value).toBe(1)
    expect(batch.viewHasMore.value).toBe(false)
    expect(batch.viewingSelected.value).toBe(true)

    // Removing the final card closes the selected view cleanly
    batch.toggleCard(total)
    await flushPromises(5)
    await nextTick()

    expect(batch.hasSelection.value).toBe(false)
    expect(batch.viewingSelected.value).toBe(false)
    expect(batch.selectedNovels.value).toEqual([])
    expect(afterLayout).toHaveBeenCalled()

    wrapper.unmount()
  })

  it('prunes stale selected IDs dropped by sortNovelIds or getNovelsByIds and closes if none remain', async () => {
    const filters = makeFilters({ order_by: 'like', order_direction: 'DESC' })
    const { result: batch, wrapper } = await mountWithQuery(() => useBatchMode(filters))

    batch.enter()
    batch.toggleCard(1)
    batch.toggleCard(2)
    batch.toggleCard(3)
    await nextTick()

    // Backend sort drops missing ID 2
    apiMocks.sortNovelIds.mockResolvedValueOnce({ ids: [3, 1], total: 2 })
    await batch.toggleViewSelected()

    expect(batch.selectedIds.value).toEqual(new Set([1, 3]))
    expect(batch.selectedCount.value).toBe(2)
    expect(batch.viewTotalCount.value).toBe(2)
    expect(batch.selectedNovels.value.map((n) => n.id)).toEqual([3, 1])

    // Close and switch to random order, where getNovelsByIds drops missing ID 3
    await batch.toggleViewSelected()
    filters.order_by = 'random'
    await nextTick()

    apiMocks.getNovelsByIds.mockResolvedValueOnce({ novels: [makeNovel(1)] })
    await batch.toggleViewSelected()

    expect(batch.selectedIds.value).toEqual(new Set([1]))
    expect(batch.viewTotalCount.value).toBe(1)
    expect(batch.viewShownCount.value).toBe(1)
    expect(batch.viewHasMore.value).toBe(false)

    // If all remaining IDs are missing from backend, prune and close view cleanly
    await batch.toggleViewSelected()
    apiMocks.getNovelsByIds.mockResolvedValueOnce({ novels: [] })
    await batch.toggleViewSelected()
    await nextTick()

    expect(batch.selectedIds.value.size).toBe(0)
    expect(batch.viewingSelected.value).toBe(false)

    wrapper.unmount()
  })

  it('patches novels/states in selected view, syncs shell.isInCollectionView, and closes on load/sort failure', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })
    const filters = makeFilters({ order_by: 'like', order_direction: 'DESC' })

    let batch!: ReturnType<typeof useBatchMode>
    let shell!: ReturnType<typeof useNovelShellState>

    const Child = defineComponent({
      setup() {
        shell = useNovelShellState()
        batch = useBatchMode(filters)
        return () => h('div')
      },
    })
    const Parent = defineComponent({
      setup() {
        useProvideNovelShellState()
        return () => h(Child)
      },
    })

    const wrapper = mount(Parent, {
      global: {
        plugins: [[VueQueryPlugin, { queryClient }]],
      },
    })

    expect(shell.isInCollectionView.value).toBe(false)

    batch.enter()
    batch.toggleCard(1)
    batch.toggleCard(2)
    apiMocks.getNovelsByIds.mockResolvedValueOnce({
      novels: [
        makeNovel(1, { author_id: 10, title: 'Old 1' }),
        makeNovel(2, { author_id: 10, title: 'Old 2' }),
      ],
    })
    await batch.toggleViewSelected()
    await nextTick()

    expect(batch.viewingSelected.value).toBe(true)
    expect(shell.isInCollectionView.value).toBe(true)

    // Patch metadata & state fields
    batch.patchSelectedNovel(makeNovel(1, { author_id: 10, title: 'New 1' }))
    batch.patchSelectedNovelState({ id: 1, field: 'is_favourite', value: true })
    batch.patchSelectedNovelState({ id: 1, field: 'is_special_follow', value: true }, 10)

    expect(batch.selectedNovels.value[0]).toMatchObject({
      title: 'New 1',
      is_favourite: true,
      is_special_follow: true,
    })
    expect(batch.selectedNovels.value[1]).toMatchObject({
      is_favourite: false,
      is_special_follow: true,
    })

    // Re-sort failure shows toast and closes view
    apiMocks.sortNovelIds.mockRejectedValueOnce(new Error('sort failed'))
    filters.order_direction = 'ASC'
    await flushPromises(5)
    await nextTick()

    expect(toastMocks.error).toHaveBeenCalledWith('sort failed')
    expect(batch.viewingSelected.value).toBe(false)
    expect(shell.isInCollectionView.value).toBe(false)

    // Initial toggle failure shows toast and closes view
    apiMocks.sortNovelIds.mockRejectedValueOnce(new Error('load failed'))
    await batch.toggleViewSelected()
    await nextTick()

    expect(toastMocks.error).toHaveBeenCalledWith('load failed')
    expect(batch.viewingSelected.value).toBe(false)
    expect(shell.isInCollectionView.value).toBe(false)

    // Exiting batch mode while viewingSelected resets view & shell state
    apiMocks.sortNovelIds.mockResolvedValueOnce({ ids: [1, 2], total: 2 })
    apiMocks.getNovelsByIds.mockResolvedValueOnce({ novels: [makeNovel(1), makeNovel(2)] })
    await batch.toggleViewSelected()
    await nextTick()
    expect(batch.viewingSelected.value).toBe(true)

    batch.exit()
    await nextTick()
    expect(batch.isBatchMode.value).toBe(false)
    expect(batch.viewingSelected.value).toBe(false)
    expect(shell.isInCollectionView.value).toBe(false)

    wrapper.unmount()
  })
})
