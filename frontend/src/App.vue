<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Sidebar from './components/Sidebar.vue'
import ToastContainer from './components/ui/ToastContainer.vue'
import UploadModal from './components/features/UploadModal.vue'
import { useNovels, useSystem, useToast, useBatchMode, useTaskStatus } from './composables'
import { SECTIONS, resolveSection, sectionFor, type SectionId } from './config'
import type { Novel, NovelFilters, BatchOperationResult, BatchMetadataResult } from './types'
import type { NovelsState, BatchState, NovelsCommand, BatchAction } from './views/Novels.vue'

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
const { toasts, success: toastSuccess, warning: toastWarning, info: toastInfo, error: toastError } = useToast()
// Global task-status singleton: one fetch on mount picks up rows that were
// already pending/running before the page loaded; it then polls only while
// something is active (no per-view polling loops).
const { startTaskStatusPolling } = useTaskStatus()
const configLoadedAndApplied = ref(false)

const isSidebarOpen = ref(false)
const route = useRoute()
const router = useRouter()
const activeSection = ref<SectionId | null>('novels')

/** 上传弹窗（不新增路由：docs/LIBRARY_DESIGN.md §1 上传入口） */
const isUploadModalOpen = ref(false)

// 集合视图（查看已选/查看被排除）活跃时，侧边栏禁用「随机」排序
const inCollectionView = ref(false)

const isNovelsRoute = computed(() => route.path === '/')

