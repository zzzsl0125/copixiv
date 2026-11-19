<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import NovelCard, { type NovelStateChange } from '../components/features/NovelCard.vue'
import NovelHeader from '../components/features/NovelHeader.vue'
import LoadMore from '../components/features/LoadMore.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import CardSkeleton from '../components/ui/CardSkeleton.vue'
import ErrorAlert from '../components/ui/ErrorAlert.vue'
import BatchBar from '../components/features/BatchBar.vue'
import UploadBar from '../components/features/UploadBar.vue'
import ExclusionBar from '../components/features/ExclusionBar.vue'
import NovelModalsHost from '../components/features/NovelModalsHost.vue'
import {
  useBatchMode,
  useCollectionViews,
  useNovels,
  useNovelQueryState,
  useNovelShellState,
  useToast,
  useVirtualMasonry,
} from '../composables'
import { getApiErrorMessage, novelApi, queryKeys, tagPreferenceApi } from '../api'
import { downloadUrl } from '../lib/utils'
import type {
  BatchMetadataResult,
  BatchOperationResult,
  Novel,
  NovelFilters,
  TagPreference,
} from '../types'

const emit = defineEmits<{
  (e: 'toggle-sidebar'): void
}>()

const toast = useToast()
const queryClient = useQueryClient()
const shell = useNovelShellState()
const { updateFilters, navigateToSection } = useNovelQueryState()

const {
  novels,
  loading,
  error,
  noMoreData,
  filters,
  activeSection,
  hasExcluded,
  handleSearch,
  handleLoadMore: loadMoreNovels,
  handleCardSearch,
  resetFilters,
  invalidateNovels,
  patchNovelInCache,
  patchNovelStateInCache,
  removeNovelFromCache,
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
  toggleCard: toggleBatchCard,
  removeCard: removeBatchCard,
  isCardSelected,
  selectAllMatched,
  clearSelection: clearBatchSelection,
  clearSelectionInScope,
} = useBatchMode(filters)

const stickyHeaderRef = ref<HTMLElement | null>(null)
const masonryContainerRef = ref<HTMLElement | null>(null)
const isUploadModalOpen = ref(false)
const activeCardId = ref<number | string | null>(null)

const tagPrefsQuery = useQuery({
  queryKey: queryKeys.tagPreferences(),
  queryFn: ({ signal }) => tagPreferenceApi.getTagPreferences(signal),
  staleTime: 60_000,
})
watch(
  () => tagPrefsQuery.error.value,
  (err) => {
    if (err) toast.error(getApiErrorMessage(err, '标签偏好加载失败'))
  },
)

const tagPreferenceMap = computed<ReadonlyMap<string, TagPreference['preference']>>(() => {
  const map = new Map<string, TagPreference['preference']>()
  for (const p of tagPrefsQuery.data.value ?? []) {
    if (!map.has(p.tag)) map.set(p.tag, p.preference)
  }
  return map
})

// ---- Modal visibility states (hosted in NovelModalsHost) ----
const deleteOpen = ref(false)
const tagModalOpen = ref(false)
const tagOperation = ref<'add_tags' | 'remove_tags'>('add_tags')
const exportOpen = ref(false)
const batchEditOpen = ref(false)
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
  filters,
  batchScope,
  batchMode,
  hasSelection,
  novels,
  onActiveChange: (active) => {
    shell.isInCollectionView.value = active
  },
  afterLayout: () => measure(),
})

const {
  columnCount,
  virtualItems,
  totalHeight,
  measureElement,
  itemStyle,
  measure,
} = useVirtualMasonry<Novel>(displayNovels, {
  containerRef: masonryContainerRef,
  headerRef: stickyHeaderRef,
  getItemKey: (novel) => novel.id,
})

const handleLoadMore = () => {
  void loadMoreNovels()
}

watch(
  () => filters.value,
  () => {
    window.scrollTo({ top: 0 })
  },
)

const handleUpdateFilters = (patch: Partial<NovelFilters>) => {
  void updateFilters(patch)
}

const handleLogoClick = () => {
  void resetFilters()
}

const handleUploaded = () => {
  void invalidateNovels()
  if (activeSection.value === 'uploads') {
    void navigateToSection('uploads')
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
    toast.info(`已清空全部选择（${result.removed} 篇）`)
  } else {
    toast.info(
      `已清除当前范围勾选的 ${result.removed} 篇，` +
        `另有 ${result.remaining} 篇来自其他搜索仍保留在已选中`,
    )
  }
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
    void nextTick().then(measure)
  }
  toggleBatchCard(id)
}

const handleBatchOperationSuccess = async (payload: {
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
  await invalidateNovels()
  void queryClient.invalidateQueries({ queryKey: ['novels', 'count'] })
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
const handleToggleActive = (id: number | string) => {
  activeCardId.value = activeCardId.value === id ? null : id
}

const applyNovelStateChange = (payload: NovelStateChange, authorId?: number) => {
  patchNovelStateInCache(payload, authorId)
  patchNovelState(payload, authorId)
}

const handleDownloadNovel = (novel: Novel) => {
  downloadUrl(novelApi.downloadUrl(novel.id, novel.has_epub === 2 ? 'epub' : 'txt'))
}

const handleToggleFavourite = async (novel: Novel) => {
  try {
    await novelApi.toggleFavourite(novel.id)
    applyNovelStateChange({
      id: novel.id,
      field: 'is_favourite',
      value: novel.is_favourite ? 0 : 1,
    })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '收藏操作失败'))
  }
}

