import { computed, nextTick, ref, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { getApiErrorMessage, novelApi, queryKeys } from '../api'
import { DEFAULT_PAGE_SIZE, excludeBlockedOverride } from '../config'
import type { BatchScope, Novel, NovelFilters, OrderBy } from '../types'
import { useNovelShellState } from './useNovelShellState'
import { useSources } from './useSources'
import { useToast } from './useToast'

export interface UseBatchModeOptions {
  /** 布局重测回调（页面传入 useVirtualMasonry 的 measure）。 */
  afterLayout?: () => void
}

/**
 * Batch-mode state machine backed by shared shell UI state, Vue Query count,
 * and a paged "view selected" collection view.
 */
export function useBatchMode(
  filtersInput: MaybeRefOrGetter<NovelFilters>,
  options?: UseBatchModeOptions,
) {
  const shell = useNovelShellState()
  const toast = useToast()
  const { shelves } = useSources()

  const filters = computed(() => toValue(filtersInput))
  const isBatchMode = shell.isBatchMode
  const selectedIds = shell.selectedIds
  const selectAllLoading = ref(false)

  const extraShelves = computed(() => shelves.value.map((entry) => entry.shelf))
  const effectiveKeyword = computed(() => filters.value.keyword.trim())
  const excludeBlocked = () => excludeBlockedOverride(filters.value.keyword, extraShelves.value)

  const countParams = computed(() => ({
    keyword: effectiveKeyword.value,
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
      novel_ids: [...selectedIds.value],
    }
  })

  // ---- 「查看已选」分页视图状态 ----
  const viewingSelected = ref(false)
  const viewIds = ref<number[]>([])
  const selectedNovels = ref<Novel[]>([])
  const viewOffset = ref(0)
  const viewSorting = ref(false)
  const viewPageLoading = ref(false)
  const viewLoadingMore = ref(false)
  let viewSeq = 0

  const viewLoading = computed(() => viewSorting.value || viewPageLoading.value)
  const viewHasMore = computed(() => viewOffset.value < viewIds.value.length)
  const viewShownCount = computed(() => selectedNovels.value.length)
  const viewTotalCount = computed(() => viewIds.value.length)

  function pruneStaleSelectedIds(staleIds: Iterable<number>) {
    const next = new Set(selectedIds.value)
    let changed = false
    for (const id of staleIds) {
      if (next.delete(id)) changed = true
    }
    if (changed) selectedIds.value = next
  }

  function resetSelectedView() {
    viewSeq++
    viewingSelected.value = false
    viewSorting.value = false
    viewPageLoading.value = false
    viewLoadingMore.value = false
    viewIds.value = []
    selectedNovels.value = []
    viewOffset.value = 0
  }

  async function closeView() {
    resetSelectedView()
    await nextTick()
    options?.afterLayout?.()
  }

  /** `random` 在已选集合视图内禁用（保持勾选顺序），其余排序字段原样透传。 */
  function scopeOrderBy(): Exclude<OrderBy, 'random'> | undefined {
    const o = filters.value.order_by
    return o === 'random' ? undefined : o
  }

  async function fetchSelectedPage(seq: number) {
    while (viewOffset.value < viewIds.value.length) {
      const pageIds = viewIds.value.slice(viewOffset.value, viewOffset.value + DEFAULT_PAGE_SIZE)
      if (pageIds.length === 0) return
      const result = await novelApi.getNovelsByIds(pageIds)
      if (seq !== viewSeq) return

      const returnedIds = new Set(result.novels.map((n) => n.id))
      const missingIds = pageIds.filter((id) => !returnedIds.has(id))
      if (missingIds.length > 0) {
        const missingSet = new Set(missingIds)
        viewIds.value = viewIds.value.filter((id) => !missingSet.has(id))
        pruneStaleSelectedIds(missingIds)
      }

      const activeIds = new Set(viewIds.value)
      const keptCount = pageIds.filter((id) => activeIds.has(id)).length
      viewOffset.value += keptCount
      const added = result.novels.filter((n) => activeIds.has(n.id))
      selectedNovels.value.push(...added)

      if (added.length > 0 || viewOffset.value >= viewIds.value.length) break
    }
  }

  async function startSelectedPages(idList: number[], seq: number) {
    viewIds.value = [...idList]
    selectedNovels.value = []
    viewOffset.value = 0
    viewLoadingMore.value = false
    if (viewIds.value.length === 0) {
      viewPageLoading.value = false
      return
    }
    viewPageLoading.value = true
    try {
      await fetchSelectedPage(seq)
    } finally {
      if (seq === viewSeq) viewPageLoading.value = false
    }
  }

  async function reloadSelectedIds(seq: number): Promise<boolean> {
    const ids = [...selectedIds.value]
    if (ids.length === 0) return false
    const orderBy = scopeOrderBy()
    viewSorting.value = true
    try {
      let sortedIds = ids
      if (orderBy === 'id') {
        const dir = filters.value.order_direction === 'ASC' ? 1 : -1
        sortedIds = [...ids].sort((a, b) => dir * (Math.abs(a) - Math.abs(b)))
      } else if (orderBy) {
        const result = await novelApi.sortNovelIds(ids, orderBy, filters.value.order_direction)
        if (seq !== viewSeq) return false
        const valid = new Set(result.ids)
        const stale = ids.filter((id) => !valid.has(id))
        if (stale.length > 0) pruneStaleSelectedIds(stale)
        sortedIds = result.ids
      }
      await startSelectedPages(sortedIds, seq)
      if (seq !== viewSeq) return false
      if (selectedIds.value.size === 0) {
        await closeView()
        return false
      }
      return true
    } finally {
      if (seq === viewSeq) viewSorting.value = false
    }
  }

  async function refreshSelectedView(errorFallback: string) {
    const seq = ++viewSeq
    try {
      const active = await reloadSelectedIds(seq)
      if (!active) return
      window.scrollTo({ top: 0 })
      await nextTick()
      options?.afterLayout?.()
    } catch (err: unknown) {
      if (seq !== viewSeq) return
      toast.error(getApiErrorMessage(err, errorFallback))
      await closeView()
    }
  }

  async function toggleViewSelected() {
    if (viewingSelected.value) {
      await closeView()
      return
    }
    if (selectedIds.value.size === 0) return
    viewingSelected.value = true
    await refreshSelectedView('加载已选小说失败')
  }

  async function loadMoreSelected() {
    if (!viewingSelected.value || !viewHasMore.value || viewLoadingMore.value || viewLoading.value) {
      return
    }
    const seq = viewSeq
    viewLoadingMore.value = true
    try {
      await fetchSelectedPage(seq)
      if (seq === viewSeq && selectedIds.value.size === 0) {
        await closeView()
      }
    } catch (err: unknown) {
      if (seq !== viewSeq) return
      toast.error(getApiErrorMessage(err, '加载更多已选小说失败'))
    } finally {
      if (seq === viewSeq) viewLoadingMore.value = false
    }
  }

  function removeIdFromView(id: number) {
    const idx = viewIds.value.indexOf(id)
    if (idx === -1) return
    viewIds.value.splice(idx, 1)
    if (idx < viewOffset.value) {
      viewOffset.value -= 1
      selectedNovels.value = selectedNovels.value.filter((n) => n.id !== id)
    }
  }

  function syncViewAfterRemoval(removedIds: Iterable<number>) {
    if (!viewingSelected.value) return
    for (const id of removedIds) {
      removeIdFromView(id)
    }
    if (selectedIds.value.size === 0) {
      void closeView()
      return
    }
    if (selectedNovels.value.length === 0 && viewHasMore.value) {
      void loadMoreSelected()
    }
    void nextTick().then(() => options?.afterLayout?.())
  }

  function enter() {
    shell.enterBatchMode()
  }

  function exit() {
    const wasBatchMode = isBatchMode.value
    shell.exitBatchMode()
    if (viewingSelected.value) {
      if (wasBatchMode) resetSelectedView()
      else void closeView()
    }
  }

  function clearSelection() {
    shell.clearSelection()
    if (viewingSelected.value) void closeView()
  }

  async function clearSelectionInScope() {
    const ids = [...selectedIds.value]
    if (ids.length === 0) return null
    if (!hasFilter.value) {
      const removed = ids.length
      selectedIds.value = new Set()
      if (viewingSelected.value) void closeView()
      return { removed, remaining: 0 }
    }
    try {
      const result = await novelApi.matchNovelIds({
        novel_ids: ids,
        keyword: effectiveKeyword.value,
        min_like: filters.value.min_like,
        min_text: filters.value.min_text,
        exclude_blocked: excludeBlocked(),
      })
      const next = new Set(selectedIds.value)
      const removedIds: number[] = []
      for (const id of result.matching_ids) {
        if (next.delete(id)) removedIds.push(id)
      }
      selectedIds.value = next
      syncViewAfterRemoval(removedIds)
      return { removed: removedIds.length, remaining: next.size }
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
        keyword: effectiveKeyword.value,
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
      return { added, total: result.total }
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
    syncViewAfterRemoval([id])
  }

  function toggleCard(id: number) {
    if (selectedIds.value.has(id)) {
      removeCard(id)
      return
    }
    const next = new Set(selectedIds.value)
    next.add(id)
    selectedIds.value = next
  }

  function isCardSelected(id: number): boolean {
    return selectedIds.value.has(id)
  }

  /** 元数据编辑保存后原地替换已选视图里的那一本（不重取）。 */
  function patchSelectedNovel(updated: Novel) {
    const index = selectedNovels.value.findIndex(
      (novel) =>
        novel.id === updated.id ||
        Boolean(
          updated.is_series &&
            updated.series_id != null &&
            novel.is_series &&
            novel.series_id === updated.series_id,
        ),
    )
    if (index >= 0) selectedNovels.value[index] = updated
  }

  /** Toggle a novel's state field (or author-wide special follow) in active selected view. */
  function patchSelectedNovelState(
    payload: {
      id: number
      field: 'is_favourite' | 'is_special_follow'
      value: boolean
      seriesId?: number | null
    },
    authorId?: number,
  ) {
    for (const novel of selectedNovels.value) {
      if (payload.field === 'is_special_follow' && authorId !== undefined) {
        if (novel.author_id === authorId) {
          novel.is_special_follow = payload.value
        }
      } else if (
        payload.field === 'is_favourite' &&
        payload.seriesId != null &&
        novel.is_series &&
        novel.series_id === payload.seriesId
      ) {
        novel.is_favourite = payload.value
      } else if (novel.id === payload.id) {
        novel[payload.field] = payload.value
      }
    }
  }

  watch(isBatchMode, (on) => {
    if (on) {
      void nextTick().then(() => options?.afterLayout?.())
      return
    }
    void closeView()
  })

  watch(hasSelection, (has) => {
    if (!has && viewingSelected.value) void closeView()
  })

  watch(
    viewingSelected,
    (active) => {
      shell.isInCollectionView.value = active
    },
    { immediate: true },
  )

  watch(
    [() => filters.value.order_by, () => filters.value.order_direction],
    ([o, d], [po, pd]) => {
      if (o === po && d === pd) return
      if (!viewingSelected.value) return
      void refreshSelectedView('重新排序失败')
    },
  )

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
    viewingSelected,
    selectedNovels,
    viewLoading,
    viewLoadingMore,
    viewHasMore,
    viewShownCount,
    viewTotalCount,
    enter,
    exit,
    toggleCard,
    removeCard,
    isCardSelected,
    selectAllMatched,
    clearSelection,
    clearSelectionInScope,
    toggleViewSelected,
    loadMoreSelected,
    patchSelectedNovel,
    patchSelectedNovelState,
  }
}

export type BatchMode = ReturnType<typeof useBatchMode>
