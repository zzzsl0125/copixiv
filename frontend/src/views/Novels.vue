<script setup lang="ts">
import { computed, ref, toRef, watch, nextTick, onMounted } from 'vue'
import NovelCard, { type NovelStateChange } from '../components/features/NovelCard.vue'
import NovelHeader from '../components/features/NovelHeader.vue'
import LoadMore from '../components/features/LoadMore.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import BatchBar from '../components/features/BatchBar.vue'
import BatchDeleteModal from '../components/features/BatchDeleteModal.vue'
import BatchTagModal from '../components/features/BatchTagModal.vue'
import BatchDownloadModal from '../components/features/BatchDownloadModal.vue'
import { useMasonryLayout } from '../composables/useMasonryLayout'
import { tagPreferenceApi, novelApi } from '../api'
import { getApiErrorMessage } from '../api/errors'
import { useToast } from '../composables'
import type { Novel, NovelFilters, TagPreference, BatchScope, BatchOperationResult } from '../types'

const props = defineProps<{
  isSidebarOpen: boolean
  filters: NovelFilters
  novels: Novel[]
  loading: boolean
  error: string | null
  noMoreData: boolean
  // ---- batch mode (state owned by App.vue) ----
  batchMode: boolean
  matchedCount: number
  countLoading: boolean
  selectAllLoading: boolean
  selectedCount: number
  hasSelection: boolean
  hasFilter: boolean
  batchScope: BatchScope | null
  isBatchSelected: (id: number) => boolean
}>()

const emit = defineEmits<{
  (e: 'toggle-sidebar'): void
  (e: 'search', keyword?: string): void
  (e: 'card-search', type: string, value: string | number): void
  (e: 'update:filters', filters: Partial<NovelFilters>): void
  (e: 'load-more'): void
  (e: 'logo-click'): void
  (e: 'novel-state-changed', payload: NovelStateChange): void
  // ---- batch mode events ----
  (e: 'batch-toggle-card', id: number): void
  (e: 'batch-select-all'): void
  (e: 'batch-clear'): void
  (e: 'batch-clear-scope'): void
  (e: 'batch-operation-success', payload: { operation: string; result: BatchOperationResult }): void
  (e: 'batch-task-submitted', payload: { operation: string; task_id: number; matched: number }): void
}>()

const toast = useToast()
const columnRefs = ref<HTMLElement[]>([])
const activeCardId = ref<number | string | null>(null)
const tagPreferences = ref<TagPreference[]>([])

// ---- batch operation modal states ----
const deleteOpen = ref(false)
const tagModalOpen = ref(false)
const tagOperation = ref<'add_tags' | 'remove_tags'>('add_tags')
const exportOpen = ref(false)

// ---- 「查看已选」view: the grid swaps to the selected novels,
// fetched in pages so huge selections stay smooth (infinite scroll) ----
const viewingSelected = ref(false)
const selectedNovels = ref<Novel[]>([])
const viewLoading = ref(false)
const viewLoadingMore = ref(false)
const viewOffset = ref(0)
const VIEW_PAGE_SIZE = 5000

const scopeLabel = computed(() => `已勾选的 ${props.selectedCount} 篇`)

const viewTotalIds = computed(() => props.batchScope?.novel_ids.length ?? 0)
const viewHasMore = computed(() => viewOffset.value < viewTotalIds.value)

const displayNovels = computed<Novel[]>(() =>
  viewingSelected.value ? selectedNovels.value : props.novels,
)

/** Fetch the next page of selected novels; reset=true starts from page 0. */
async function fetchSelectedPage(reset: boolean) {
  const ids = props.batchScope?.novel_ids ?? []
  if (reset) viewOffset.value = 0
  const pageIds = ids.slice(viewOffset.value, viewOffset.value + VIEW_PAGE_SIZE)
  if (pageIds.length === 0) return
  const result = await novelApi.getNovelsByIds(pageIds)
  viewOffset.value += pageIds.length
  if (reset) selectedNovels.value = result.novels
  else selectedNovels.value.push(...result.novels)
}

function resetSelectedView() {
  viewingSelected.value = false
  selectedNovels.value = []
  viewOffset.value = 0
}

// 清除按钮：列表视图 → 仅清当前筛选范围；查看已选视图 → 清空全部。
function handleBarClear() {
  if (viewingSelected.value) {
    emit('batch-clear')
  } else {
    emit('batch-clear-scope')
  }
}

async function toggleViewSelected() {
  if (viewingSelected.value) {
    resetSelectedView()
    await nextTick()
    relayout()
    return
  }
  const ids = props.batchScope?.novel_ids ?? []
  if (ids.length === 0) return
  viewingSelected.value = true
  viewLoading.value = true
  try {
    await fetchSelectedPage(true)
    await nextTick()
    relayout()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '加载已选小说失败'))
    resetSelectedView()
    await nextTick()
    relayout()
  } finally {
    viewLoading.value = false
  }
}

async function loadMoreSelected() {
  if (!viewHasMore.value || viewLoadingMore.value) return
  viewLoadingMore.value = true
  try {
    await fetchSelectedPage(false)
    // In-place push → the masonry length watcher appends the new cards.
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '加载更多已选小说失败'))
  } finally {
    viewLoadingMore.value = false
  }
}

// Leaving batch mode (or an emptied selection) closes the view.
watch(() => props.batchMode, (on) => {
  if (!on) {
    resetSelectedView()
    void nextTick().then(relayout)
  }
})

watch(() => props.hasSelection, (has) => {
  if (!has && viewingSelected.value) {
    resetSelectedView()
    void nextTick().then(relayout)
  }
})