const handleToggleFollow = async (novel: Novel) => {
  if (!novel.author_id) return
  try {
    await novelApi.toggleSpecialFollow(novel.author_id)
    applyNovelStateChange(
      {
        id: novel.id,
        field: 'is_special_follow',
        value: novel.is_special_follow ? 0 : 1,
      },
      novel.author_id,
    )
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '追更操作失败'))
  }
}

const handleNovelUpdated = (updated: Novel) => {
  if (editingNovel.value?.id === updated.id) editingNovel.value = updated
  patchNovelInCache(updated)
  patchNovel(updated)
  void nextTick().then(measure)
}

const handleNovelDeleted = (novelId: number) => {
  removeNovelFromCache(novelId)
  removeNovel(novelId)
  removeBatchCard(novelId)
  if (activeCardId.value === novelId) activeCardId.value = null
  if (editingNovel.value?.id === novelId) editingNovel.value = null
  if (viewingSelected.value && selectedView.novels.value.length === 0 && selectedView.hasMore.value) {
    void selectedView.loadMore()
  }
  void nextTick().then(measure)
  toast.success('已删除小说')
}
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full" @click="activeCardId = null">
    <div ref="stickyHeaderRef" class="sticky top-0 z-20">
      <NovelHeader
        :filters="filters"
        @search="(keyword?: string) => void handleSearch(keyword, { setOrdering: true })"
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
      <ErrorAlert :message="error" />

      <div
        v-if="viewingSelected"
        class="mb-4 text-xs text-blue-700 bg-blue-50 border border-blue-200 rounded-md px-3 py-2"
      >
        <template v-if="viewLoading">正在加载已选小说…</template>
        <template v-else>
          正在查看已选的小说（已展示 {{ viewShownCount }} / 共 {{ viewTotalCount }} 篇）。
          点击卡片即可取消勾选并立即从该列表移除；点「返回搜索列表」回到筛选列表。
        </template>
      </div>

      <div
        v-if="viewingExcluded"
        class="mb-4 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-md px-3 py-2"
      >
        <template v-if="viewLoading">正在加载被排除的小说…</template>
        <template v-else>
          正在查看本次搜索中被排除的小说（已展示 {{ viewShownCount }} / 共 {{ viewTotalCount }} 篇）。
          点「返回浏览列表」回到筛选列表。
        </template>
      </div>

      <div
        v-if="displayNovels.length > 0"
        ref="masonryContainerRef"
        class="relative w-full [overflow-anchor:none]"
        :style="{ height: `${totalHeight}px` }"
        data-testid="masonry-grid"
      >
        <div
          v-for="vItem in virtualItems"
          :key="String(vItem.key)"
          :data-index="vItem.index"
          :ref="measureElement"
          :style="itemStyle(vItem)"
        >
          <NovelCard
            v-if="displayNovels[vItem.index]"
            :novel="displayNovels[vItem.index]!"
            :is-active="activeCardId === displayNovels[vItem.index]!.id"
            :tag-preference-map="tagPreferenceMap"
            :batch-mode="batchMode"
            :batch-selected="isCardSelected(displayNovels[vItem.index]!.id)"
            @toggle-active="handleToggleActive"
            @toggle-batch-select="handleBatchToggleCard"
            @search="(type: string, value: string | number) => void handleCardSearch(type, value)"
            @toggle-favourite="(n) => void handleToggleFavourite(n)"
            @toggle-follow="(n) => void handleToggleFollow(n)"
            @download="handleDownloadNovel"
            @edit="editingNovel = $event"
          />
        </div>
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

    <NovelModalsHost
      :delete-open="deleteOpen"
      :tag-modal-open="tagModalOpen"
      :tag-operation="tagOperation"
      :batch-edit-open="batchEditOpen"
      :export-open="exportOpen"
      :editing-novel="editingNovel"
      :upload-open="isUploadModalOpen"
      :batch-scope="batchScope"
      :scope-label="scopeLabel"
      :filters="filters"
      @close-delete="deleteOpen = false"
      @close-tag="tagModalOpen = false"
      @close-batch-edit="batchEditOpen = false"
      @close-export="exportOpen = false"
      @close-edit="editingNovel = null"
      @close-upload="isUploadModalOpen = false"
      @batch-success="(payload) => void handleBatchOperationSuccess(payload)"
      @batch-task-submitted="handleBatchTaskSubmitted"
      @download-success="toast.success(`已开始下载：${$event}`)"
      @novel-updated="handleNovelUpdated"
      @novel-deleted="handleNovelDeleted"
      @uploaded="handleUploaded"
      @error="toast.error"
    />
  </div>
</template>
