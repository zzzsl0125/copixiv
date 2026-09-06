<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Sidebar from './components/Sidebar.vue'
import ToastContainer from './components/ui/ToastContainer.vue'
import { useNovels, useSystem, useToast, useBatchMode } from './composables'
import { DEFAULT_MIN_LIKE, DEFAULT_MIN_TEXT } from './config'
import type { NovelFilters, BatchOperationResult } from './types'

const {
  novels,
  loading,
  error,
  noMoreData,
  filters,
  hasExcluded,
  loadNovels,
  handleSearch,
  handleLoadMore,
  handleCardSearch,
} = useNovels()

const { systemConfig, fetchConfig } = useSystem()
const { toasts, success: toastSuccess, warning: toastWarning, info: toastInfo } = useToast()
const configLoadedAndApplied = ref(false)

const isSidebarOpen = ref(false)
const route = useRoute()
const router = useRouter()
const activeSection = ref<'novels' | 'favourites' | 'special_follow' | null>('novels')

// 集合视图（查看已选/查看被排除）活跃时，侧边栏禁用「随机」排序
const inCollectionView = ref(false)

const isNovelsRoute = computed(() => route.path === '/')

// ---- batch mode (selection lives at App level; Sidebar toggles it,
// Novels renders the bar/cards/checkboxes) ----
const {
  isBatchMode,
  matchedCount,
  countLoading,
  selectAllLoading,
  selectedCount,
  hasSelection,
  hasFilter,
  scope: batchScope,
  enter: enterBatchMode,
  exit: exitBatchMode,
  toggleCard: toggleBatchCard,
  isCardSelected,
  selectAllMatched,
  clearSelection: clearBatchSelection,
  clearSelectionInScope,
} = useBatchMode(filters)

const handleToggleBatchMode = () => {
  if (isBatchMode.value) exitBatchMode()
  else enterBatchMode()
}

const handleBatchSelectAll = async () => {
  const result = await selectAllMatched()
  if (!result) return
  if (result.truncated) {
    toastWarning(
      `已全选匹配中的前 ${result.added} 篇（共匹配 ${result.total} 篇，` +
      `超过单次操作上限）。可继续搜索细分后再次全选，已选不会丢失。`,
    )
  } else {
    toastSuccess(`已将 ${result.added} 篇匹配小说加入选择（当前已选 ${selectedCount.value} 篇）`)
  }
}

/** Scoped clear (列表视图): remove only the picks inside the current scope. */
const handleBatchClearScope = async () => {
  const result = await clearSelectionInScope()
  if (!result) return
  if (result.remaining === 0) {
    toastInfo(`已清空全部选择（${result.removed} 篇）`)
  } else {
    toastInfo(
      `已清除当前范围勾选的 ${result.removed} 篇，` +
      `另有 ${result.remaining} 篇来自其他搜索仍保留在已选中`,
    )
  }
}

const handleBatchOperationSuccess = (payload: {
  operation: string
  result: BatchOperationResult
}) => {
  const { operation, result } = payload
  if (operation === 'delete') {
    toastSuccess(`已删除 ${result.affected} 篇小说`)
  } else if (operation === 'add_tags') {
    toastSuccess(`已为 ${result.affected} 篇小说添加标签`)
  } else if (operation === 'remove_tags') {
    toastSuccess(`已从 ${result.affected} 篇小说移除标签`)
  }
  clearBatchSelection()
  // Refresh the list in place (keep current filters & URL).
  handleSearch(undefined, { updateUrl: false })
}

const handleBatchTaskSubmitted = (payload: {
  operation: string
  task_id: number
  matched: number
}) => {
  const labels: Record<string, string> = {
    delete: '批量删除',
    add_tags: '批量添加标签',
    remove_tags: '批量移除标签',
    export: '批量导出',
  }
  const isExport = payload.operation === 'export'
  toastInfo(
    `${labels[payload.operation] ?? '批量操作'}已提交为后台任务 #${payload.task_id}` +
    `（共 ${payload.matched} 篇）。可关闭页面，到「任务管理」查看进度` +
    (isExport ? '，完成后在任务队列中点击下载。' : '。'),
  )
  clearBatchSelection()
}

const syncActiveSection = () => {
  if (filters.keyword === 'is_favourite:true;') {
    activeSection.value = 'favourites'
  } else if (filters.keyword === 'is_special_follow:true;') {
    activeSection.value = 'special_follow'
  } else if (filters.keyword) {
    activeSection.value = null
  } else {
    activeSection.value = 'novels'
  }
}

const applyConfigAndLoad = () => {
  if (configLoadedAndApplied.value) return

  const config = systemConfig.value
  if (config) {
    const urlParams = new URLSearchParams(window.location.search)
    if (!urlParams.has('min_like')) {
      filters.min_like = DEFAULT_MIN_LIKE
    }
    if (!urlParams.has('min_text')) {
      filters.min_text = DEFAULT_MIN_TEXT
    }
  }

  configLoadedAndApplied.value = true
  syncActiveSection()
  loadNovels()
}

