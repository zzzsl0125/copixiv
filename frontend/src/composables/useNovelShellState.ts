import { ref } from 'vue'
import { createInjectionState } from '@vueuse/core'

function createNovelShellState() {
  const isBatchMode = ref(false)
  const selectedIds = ref<Set<number>>(new Set())
  const isInCollectionView = ref(false)
  const refreshNonce = ref(0)

  function enterBatchMode() {
    isBatchMode.value = true
  }

  function clearSelection() {
    selectedIds.value = new Set()
  }

  function exitBatchMode() {
    isBatchMode.value = false
    clearSelection()
  }

  function toggleBatchMode() {
    if (isBatchMode.value) exitBatchMode()
    else enterBatchMode()
  }

  function bumpRefreshNonce() {
    refreshNonce.value += 1
  }

  return {
    isBatchMode,
    selectedIds,
    isInCollectionView,
    refreshNonce,
    enterBatchMode,
    exitBatchMode,
    toggleBatchMode,
    clearSelection,
    bumpRefreshNonce,
  }
}

export type NovelShellState = ReturnType<typeof createNovelShellState>

const [useProvideNovelShellState, useInjectedNovelShellState] =
  createInjectionState(createNovelShellState)

export { useProvideNovelShellState }

/**
 * Resolve the shared shell UI state (falls back to a fresh local state when
 * a component or composable is mounted standalone in unit tests).
 */
export function useNovelShellState(): NovelShellState {
  return useInjectedNovelShellState() ?? createNovelShellState()
}
