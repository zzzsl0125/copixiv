<script setup lang="ts">
import { computed, ref, toRef, nextTick, watch, onMounted, onUnmounted } from 'vue'
import NovelCard, { type NovelStateChange } from '../components/features/NovelCard.vue'
import NovelHeader from '../components/features/NovelHeader.vue'
import LoadMore from '../components/features/LoadMore.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import CardSkeleton from '../components/ui/CardSkeleton.vue'
import BatchBar from '../components/features/BatchBar.vue'
import UploadBar from '../components/features/UploadBar.vue'
import UploadModal from '../components/features/UploadModal.vue'
import BatchDeleteModal from '../components/features/BatchDeleteModal.vue'
import BatchTagModal from '../components/features/BatchTagModal.vue'
import BatchEditModal from '../components/features/BatchEditModal.vue'
import BatchDownloadModal from '../components/features/BatchDownloadModal.vue'
import EditNovelModal from '../components/features/EditNovelModal.vue'
import ExclusionBar from '../components/features/ExclusionBar.vue'
import {
  useNovels,
  useSystem,
  useBatchMode,
  useMasonryLayout,
  useCollectionViews,
  useToast,
  useLoadMoreAnchor,
  useNovelSidebarBridge,
} from '../composables'
import { tagPreferenceApi, getApiErrorMessage } from '../api'
import type {
  Novel,
  NovelFilters,
  TagPreference,
  BatchOperationResult,
  BatchMetadataResult,
} from '../types'
import { SECTIONS, resolveSection, sectionFor, type SectionId } from '../config'

const emit = defineEmits<{
  (e: 'toggle-sidebar'): void
}>()

const toast = useToast()
const { systemConfig, fetchConfig } = useSystem()
const bridge = useNovelSidebarBridge()

const {
  novels,
  loading,
  error,
  noMoreData,
  filters,
  hasExcluded,
  lastFetchedKeyword,
  loadNovels,
  handleSearch,
  handleLoadMore: loadMoreNovels,
  handleCardSearch,
} = useNovels()

const {
  isBatchMode: batchMode,
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
  removeCard: removeBatchCard,
  isCardSelected,
  selectAllMatched,
  clearSelection: clearBatchSelection,
  clearSelectionInScope,
} = useBatchMode(filters)

const activeSection = ref<SectionId | null>('novels')
const configLoadedAndApplied = ref(false)
const isUploadModalOpen = ref(false)
const activeCardId = ref<number | string | null>(null)
const tagPreferences = ref<TagPreference[]>([])

// ---- batch operation modal states ----
const deleteOpen = ref(false)
const tagModalOpen = ref(false)
const tagOperation = ref<'add_tags' | 'remove_tags'>('add_tags')
const exportOpen = ref(false)
const batchEditOpen = ref(false)

// ---- 元数据编辑弹窗宿主（初值来自卡片完整对象，保存/同步/删除后原地更新）----
const editingNovel = ref<Novel | null>(null)

// ---- 「查看已选」/「查看被排除」集合视图编排 ----
const {
  viewingSelected,
  viewingExcluded,
  selectedView,
  displayNovels,
  viewLoading,
  viewLoadingMore,
  viewHasMore,
  viewShownCount,
  viewTotalCount,
  toggleViewSelected,
  toggleViewExcluded,
  handleViewLoadMore,
  patchNovel,
  patchNovelState,
  removeNovel,
} = useCollectionViews({
  filters: toRef(() => filters),
  batchScope,
  batchMode,
  hasSelection,
  novels,
  onActiveChange: (active) => {
    bridge.isInCollectionView.value = active
  },
  afterLayout: () => relayout(),
})

const {
  containerRef: masonryContainerRef,
  columnCount,
  totalHeight,
  layoutRevision,
  cardStyle,
  setItemRef,
  relayout,
} = useMasonryLayout<Novel>(toRef(displayNovels))