watch(() => systemConfig.value, (newConfig) => {
  if (newConfig && !configLoadedAndApplied.value) {
    applyConfigAndLoad()
  }
})

// 全局「排除厌恶标签」开关在标签管理页变更后，回到列表页时立即生效
// （首次赋值 old=undefined 跳过，避免与初始加载重复）。
watch(
  () => systemConfig.value?.exclude_blocked_tag_novels,
  (val, old) => {
    if (val !== undefined && old !== undefined && configLoadedAndApplied.value) {
      handleSearch(undefined, { updateUrl: false })
    }
  },
)

watch(route, (to) => {
  if (to.path !== '/') {
    activeSection.value = null
  } else {
    syncActiveSection()
  }
})

const handleNovelStateChanged = (payload: { id: number; field: 'is_favourite' | 'is_special_follow'; value: number }) => {
  const novel = novels.value.find(n => n.id === payload.id)
  if (novel) novel[payload.field] = payload.value
}

const handleSectionSearch = (keyword: string | undefined, section: 'novels' | 'favourites' | 'special_follow') => {
  activeSection.value = section
  handleSearch(keyword, { setOrdering: true })
}

const handleResetToDefaults = () => {
  filters.keyword = ''
  filters.order_by = 'random'
  filters.order_direction = 'DESC'
  filters.min_like = DEFAULT_MIN_LIKE
  filters.min_text = DEFAULT_MIN_TEXT

  activeSection.value = 'novels'
  handleSearch(undefined, { setOrdering: false })
}

const handleLogoClick = () => {
  if (route.path !== '/') router.push('/')
  handleResetToDefaults()
}

onMounted(() => {
  if (route.path !== '/') {
    activeSection.value = null
  }
  // Load novels once the config attempt settles — success applies the
  // defaults, failure still lets the home page browse with URL/current
  // filters instead of waiting forever for a systemConfig that never comes.
  fetchConfig().then(() => {
    if (!configLoadedAndApplied.value) applyConfigAndLoad()
  })
})
</script>

<template>
  <div class="min-h-screen bg-gray-50 flex">
    <Sidebar
      :is-open="isSidebarOpen"
      :filters="filters"
      :show-filters="isNovelsRoute"
      :active-section="activeSection"
      :config-loaded-and-applied="configLoadedAndApplied"
      :is-batch-mode="isBatchMode"
      :random-disabled="isNovelsRoute && inCollectionView"
      @close="isSidebarOpen = false"
      @search="handleSectionSearch"
      @update:filters="($event: NovelFilters) => { Object.assign(filters, $event); handleSearch(); }"
      @reset-to-defaults="handleResetToDefaults"
      @toggle-batch-mode="handleToggleBatchMode"
    />

    <div class="flex-1 flex flex-col min-w-0">
      <router-view
        :is-sidebar-open="isSidebarOpen"
        :filters="filters"
        :novels="novels"
        :loading="loading"
        :error="error"
        :no-more-data="noMoreData"
        :has-excluded="hasExcluded"
        :batch-mode="isNovelsRoute ? isBatchMode : undefined"
        :matched-count="isNovelsRoute ? matchedCount : undefined"
        :count-loading="isNovelsRoute ? countLoading : undefined"
        :select-all-loading="isNovelsRoute ? selectAllLoading : undefined"
        :selected-count="isNovelsRoute ? selectedCount : undefined"
        :has-selection="isNovelsRoute ? hasSelection : undefined"
        :has-filter="isNovelsRoute ? hasFilter : undefined"
        :batch-scope="isNovelsRoute ? batchScope : undefined"
        :is-batch-selected="isNovelsRoute ? isCardSelected : undefined"
        @logo-click="handleLogoClick"
        @load-more="handleLoadMore"
        @search="(keyword?: string) => handleSearch(keyword, { setOrdering: true })"
        @card-search="handleCardSearch"
        @novel-state-changed="handleNovelStateChanged"
        @update:filters="($event: NovelFilters) => { Object.assign(filters, $event); handleSearch(); }"
        @toggle-sidebar="isSidebarOpen = !isSidebarOpen"
        @collection-view="(active: boolean) => (inCollectionView = active)"
        @batch-toggle-card="toggleBatchCard"
        @batch-select-all="handleBatchSelectAll"
        @batch-clear="clearBatchSelection"
        @batch-clear-scope="handleBatchClearScope"
        @batch-operation-success="handleBatchOperationSuccess"
        @batch-task-submitted="handleBatchTaskSubmitted"
      />
    </div>

    <ToastContainer :toasts="toasts" />
  </div>
</template>
