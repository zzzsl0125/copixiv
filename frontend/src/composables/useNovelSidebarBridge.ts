import { reactive, ref } from 'vue'
import type { SectionId } from '../config'
import type { NovelFilters } from '../types'

export interface NovelSidebarHandlers {
  sectionSearch: (keyword: string | undefined, section: SectionId) => void
  updateFilters: (patch: Partial<NovelFilters>) => void
  resetToDefaults: () => void
  toggleBatchMode: () => void
}

export interface PendingSidebarSection {
  keyword: string | undefined
  section: SectionId
}

const activeSection = ref<SectionId | null>(null)
const configLoadedAndApplied = ref(false)
const isBatchMode = ref(false)
const isInCollectionView = ref(false)
const sidebarFilters = reactive<NovelFilters>({
  keyword: '',
  order_by: 'random',
  order_direction: 'DESC',
  min_like: undefined,
  min_text: undefined,
})

let handlers: NovelSidebarHandlers | null = null
let pendingSection: PendingSidebarSection | null = null
let pendingBatchMode = false

/**
 * Shared navigation/filter bridge between the persistent shell (`App.vue` /
 * `Sidebar.vue`) and the route-scoped `Novels.vue` workspace.
 *
 * Keeps novel fetching, batch selection, and modals completely out of `App.vue`
 * while letting `Sidebar.vue` drive `/` cleanly from any route.
 */
export function useNovelSidebarBridge() {
  function register(nextHandlers: NovelSidebarHandlers) {
    handlers = nextHandlers
  }

  function unregister() {
    handlers = null
    activeSection.value = null
    configLoadedAndApplied.value = false
    isBatchMode.value = false
    isInCollectionView.value = false
  }

  function syncFilters(filters: NovelFilters) {
    sidebarFilters.keyword = filters.keyword
    sidebarFilters.order_by = filters.order_by
    sidebarFilters.order_direction = filters.order_direction
    sidebarFilters.min_like = filters.min_like
    sidebarFilters.min_text = filters.min_text
  }

  function dispatchSectionSearch(keyword: string | undefined, section: SectionId) {
    if (handlers) {
      handlers.sectionSearch(keyword, section)
      return
    }
    pendingSection = { keyword, section }
  }

  function dispatchUpdateFilters(patch: Partial<NovelFilters>) {
    if (handlers) handlers.updateFilters(patch)
  }

  function dispatchResetToDefaults() {
    if (handlers) handlers.resetToDefaults()
  }

  function dispatchToggleBatchMode() {
    if (handlers) {
      handlers.toggleBatchMode()
      return
    }
    pendingBatchMode = true
  }

  function consumePendingSection(): PendingSidebarSection | null {
    const next = pendingSection
    pendingSection = null
    return next
  }

  function consumePendingBatchMode(): boolean {
    const next = pendingBatchMode
    pendingBatchMode = false
    return next
  }

  return {
    activeSection,
    configLoadedAndApplied,
    isBatchMode,
    isInCollectionView,
    sidebarFilters,
    register,
    unregister,
    syncFilters,
    dispatchSectionSearch,
    dispatchUpdateFilters,
    dispatchResetToDefaults,
    dispatchToggleBatchMode,
    consumePendingSection,
    consumePendingBatchMode,
  }
}