const { run: runLoadMore } = useLoadMoreAnchor({
  container: masonryContainerRef,
  firstItem: () => displayNovels.value[0]?.id,
  layoutRevision,
})
const handleLoadMore = () => runLoadMore(() => loadMoreNovels())

// ---- Bridge sync: keep Sidebar reflective of current home state ----
watch(
  [
    () => filters.keyword,
    () => filters.order_by,
    () => filters.order_direction,
    () => filters.min_like,
    () => filters.min_text,
  ],
  () => bridge.syncFilters(filters),
  { immediate: true },
)
watch(activeSection, (val) => {
  bridge.activeSection.value = val
}, { immediate: true })
watch(configLoadedAndApplied, (val) => {
  bridge.configLoadedAndApplied.value = val
}, { immediate: true })
watch(batchMode, (val) => {
  bridge.isBatchMode.value = val
}, { immediate: true })

// ---- Section & filter orchestration ----
const syncActiveSection = () => {
  activeSection.value = resolveSection(filters.keyword)
}

const applySectionOrdering = (id: SectionId) => {
  const section = SECTIONS[id]
  filters.order_by = section.order_by
  filters.order_direction = section.order_direction
}

const applySection = (id: SectionId, rawKeyword?: string) => {
  const section = SECTIONS[id]
  filters.keyword = rawKeyword ?? section.keyword
  activeSection.value = id
  filters.min_like = section.min_like
  filters.min_text = section.min_text
  applySectionOrdering(id)
  configLoadedAndApplied.value = true
  handleSearch(undefined, { setOrdering: false })
}

