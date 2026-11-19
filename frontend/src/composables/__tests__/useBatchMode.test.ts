import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, reactive, ref } from 'vue'
import type { NovelFilters } from '../../types'
import { flushPromises, mountWithQuery } from '../../test-utils/queryWrapper'

const apiMocks = vi.hoisted(() => ({
  countNovels: vi.fn(),
  getNovelIds: vi.fn(),
  matchNovelIds: vi.fn(),
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
    },
  }
})

vi.mock('../useSystem', () => ({
  useSystem: () => ({
    systemConfig: ref(null),
    isConfigReady: ref(true),
  }),
}))

vi.mock('../useToast', () => ({
  useToast: () => toastMocks,
}))

import { useBatchMode } from '../useBatchMode'

describe('useBatchMode (Vue Query)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.countNovels.mockResolvedValue({ total: 12, excluded: 0, truncated: false })
    apiMocks.getNovelIds.mockResolvedValue({ ids: [2, 3], total: 2, truncated: false })
    apiMocks.matchNovelIds.mockResolvedValue({ matching_ids: [1, 3] })
  })

  it('gates count query on batch mode and manages union/scoped selection', async () => {
    const filters = reactive<NovelFilters>({
      keyword: '',
      min_like: 0,
      min_text: 0,
      order_by: 'id',
      order_direction: 'DESC',
    })

    const { result: batch, wrapper } = await mountWithQuery(() => useBatchMode(filters))
    expect(batch.isBatchMode.value).toBe(false)
    expect(batch.scope.value).toBeNull()
    expect(apiMocks.countNovels).not.toHaveBeenCalled()

    batch.enter()
    await nextTick()
    await flushPromises(5)

    expect(apiMocks.countNovels).toHaveBeenCalledWith(
      {
        keyword: undefined,
        min_like: 0,
        min_text: 0,
        exclude_blocked: true,
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
      mode: 'ids',
      novel_ids: [1, 2],
      excluded_ids: [],
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
    expect(selectRes).toEqual({ added: 2, total: 2, truncated: false })
    expect(batch.selectedIds.value).toEqual(new Set([1, 2, 3]))

    // Filtered clear removes only matching_ids [1, 3], preserving 2
    const scopedRes = await batch.clearSelectionInScope()
    expect(apiMocks.matchNovelIds).toHaveBeenCalledWith({
      novel_ids: [1, 2, 3],
      keyword: 'fantasy',
      min_like: 0,
      min_text: 0,
      exclude_blocked: true,
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
})
