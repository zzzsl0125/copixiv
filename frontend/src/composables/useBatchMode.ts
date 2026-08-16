import { computed, ref, watch } from 'vue'
import { novelApi } from '../api'
import type { NovelFilters, BatchScope } from '../types'

/**
 * Batch-mode state machine.
 *
 * Selection model (per the user's design):
 * - The search list is only a *picking surface*: searching, filtering and
 *   paging never touch the selection.
 * - The selection is a plain ID set that lives across filter changes —
 *   pick some A-novels, switch to search B, pick more; all accumulate.
 * - 「全选匹配」is a bulk-ADD convenience: it snapshots the currently
 *   matched IDs (server-resolved, so pages beyond what is loaded are
 *   included) into the selection as a union.
 * - Operations act on the ID set; nothing selected ⇒ operation buttons
 *   are disabled and the bar prompts the user to narrow the scope first.
 */
export function useBatchMode(filters: NovelFilters) {
  const isBatchMode = ref(false)
  const selectedIds = ref<Set<number>>(new Set())
  const matchedCount = ref(0)
  const countLoading = ref(false)
  const selectAllLoading = ref(false)

  /** Current filters actively narrow the list (vs. browsing the whole library). */
  const hasFilter = computed(
    () => !!(filters.keyword.trim() || filters.min_like || filters.min_text),
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

  let countSeq = 0

  async function refreshMatchedCount() {
    const seq = ++countSeq
    countLoading.value = true
    try {
      const result = await novelApi.countNovels({
        keyword: filters.keyword.trim() || undefined,
        min_like: filters.min_like,
        min_text: filters.min_text,
      })
      if (seq !== countSeq) return
      matchedCount.value = result.total
    } catch {
      if (seq === countSeq) matchedCount.value = 0
    } finally {
      if (seq === countSeq) countLoading.value = false
    }
  }

  watch(
    [
      isBatchMode,
      () => filters.keyword,
      () => filters.min_like,
      () => filters.min_text,
    ],
    () => {
      if (isBatchMode.value) void refreshMatchedCount()
    },
  )

  function enter() {
    isBatchMode.value = true
  }

  function exit() {
    isBatchMode.value = false
    clearSelection()
  }

  function clearSelection() {
    selectedIds.value = new Set()
  }

  /**
   * Scoped clear: remove only the selected IDs that belong to the CURRENT
   * search scope.  Picks accumulated from other searches survive — the
   * global clear lives in the 「查看已选」view.  No active filter means
   * the scope is the whole library, so everything goes.
   *
   * Returns ``{ removed, remaining }`` (or null when nothing was selected).
   */
  async function clearSelectionInScope() {
    const ids = [...selectedIds.value]
    if (ids.length === 0) return null
    if (!hasFilter.value) {
      const removed = ids.length
      selectedIds.value = new Set()
      return { removed, remaining: 0 }
    }
    const result = await novelApi.matchNovelIds({
      novel_ids: ids,
      keyword: filters.keyword.trim() || undefined,
      min_like: filters.min_like,
      min_text: filters.min_text,
    })
    const toRemove = new Set(result.matching_ids)
    const next = new Set(selectedIds.value)
    let removed = 0
    for (const id of toRemove) {
      if (next.delete(id)) removed++
    }
    selectedIds.value = next
    return { removed, remaining: next.size }
  }

  /** Bulk-add the currently matched IDs into the selection (union). */
  async function selectAllMatched() {
    if (selectAllLoading.value) return null
    selectAllLoading.value = true
    try {
      const result = await novelApi.getNovelIds({
        keyword: filters.keyword.trim() || undefined,
        min_like: filters.min_like,
        min_text: filters.min_text,
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
    } finally {
      selectAllLoading.value = false
    }
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
    countLoading,
    selectAllLoading,
    hasFilter,
    selectedCount,
    hasSelection,
    scope,
    enter,
    exit,
    toggleCard,
    isCardSelected,
    selectAllMatched,
    clearSelection,
    clearSelectionInScope,
    refreshMatchedCount,
  }
}

export type BatchMode = ReturnType<typeof useBatchMode>