const applyConfigAndLoad = () => {
  if (configLoadedAndApplied.value) return

  const initialSection = resolveSection(filters.keyword)
  const config = systemConfig.value
  if (config) {
    const urlParams = new URLSearchParams(window.location.search)
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

// 全局「排除厌恶标签」开关在标签管理页变更后即时生效
watch(
  () => systemConfig.value?.exclude_blocked_tag_novels,
  (val, old) => {
    if (val !== undefined && old !== undefined && configLoadedAndApplied.value) {
      handleSearch(undefined, { updateUrl: false })
    }
  },
)

// Popstate / 搜索历史改写 filters.keyword 时反推 section
watch(() => filters.keyword, (keyword) => {
  if (!configLoadedAndApplied.value) return
  const section = resolveSection(keyword)

  if (lastFetchedKeyword.value === keyword) {
    lastFetchedKeyword.value = null
    if (section && section !== 'novels') {
      const def = SECTIONS[section]
      activeSection.value = section
      filters.min_like = def.min_like
      filters.min_text = def.min_text
      applySectionOrdering(section)
    } else {
      activeSection.value = section
    }
    return
  }

  if (section && section !== 'novels') {
    applySection(section)
  } else {
    activeSection.value = section
  }
})

const handleSectionSearch = (_keyword: string | undefined, section: SectionId) => {
  applySection(section)
}

const handleUpdateFilters = (patch: Partial<NovelFilters>) => {
  Object.assign(filters, patch)
  handleSearch()
}

const handleResetToDefaults = () => {
  applySection('novels')
}

const handleLogoClick = () => {
  handleResetToDefaults()
}

const handleToggleBatchMode = () => {
  if (batchMode.value) exitBatchMode()
  else enterBatchMode()
}

const handleUploaded = () => {
  if (activeSection.value === 'uploads') {
    applySection('uploads')
  } else {
    toast.info('已入库，可在侧边栏「我的上传」查看')
  }
}

// ---- Batch bar & modal handlers ----
const scopeLabel = computed(() => `已勾选的 ${selectedCount.value} 篇`)

const handleBatchSelectAll = async () => {
  const result = await selectAllMatched()
  if (!result) return
  if (result.truncated) {
    toast.warning(
      `已全选匹配中的前 ${result.added} 篇（共匹配 ${result.total} 篇，` +
      `超过单次操作上限）。可继续搜索细分后再次全选，已选不会丢失。`,
    )
  } else {
    toast.success(`已将 ${result.added} 篇匹配小说加入选择（当前已选 ${selectedCount.value} 篇）`)
  }
}

const handleBatchClearScope = async () => {
  const result = await clearSelectionInScope()
  if (!result) return
  if (result.remaining === 0) {
    toastInfoOrClear(result.removed)
  } else {
    toast.info(
      `已清除当前范围勾选的 ${result.removed} 篇，` +
      `另有 ${result.remaining} 篇来自其他搜索仍保留在已选中`,
    )
  }
}

function toastInfoOrClear(removed: number) {
  toast.info(`已清空全部选择（${removed} 篇）`)
}

function handleBarClear() {
  if (viewingSelected.value) {
    clearBatchSelection()
  } else {
    void handleBatchClearScope()
  }
}

function handleBatchToggleCard(id: number) {
  if (viewingSelected.value && isCardSelected(id)) {
    selectedView.removeId(id)
    if (selectedView.novels.value.length === 0 && selectedView.hasMore.value) {
      void selectedView.loadMore()
    }
    void nextTick().then(relayout)
  }
  toggleBatchCard(id)
}

const handleBatchOperationSuccess = (payload: {
  operation: string
  result: BatchOperationResult | BatchMetadataResult
}) => {
  const { operation, result } = payload
  if (operation === 'delete') {
    toast.success(`已删除 ${result.affected} 篇小说`)
  } else if (operation === 'add_tags') {
    toast.success(`已为 ${result.affected} 篇小说添加标签`)
  } else if (operation === 'remove_tags') {
    toast.success(`已从 ${result.affected} 篇小说移除标签`)
  } else if (operation === 'update_metadata') {
    const skipped = 'skipped' in result ? result.skipped : 0
    toast.success(
      `已修改 ${result.affected} 篇的作者/系列` +
      (skipped > 0 ? `（跳过 ${skipped} 篇 pixiv 书）` : ''),
    )
  }
  clearBatchSelection()
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
  toast.info(
    `${labels[payload.operation] ?? '批量操作'}已提交为后台任务 #${payload.task_id}` +
    `（共 ${payload.matched} 篇）。可关闭页面，到「任务管理」查看进度` +
    (isExport ? '，完成后在任务队列中点击下载。' : '。'),
  )
  clearBatchSelection()
}

// ---- Card interaction handlers ----
const fetchTagPreferences = async () => {
  try {
    tagPreferences.value = await tagPreferenceApi.getTagPreferences()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '标签偏好加载失败'))
  }
}

const handleToggleActive = (id: number | string) => {
  activeCardId.value = activeCardId.value === id ? null : id
}

const handleNovelStateChanged = (payload: NovelStateChange) => {
  const sourceNovel =
    novels.value.find(n => n.id === payload.id) ??
    displayNovels.value.find(n => n.id === payload.id)
  const authorId = sourceNovel?.author_id ?? undefined

  for (const novel of novels.value) {
    if (payload.field === 'is_special_follow' && authorId !== undefined) {
      if (novel.author_id === authorId) {
        novel.is_special_follow = payload.value
      }
    } else if (novel.id === payload.id) {
      novel[payload.field] = payload.value
    }
  }
  patchNovelState(payload, authorId)
}

const handleNovelUpdated = (updated: Novel) => {
  if (editingNovel.value?.id === updated.id) editingNovel.value = updated
  const target = novels.value.find(n => n.id === updated.id)
  if (target) Object.assign(target, updated)
  patchNovel(updated)
  void nextTick().then(relayout)
}

const handleNovelDeleted = (novelId: number) => {
  const idx = novels.value.findIndex(n => n.id === novelId)
  if (idx >= 0) novels.value.splice(idx, 1)
  removeNovel(novelId)
  removeBatchCard(novelId)
  if (activeCardId.value === novelId) activeCardId.value = null
  if (editingNovel.value?.id === novelId) editingNovel.value = null
  if (viewingSelected.value && selectedView.novels.value.length === 0 && selectedView.hasMore.value) {
    void selectedView.loadMore()
  }
  void nextTick().then(relayout)
  toast.success('已删除小说')
}

