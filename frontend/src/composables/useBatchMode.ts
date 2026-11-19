import { computed, ref, toValue, type MaybeRefOrGetter } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { getApiErrorMessage, novelApi, queryKeys } from '../api'
import { shouldExcludeBlocked } from '../config'
import type { BatchScope, NovelFilters } from '../types'
import { useNovelShellState } from './useNovelShellState'
import { useSystem } from './useSystem'
import { useToast } from './useToast'

/**
 * Batch-mode state machine backed by shared shell UI state + Vue Query count.
 */
export function useBatchMode(filtersInput: MaybeRefOrGetter<NovelFilters>) {
  const { systemConfig } = useSystem()
  const shell = useNovelShellState()
  const toast = useToast()

  const filters = computed(() => toValue(filtersInput))
  const isBatchMode = shell.isBatchMode
  const selectedIds = shell.selectedIds
  const selectAllLoading = ref(false)

  const excludeBlocked = () => shouldExcludeBlocked(filters.value.keyword, systemConfig.value)

  const countParams = computed(() => ({
    keyword: filters.value.keyword.trim() || undefined,
    min_like: filters.value.min_like,
    min_text: filters.value.min_text,
    exclude_blocked: excludeBlocked(),
  }))

  const countQuery = useQuery({
    queryKey: computed(() => queryKeys.novelsCount(countParams.value)),
    queryFn: ({ signal }) => novelApi.countNovels(countParams.value, signal),
    enabled: isBatchMode,
    staleTime: 10_000,
  })

  const matchedCount = computed(() => countQuery.data.value?.total ?? 0)
  const matchedTruncated = computed(() => countQuery.data.value?.truncated ?? false)
  const countLoading = computed(() => countQuery.isFetching.value)

  /** Current filters actively narrow the list (vs. browsing the whole library). */
  const hasFilter = computed(
    () => !!(filters.value.keyword.trim() || filters.value.min_like || filters.value.min_text),
  )

  const selectedCount = computed(() => selectedIds.value.size)
  const hasSelection = computed(() => selectedCount.value > 0)

  /** Effective API scope — null when nothing is selected. */
  const scope = computed<BatchScope | null>(() => {
    if (!hasSelection.value) return null
    return {
      mode: 'ids',
      novel_ids: [...selectedIds.value],
      excluded_ids: [],
    }
  })

  function refreshMatchedCount() {
    return countQuery.refetch()
  }

  function enter() {
    shell.enterBatchMode()
  }

  function exit() {
    shell.exitBatchMode()
  }

  function clearSelection() {
    shell.clearSelection()
  }

  async function clearSelectionInScope() {
    const ids = [...selectedIds.value]
    if (ids.length === 0) return null
    if (!hasFilter.value) {
      const removed = ids.length
      selectedIds.value = new Set()
      return { removed, remaining: 0 }
    }
    try {
      const result = await novelApi.matchNovelIds({
        novel_ids: ids,
        keyword: filters.value.keyword.trim() || undefined,
        min_like: filters.value.min_like,
        min_text: filters.value.min_text,
        exclude_blocked: excludeBlocked(),
      })
      const toRemove = new Set(result.matching_ids)
      const next = new Set(selectedIds.value)
      let removed = 0
      for (const id of toRemove) {
        if (next.delete(id)) removed++
      }
      selectedIds.value = next
      return { removed, remaining: next.size }
    } catch (err: unknown) {
      toast.error(getApiErrorMessage(err, '清除选择失败'))
      return null
    }
  }

  async function selectAllMatched() {
    if (selectAllLoading.value) return null
    selectAllLoading.value = true
    try {
      const result = await novelApi.getNovelIds({
        keyword: filters.value.keyword.trim() || undefined,
        min_like: filters.value.min_like,
        min_text: filters.value.min_text,
        exclude_blocked: excludeBlocked(),
      })
      const next = new Set(selectedIds.value)
      let added = 0
      for (const id of result.ids) {
        if (!next.has(id)) {
          next.add(id)
          added++
        }
      }
      selectedIds.value = next
      return { added, total: result.total, truncated: result.truncated }
    } catch (err: unknown) {
      toast.error(getApiErrorMessage(err, '全选匹配失败'))
      return null
    } finally {
      selectAllLoading.value = false
    }
  }

  function removeCard(id: number) {
    if (!selectedIds.value.has(id)) return
    const next = new Set(selectedIds.value)
    next.delete(id)
    selectedIds.value = next
  }

  function toggleCard(id: number) {
    const next = new Set(selectedIds.value)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    selectedIds.value = next
  }

  function isCardSelected(id: number): boolean {
    return selectedIds.value.has(id)
  }

  return {
    isBatchMode,
    selectedIds,
    matchedCount,
    matchedTruncated,
    countLoading,
    selectAllLoading,
    hasFilter,
    selectedCount,
    hasSelection,
    scope,
    enter,
    exit,
    toggleCard,
    removeCard,
    isCardSelected,
    selectAllMatched,
    clearSelection,
    clearSelectionInScope,
    refreshMatchedCount,
  }
}

export type BatchMode = ReturnType<typeof useBatchMode>