const { columns, relayout } = useMasonryLayout<Novel>(
  toRef(displayNovels),
  columnRefs,
)

const fetchTagPreferences = async () => {
  try {
    tagPreferences.value = await tagPreferenceApi.getTagPreferences()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '标签偏好加载失败'))
  }
}

onMounted(fetchTagPreferences)

const handleToggleActive = (id: number | string) => {
  if (activeCardId.value === id) {
    activeCardId.value = null
  } else {
    activeCardId.value = id
  }
}
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full" @click="activeCardId = null">
    <div class="sticky top-0 z-20">
      <NovelHeader
        :filters="props.filters"
        @search="(keyword?: string) => emit('search', keyword)"
        @update:filters="emit('update:filters', $event)"
        @toggle-sidebar="emit('toggle-sidebar')"
        @logo-click="emit('logo-click')"
      />
      <BatchBar
        v-if="props.batchMode"
        :matched-count="props.matchedCount"
        :count-loading="props.countLoading"
        :select-all-loading="props.selectAllLoading"
        :selected-count="props.selectedCount"
        :has-selection="props.hasSelection"
        :has-filter="props.hasFilter"
        :is-viewing-selected="viewingSelected"
        @select-all="emit('batch-select-all')"
        @clear="handleBarClear"
        @toggle-view-selected="toggleViewSelected"
        @export="exportOpen = true"
        @add-tags="tagOperation = 'add_tags'; tagModalOpen = true"
        @remove-tags="tagOperation = 'remove_tags'; tagModalOpen = true"
        @delete="deleteOpen = true"
      />
    </div>

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-grow">
      <div v-if="props.error" class="bg-red-50 border-l-4 border-red-400 p-4 mb-6 rounded-md">
        <div class="flex">
          <div class="flex-shrink-0">
            <svg class="h-5 w-5 text-red-400" viewBox="0 0 20 20" fill="currentColor">
              <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
            </svg>
          </div>
          <div class="ml-3">
            <p class="text-sm text-red-700">{{ props.error }}</p>
          </div>
        </div>
      </div>

      <!-- 查看已选时的视图提示 -->
      <div v-if="viewingSelected" class="mb-4 text-xs text-blue-700 bg-blue-50 border border-blue-200 rounded-md px-3 py-2">
        <template v-if="viewLoading">正在加载已选小说…</template>
        <template v-else>
          正在查看已选的小说（已展示 {{ selectedNovels.length }} / 共 {{ viewTotalIds }} 篇）。
          取消勾选只会去掉蓝框，退出视图后生效；点「返回搜索列表」回到筛选列表。
        </template>
      </div>

      <div v-if="displayNovels.length > 0" class="flex gap-6 items-start">
        <div v-for="(col, colIndex) in columns" :key="colIndex" :ref="el => { if (el) columnRefs[colIndex] = el as HTMLElement }" class="flex-1 flex flex-col gap-6 w-full min-w-0">
          <NovelCard
            v-for="novel in col"
            :key="novel.id"
            :novel="novel"
            :is-active="activeCardId === novel.id"
            :tag-preferences="tagPreferences"
            :batch-mode="props.batchMode"
            :batch-selected="props.isBatchSelected(novel.id)"
            @toggle-active="handleToggleActive"
            @toggle-batch-select="(id: number) => emit('batch-toggle-card', id)"
            @search="(type, value) => emit('card-search', type, value)"
            @state-changed="emit('novel-state-changed', $event)"
          />
        </div>
      </div>

      <EmptyState v-else-if="!props.loading && !viewLoading" :loading="props.loading" />

      <LoadMore
        v-if="!viewingSelected"
        :loading="props.loading"
        :no-more-data="props.noMoreData"
        :has-data="props.novels.length > 0"
        @load-more="emit('load-more')"
      />
      <LoadMore
        v-else
        :loading="viewLoadingMore"
        :no-more-data="!viewHasMore"
        :has-data="selectedNovels.length > 0"
        @load-more="loadMoreSelected"
      />
    </main>

    <!-- 批量操作模态框（勾选完成后借用模态框进行后续操作） -->
    <BatchDeleteModal
      :is-open="deleteOpen"
      :scope="props.batchScope"
      :scope-label="scopeLabel"
      @close="deleteOpen = false"
      @success="(payload) => emit('batch-operation-success', { operation: 'delete', result: payload })"
      @task-submitted="(payload) => emit('batch-task-submitted', { operation: 'delete', ...payload })"
      @error="toast.error"
    />
    <BatchTagModal
      :is-open="tagModalOpen"
      :operation="tagOperation"
      :scope="props.batchScope"
      :scope-label="scopeLabel"
      @close="tagModalOpen = false"
      @success="(payload) => emit('batch-operation-success', { operation: tagOperation, result: payload })"
      @task-submitted="(payload) => emit('batch-task-submitted', { operation: tagOperation, ...payload })"
      @error="toast.error"
    />
    <BatchDownloadModal
      :is-open="exportOpen"
      :keyword="props.filters.keyword"
      :order_by="props.filters.order_by"
      :order_direction="props.filters.order_direction"
      :min_like="props.filters.min_like"
      :min_text="props.filters.min_text"
      :novel-ids="props.batchScope?.novel_ids"
      @close="exportOpen = false"
      @download-success="toast.success(`已开始下载：${$event}`)"
      @download-error="toast.error"
      @task-submitted="(payload) => emit('batch-task-submitted', { operation: 'export', ...payload })"
    />
  </div>
</template>
