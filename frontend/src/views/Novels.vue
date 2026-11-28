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
import SourceShelfBar from '../components/features/SourceShelfBar.vue'
import ExclusionBar from '../components/features/ExclusionBar.vue'
import NovelModalsHost from '../components/features/NovelModalsHost.vue'
import {
  useBatchMode,
  useNovels,
  useToast,
  useVirtualMasonry,
} from '../composables'
import { useSources } from '../composables/useSources'
import { getApiErrorMessage, novelApi, queryKeys, tagPreferenceApi } from '../api'
import { downloadUrl } from '../lib/utils'
import type {
  BatchMetadataResult,
  BatchOperationResult,
  Novel,
  TagPreference,
} from '../types'

const emit = defineEmits<{
  (e: 'toggle-sidebar'): void
}>()

const toast = useToast()
const queryClient = useQueryClient()
const viewingExcluded = ref(false)

const {
  novels,
  loading,
  error,
  noMoreData,
  filters,
  hasExcluded,
  isSeriesDrilldown,
  handleSearch,
  handleLoadMore: loadMoreNovels,
  handleCardSearch,
  resetFilters,
  invalidateNovels,
  patchNovelInCache,
  patchNovelStateInCache,
  removeNovelFromCache,
} = useNovels({ onlyBlocked: viewingExcluded })

const { shelves } = useSources()
const activeSourceShelf = computed(() => {
  const keyword = filters.value.keyword.trim()
  if (!keyword) return null
  return shelves.value.find((entry) => entry.shelf.keyword.trim() === keyword) ?? null
})

const {
  isBatchMode: batchMode,
  matchedCount,
  matchedTruncated,
  countLoading,
  selectAllLoading,
  selectedCount,
  hasSelection,
  scope: batchScope,
  viewingSelected,
  selectedNovels,
  viewLoading,
  viewLoadingMore,
  viewHasMore,
  viewShownCount,
  viewTotalCount,
  toggleCard: toggleBatchCard,
  removeCard: removeBatchCard,
  isCardSelected,
  selectAllMatched,
  clearSelection: clearBatchSelection,
  clearSelectionInScope,
  toggleViewSelected,
  loadMoreSelected,
  patchSelectedNovel,
  patchSelectedNovelState,
} = useBatchMode(filters, {
  afterLayout: () => measure(),
})

const displayNovels = computed<Novel[]>(() =>
  viewingSelected.value ? selectedNovels.value : novels.value,
)

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

function toggleViewExcluded() {
  viewingExcluded.value = !viewingExcluded.value
  window.scrollTo({ top: 0 })
}

watch(batchMode, (on) => {
  if (on) viewingExcluded.value = false
})

watch(
  () => filters.value.keyword,
  (keyword) => {
    if (!keyword.trim()) viewingExcluded.value = false
  },
)

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

const handleLogoClick = () => {
  void resetFilters()
}

const handleUploaded = () => {
  void invalidateNovels()
}

// ---- Batch bar & modal handlers ----
const scopeLabel = computed(() => `已勾选的 ${selectedCount.value} 篇`)

const handleBatchSelectAll = async () => {
  const result = await selectAllMatched()
  if (!result) return
  toast.success(`已将 ${result.added} 篇匹配小说加入选择（当前已选 ${selectedCount.value} 篇）`)
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
  void queryClient.invalidateQueries({ queryKey: queryKeys.novelsAll() })
}

const handleExportTaskSubmitted = (payload: { task_id: number; matched: number }) => {
  toast.info(
    `批量导出已提交为后台任务 #${payload.task_id}（共 ${payload.matched} 篇）。可关闭页面，到「任务管理」查看进度，完成后在任务队列中点击下载。`,
  )
  clearBatchSelection()
}

// ---- Card interaction handlers ----
const handleToggleActive = (id: number | string) => {
  activeCardId.value = activeCardId.value === id ? null : id
}

const applyNovelStateChange = (payload: NovelStateChange, authorId?: number) => {
  patchNovelStateInCache(payload, authorId)
  patchSelectedNovelState(payload, authorId)
}