onMounted(() => {
  void fetchTagPreferences()
  bridge.register({
    sectionSearch: handleSectionSearch,
    updateFilters: handleUpdateFilters,
    resetToDefaults: handleResetToDefaults,
    toggleBatchMode: handleToggleBatchMode,
  })

  const pendingSection = bridge.consumePendingSection()
  const wantBatch = bridge.consumePendingBatchMode()
  void fetchConfig().then(() => {
    if (pendingSection) {
      applySection(pendingSection.section, pendingSection.keyword)
    } else if (!configLoadedAndApplied.value) {
      applyConfigAndLoad()
    }
    if (wantBatch && !batchMode.value) enterBatchMode()
  })
})

onUnmounted(() => {
  bridge.unregister()
})
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full" @click="activeCardId = null">
    <div class="sticky top-0 z-20">
      <NovelHeader
        :filters="filters"
        @search="(keyword?: string) => handleSearch(keyword, { setOrdering: true })"
        @update:filters="handleUpdateFilters"
        @toggle-sidebar="emit('toggle-sidebar')"
        @logo-click="handleLogoClick"
      />
      <ExclusionBar
        v-if="filters.keyword.trim() !== '' && hasExcluded"
        :is-viewing-excluded="viewingExcluded"
        :interactive="!batchMode"
        @toggle-view-excluded="toggleViewExcluded"
      />
      <BatchBar
        v-if="batchMode"
        :matched-count="matchedCount"
        :matched-truncated="matchedTruncated"
        :count-loading="countLoading"
        :select-all-loading="selectAllLoading"
        :selected-count="selectedCount"
        :has-selection="hasSelection"
        :has-filter="hasFilter"
        :is-viewing-selected="viewingSelected"
        @select-all="handleBatchSelectAll"
        @clear="handleBarClear"
        @toggle-view-selected="toggleViewSelected"
        @edit="batchEditOpen = true"
        @export="exportOpen = true"
        @add-tags="tagOperation = 'add_tags'; tagModalOpen = true"
        @remove-tags="tagOperation = 'remove_tags'; tagModalOpen = true"
        @delete="deleteOpen = true"
      />
      <UploadBar
        v-if="activeSection === 'uploads' && !batchMode"
        @upload="isUploadModalOpen = true"
      />
    </div>

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-grow">
      <div v-if="error" class="bg-red-50 border-l-4 border-red-400 p-4 mb-6 rounded-md">
        <div class="flex">
          <div class="flex-shrink-0">
            <svg class="h-5 w-5 text-red-400" viewBox="0 0 20 20" fill="currentColor">
              <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
            </svg>
          </div>
          <div class="ml-3">
            <p class="text-sm text-red-700">{{ error }}</p>
          </div>
        </div>
      </div>

      <!-- 查看已选时的视图提示 -->
      <div v-if="viewingSelected" class="mb-4 text-xs text-blue-700 bg-blue-50 border border-blue-200 rounded-md px-3 py-2">
        <template v-if="viewLoading">正在加载已选小说…</template>
        <template v-else>
          正在查看已选的小说（已展示 {{ viewShownCount }} / 共 {{ viewTotalCount }} 篇）。
          点击卡片即可取消勾选并立即从该列表移除；点「返回搜索列表」回到筛选列表。
        </template>
      </div>

      <!-- 查看被排除时的视图提示 -->
      <div v-if="viewingExcluded" class="mb-4 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-md px-3 py-2">
        <template v-if="viewLoading">正在加载被排除的小说…</template>
        <template v-else>
          正在查看本次搜索中被排除的小说（已展示 {{ viewShownCount }} / 共 {{ viewTotalCount }} 篇）。
          点「返回浏览列表」回到筛选列表。
        </template>
      </div>

      <div
        v-if="displayNovels.length > 0"
        ref="masonryContainerRef"
        class="relative w-full"
        :style="{ height: totalHeight + 'px', visibility: totalHeight ? 'visible' : 'hidden' }"
        data-testid="masonry-grid"
      >
        <NovelCard
          v-for="(novel, index) in displayNovels"
          :key="novel.id"
          :style="cardStyle()"
          :data-anchor-id="String(novel.id)"
          :novel="novel"
          :is-active="activeCardId === novel.id"
          :tag-preferences="tagPreferences"
          :batch-mode="batchMode"
          :batch-selected="isCardSelected(novel.id)"
          :ref="el => setItemRef(index, el)"
          @toggle-active="handleToggleActive"
          @toggle-batch-select="handleBatchToggleCard"
          @search="handleCardSearch"
          @state-changed="handleNovelStateChanged"
          @edit="editingNovel = $event"
        />
      </div>

      <div
        v-else-if="loading || viewLoading"
        class="grid gap-6"
        :style="{ gridTemplateColumns: `repeat(${columnCount}, minmax(0, 1fr))` }"
        data-testid="masonry-skeleton"
      >
        <CardSkeleton v-for="n in columnCount * 2" :key="n" />
      </div>

      <EmptyState v-else-if="!loading && !viewLoading" :loading="loading" />

      <LoadMore
        v-if="!viewingSelected && !viewingExcluded"
        :loading="loading"
        :no-more-data="noMoreData"
        :has-data="novels.length > 0"
        @load-more="handleLoadMore"
      />
      <LoadMore
        v-else
        :loading="viewLoadingMore"
        :no-more-data="!viewHasMore"
        :has-data="viewShownCount > 0"
        @load-more="handleViewLoadMore"
      />
    </main>

    <BatchDeleteModal
      :is-open="deleteOpen"
      :scope="batchScope"
      :scope-label="scopeLabel"
      @close="deleteOpen = false"
      @success="(payload) => handleBatchOperationSuccess({ operation: 'delete', result: payload })"
      @task-submitted="(payload) => handleBatchTaskSubmitted({ operation: 'delete', ...payload })"
      @error="toast.error"
    />
    <BatchTagModal
      :is-open="tagModalOpen"
      :operation="tagOperation"
      :scope="batchScope"
      :scope-label="scopeLabel"
      @close="tagModalOpen = false"
      @success="(payload) => handleBatchOperationSuccess({ operation: tagOperation, result: payload })"
      @task-submitted="(payload) => handleBatchTaskSubmitted({ operation: tagOperation, ...payload })"
      @error="toast.error"
    />
    <BatchEditModal
      :is-open="batchEditOpen"
      :scope="batchScope"
      :scope-label="scopeLabel"
      @close="batchEditOpen = false"
      @success="(payload) => handleBatchOperationSuccess({ operation: 'update_metadata', result: payload })"
      @error="toast.error"
    />
    <BatchDownloadModal
      :is-open="exportOpen"
      :keyword="filters.keyword"
      :order_by="filters.order_by"
      :order_direction="filters.order_direction"
      :min_like="filters.min_like"
      :min_text="filters.min_text"
      :novel-ids="batchScope?.novel_ids"
      @close="exportOpen = false"
      @download-success="toast.success(`已开始下载：${$event}`)"
      @download-error="toast.error"
      @task-submitted="(payload) => handleBatchTaskSubmitted({ operation: 'export', ...payload })"
    />

    <EditNovelModal
      :is-open="editingNovel !== null"
      :novel="editingNovel"
      @close="editingNovel = null"
      @updated="handleNovelUpdated"
      @deleted="handleNovelDeleted"
      @error="toast.error"
    />

    <UploadModal
      :is-open="isUploadModalOpen"
      @close="isUploadModalOpen = false"
      @uploaded="handleUploaded"
      @error="toast.error"
    />
  </div>
</template>