// ---- batch mode (selection lives at App level; Sidebar toggles it,
// Novels renders the bar/cards/checkboxes) ----
const {
  isBatchMode,
  matchedCount,
  matchedTruncated,
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

// ---- F1 粗粒度绑定：15 个散点状态收敛为两个对象（非首页路由 batch 传
// undefined，与原 isNovelsRoute ? … : undefined 语义逐项等价）----
const novelsState = computed<NovelsState>(() => ({
  novels: novels.value,
  loading: loading.value,
  error: error.value,
  noMoreData: noMoreData.value,
  hasExcluded: hasExcluded.value,
}))

const batchState = computed<BatchState | undefined>(() =>
  isNovelsRoute.value
    ? {
        mode: isBatchMode.value,
        matchedCount: matchedCount.value,
        matchedTruncated: matchedTruncated.value,
        countLoading: countLoading.value,
        selectAllLoading: selectAllLoading.value,
        selectedCount: selectedCount.value,
        hasSelection: hasSelection.value,
        hasFilter: hasFilter.value,
        scope: batchScope.value,
        isSelected: isCardSelected,
      }
    : undefined,
)

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

/**
 * 批量操作成功：按 operation 出 toast，然后清空选择并**原地重取**列表
 * （筛选与 URL 不动）。
 *
 * `result` 对 `update_metadata` 多带 `applicable` / `skipped`（本地书 / pixiv 书
 * 的构成），所以要按联合类型收：那两个数必须进 toast——批量编辑只作用于本地书，
 * pixiv 书一律跳过，"选了 10 篇只改了 4 篇"不说清跳过数就会被读成丢了 6 篇。
 * 至于改的是作者还是系列，这里拿到的是同一个响应（没有字段信息），
 * 所以文案用一句概括的「作者/系列」。
 */
const handleBatchOperationSuccess = (payload: {
  operation: string
  result: BatchOperationResult | BatchMetadataResult
}) => {
  const { operation, result } = payload
  if (operation === 'delete') {
    toastSuccess(`已删除 ${result.affected} 篇小说`)
  } else if (operation === 'add_tags') {
    toastSuccess(`已为 ${result.affected} 篇小说添加标签`)
  } else if (operation === 'remove_tags') {
    toastSuccess(`已从 ${result.affected} 篇小说移除标签`)
  } else if (operation === 'update_metadata') {
    const skipped = 'skipped' in result ? result.skipped : 0
    toastSuccess(
      `已修改 ${result.affected} 篇的作者/系列` +
      (skipped > 0 ? `（跳过 ${skipped} 篇 pixiv 书）` : ''),
    )
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

/**
 * section 状态机（docs/LIBRARY_DESIGN.md §5）：section 的全部事实——
 * keyword / 阈值 / 默认排序——都来自 config.ts 的 SECTIONS 表，
 * 侧边栏 emit、这里从 keyword 的反推、以及阈值三处同源。
 */
const syncActiveSection = () => {
  activeSection.value = resolveSection(filters.keyword)
}

/** 套用某 section 的默认排序（时间/点赞/随机会覆盖 URL 里的旧值）。 */
const applySectionOrdering = (id: SectionId) => {
  const section = SECTIONS[id]
  filters.order_by = section.order_by
  filters.order_direction = section.order_direction
}

/** 套用某个 section 的阈值与默认排序，并重取列表（入口：点击侧边栏 / 上传成功）。 */
const applySection = (id: SectionId) => {
  const section = SECTIONS[id]
  activeSection.value = id
  filters.min_like = section.min_like
  filters.min_text = section.min_text
  applySectionOrdering(id)
  handleSearch(undefined, { setOrdering: false })
}

const applyConfigAndLoad = () => {
  if (configLoadedAndApplied.value) return

  // URL 上已经带着一个 section 的 keyword（直达链接 / 后退前进后的刷新）
  // 时，用该 section 的阈值与排序，别套小说列表的默认值。
  const initialSection = resolveSection(filters.keyword)

  const config = systemConfig.value
  if (config) {
    const urlParams = new URLSearchParams(window.location.search)
    // URL 显式给值就尊重 URL；否则套**该 keyword 命中的 section** 的阈值——
    // 固定用 DEFAULT_MIN_LIKE/TEXT 会让 ?keyword=source:local; 走进「我的上传」
    // 却因 like=0 被 `like >= 500` 过滤干净（书架恒空）。
    const section = sectionFor(filters.keyword)
    if (!urlParams.has('min_like')) {
      filters.min_like = section.min_like
    }
    if (!urlParams.has('min_text')) {
      filters.min_text = section.min_text
    }
  }
  if (initialSection) applySectionOrdering(initialSection)

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

/**
 * keyword 是 section 的唯一判据：任何让 keyword 变化的路径都会重新反推
 * section，命中某 section 时顺带套上它的阈值与默认排序。
 *
 * 为什么不能只靠点击侧边栏时设置：
 * - popstate（useNovels.onPopState）直接改写 filters.keyword，不经过 App；
 * - 搜索历史选中的 `source:local;` 走 NovelHeader → NovelsCommand('search')，
 *   只调 handleSearch、不反推 section。
 * 这两条路径否则会「列表已是我的上传、侧边栏还高亮着小说列表」。
 */
watch(() => filters.keyword, (keyword) => {
  if (!configLoadedAndApplied.value) return
  const section = resolveSection(keyword)
  if (section && section !== 'novels') {
    // 直接给 filters 赋值（而非 handleSectionSearch）：keyword 已是当前值，
    // 不会与 watch 递归；命中 section 才重取，自由搜索交给调用方已有的 handleSearch。
    applySection(section)
  } else {
    activeSection.value = section
  }
})

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

/** 元数据编辑保存成功：用响应值**原地替换**主列表里的那一本。
 *
 * 集合视图（查看已选 / 被排除）的副本由 Novels.vue 自己 patch——两份都不
 * 重取列表：编辑是单本操作，重取会让瀑布流跳动、还多打一次请求（§6.3）。 */
const handleNovelUpdated = (updated: Novel) => {
  const novel = novels.value.find(n => n.id === updated.id)
  if (novel) Object.assign(novel, updated)
}

/** 侧边栏「阅览」一栏（我的收藏 / 特别关注 / 我的上传）→ 一次特殊搜索。
 *  签名与 Sidebar 的 `search` emit 对齐（keyword, section）；keyword 只是
 *  给 URL/历史看的展示值，真正的阈值与排序由 section 决定——故不再读它，
 *  统一以 SECTIONS[section].keyword 回填，保证点击与 URL 直达等价。
 *
 *  重取只走一条路：keyword 变化时由下面的 keyword watcher 负责 applySection，
 *  这里不再自己调一次——否则「赋值触发 watcher」+「本函数直接调用」会让每次
 *  点击发两次列表请求（App-sections.test.ts ① 用 toHaveBeenCalledTimes(1) 钉住）。
 *  只有 keyword 没变（在书架里再点同一项）时 watcher 不会触发，才由这里直接重取。 */
const handleSectionSearch = (_keyword: string | undefined, section: SectionId) => {
  const keyword = SECTIONS[section].keyword
  if (filters.keyword === keyword) {
    applySection(section)
    return
  }
  filters.keyword = keyword
}

const handleResetToDefaults = () => {
  // 「重置」= 回到小说列表（novels section）的出厂状态。
  filters.keyword = SECTIONS.novels.keyword
  filters.min_like = SECTIONS.novels.min_like
  filters.min_text = SECTIONS.novels.min_text
  filters.order_by = SECTIONS.novels.order_by
  filters.order_direction = SECTIONS.novels.order_direction

  activeSection.value = 'novels'
  handleSearch(undefined, { setOrdering: false })
}

// ---- 上传弹窗（我的上传）：写入成功后刷新当前 section ----
// 打开入口只有一处：列表顶部的 UploadBar → `novels-command {type:'upload'}` →
// handleNovelsCommand（侧边栏已不再挂上传按钮）。
const handleUploaded = () => {
  if (activeSection.value === 'uploads') {
    // 已在书架：原地按表里的默认排序重取（新书排最前），不叠加 URL 历史。
    applySection('uploads')
  } else {
    // 不在书架（如正在自由搜索）：不强行改关键词打断当前视图，
    // 只交给弹窗提示「已入库」，侧边栏点「我的上传」即可看到。
    toastInfo('已入库，可在侧边栏「我的上传」查看')
  }
}

const handleLogoClick = () => {
  if (route.path !== '/') router.push('/')
  handleResetToDefaults()
}

// ---- F1 薄分发层：判别联合事件路由到既有 handler（handler 本体原样保留，
// payload 与原单事件一致；switch 按判别字段收窄）----
const handleNovelsCommand = (command: NovelsCommand) => {
  switch (command.type) {
    case 'search':
      handleSearch(command.keyword, { setOrdering: true })
      break
    case 'card-search':
      handleCardSearch(command.payload.type, command.payload.value)
      break
    case 'load-more':
      handleLoadMore()
      break
    case 'update-filters':
      Object.assign(filters, command.payload)
      handleSearch()
      break
    case 'collection-view':
      inCollectionView.value = command.payload
      break
    case 'upload':
      // 列表顶部 UploadBar → 上传弹窗（唯一入口；侧边栏不挂上传按钮）
      isUploadModalOpen.value = true
      break
  }
}

const handleBatchAction = (action: BatchAction) => {
  switch (action.type) {
    case 'toggle-card':
      toggleBatchCard(action.id)
      break
    case 'select-all':
      void handleBatchSelectAll()
      break
    case 'clear':
      clearBatchSelection()
      break
    case 'clear-scope':
      void handleBatchClearScope()
      break
    case 'operation-success':
      handleBatchOperationSuccess(action.payload)
      break
    case 'task-submitted':
      handleBatchTaskSubmitted(action.payload)
      break
  }
}

onMounted(() => {
  if (route.path !== '/') {
    activeSection.value = null
  }
  startTaskStatusPolling()
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
        :novels-state="novelsState"
        :batch-state="batchState"
        :active-section="activeSection"
        @novels-command="handleNovelsCommand"
        @batch-action="handleBatchAction"
        @logo-click="handleLogoClick"
        @toggle-sidebar="isSidebarOpen = !isSidebarOpen"
        @novel-state-changed="handleNovelStateChanged"
        @novel-updated="handleNovelUpdated"
      />
    </div>

    <UploadModal
      :is-open="isUploadModalOpen"
      @close="isUploadModalOpen = false"
      @uploaded="handleUploaded"
      @error="toastError"
    />

    <ToastContainer :toasts="toasts" />
  </div>
</template>