const handleDownloadNovel = (novel: Novel) => {
  const fmt = novel.has_epub === 2 ? 'epub' : 'txt'
  if (novel.is_series && novel.series_id != null) {
    downloadUrl(novelApi.seriesDownloadUrl(novel.series_id, fmt))
    return
  }
  downloadUrl(novelApi.downloadUrl(novel.id, fmt))
}

const handleToggleFavourite = async (novel: Novel) => {
  try {
    if (novel.is_series && novel.series_id != null) {
      await novelApi.toggleSeriesFavourite(novel.series_id)
      applyNovelStateChange({
        id: novel.id,
        field: 'is_favourite',
        value: !novel.is_favourite,
        seriesId: novel.series_id,
      })
      return
    }
    await novelApi.toggleFavourite(novel.id)
    applyNovelStateChange({
      id: novel.id,
      field: 'is_favourite',
      value: !novel.is_favourite,
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
        value: !novel.is_special_follow,
      },
      novel.author_id,
    )
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '追更操作失败'))
  }
}

const handleNovelUpdated = (updated: Novel) => {
  if (
    editingNovel.value &&
    (editingNovel.value.id === updated.id ||
      (editingNovel.value.is_series &&
        updated.is_series &&
        editingNovel.value.series_id != null &&
        editingNovel.value.series_id === updated.series_id))
  ) {
    editingNovel.value = updated
  }
  patchNovelInCache(updated)
  patchSelectedNovel(updated)
  void nextTick().then(measure)
}

const handleNovelDeleted = (novelId: number) => {
  const deletedSeriesId = editingNovel.value?.is_series ? editingNovel.value.series_id : null
  removeNovelFromCache(novelId, deletedSeriesId)
  removeBatchCard(novelId)
  if (activeCardId.value === novelId) activeCardId.value = null
  if (editingNovel.value?.id === novelId) editingNovel.value = null
  void nextTick().then(measure)
  toast.success(deletedSeriesId != null ? '已删除系列' : '已删除小说')
}
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full" @click="activeCardId = null">
    <div ref="stickyHeaderRef" class="sticky top-0 z-20">
      <NovelHeader
        :filters="filters"
        @search="(keyword?: string) => void handleSearch(keyword)"
        @toggle-sidebar="emit('toggle-sidebar')"
        @logo-click="handleLogoClick"
      />
      <ExclusionBar
        v-if="filters.keyword.trim() !== '' && (hasExcluded || viewingExcluded)"
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
      <SourceShelfBar
        v-if="activeSourceShelf && activeSourceShelf.shelf.intake_action !== 'none' && !batchMode"
        :source="activeSourceShelf.source"
        :display-name="activeSourceShelf.displayName"
        :shelf="activeSourceShelf.shelf"
        @upload="isUploadModalOpen = true"
      />
    </div>

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-grow [overflow-anchor:none]">
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
        <template v-if="loading && novels.length === 0">正在加载被排除的小说…</template>
        <template v-else>
          正在查看本次搜索中被排除的小说（已加载 {{ novels.length }} 篇）。
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
            @toggle-batch-select="toggleBatchCard"
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

      <EmptyState v-else-if="!loading && !viewLoading" />

      <LoadMore
        v-if="!viewingSelected"
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
        @load-more="loadMoreSelected"
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
      :expand-series="!isSeriesDrilldown"
      :merge-series="!isSeriesDrilldown"
      @close-delete="deleteOpen = false"
      @close-tag="tagModalOpen = false"
      @close-batch-edit="batchEditOpen = false"
      @close-export="exportOpen = false"
      @close-edit="editingNovel = null"
      @close-upload="isUploadModalOpen = false"
      @batch-success="(payload) => void handleBatchOperationSuccess(payload)"
      @export-task-submitted="handleExportTaskSubmitted"
      @novel-updated="handleNovelUpdated"
      @novel-deleted="handleNovelDeleted"
      @uploaded="handleUploaded"
      @error="toast.error"
    />
  </div>
</template>
