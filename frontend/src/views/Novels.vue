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
import { useToast, usePagedNovelIdView } from '../composables'
import ExclusionBar from '../components/features/ExclusionBar.vue'
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
  (e: 'collection-view', active: boolean): void
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

// ---- 「查看已选」/「查看被排除」views: the grid swaps to an explicit
// id list, fetched in pages so huge lists stay smooth (infinite scroll).
// Shared paging lives in usePagedNovelIdView; the two views are mutually
// exclusive (批量模式优先) and exit on new searches. ----
const selectedView = usePagedNovelIdView()
const excludedView = usePagedNovelIdView()
const viewingSelected = ref(false)
const viewingExcluded = ref(false)

const scopeLabel = computed(() => `已勾选的 ${props.selectedCount} 篇`)

const displayNovels = computed<Novel[]>(() =>
  viewingSelected.value
    ? selectedView.novels.value
    : viewingExcluded.value
      ? excludedView.novels.value
      : props.novels,
)

// LoadMore / 提示框用的「当前激活视图」聚合状态
const viewLoading = computed(() =>
  viewingSelected.value ? selectedView.loading.value : excludedView.loading.value,
)
const viewLoadingMore = computed(() =>
  viewingSelected.value ? selectedView.loadingMore.value : excludedView.loadingMore.value,
)
const viewHasMore = computed(() =>
  viewingSelected.value ? selectedView.hasMore.value : excludedView.hasMore.value,
)
const viewShownCount = computed(() =>
  viewingSelected.value ? selectedView.novels.value.length : excludedView.novels.value.length,
)
const viewTotalCount = computed(() =>
  viewingSelected.value ? selectedView.totalIds.value : excludedView.totalIds.value,
)

function resetSelectedView() {
  viewingSelected.value = false
  selectedView.reset()
}

function resetExcludedView() {
  viewingExcluded.value = false
  excludedView.reset()
}

// 清除按钮：列表视图 → 仅清当前筛选范围；查看已选视图 → 清空全部。
function handleBarClear() {
  if (viewingSelected.value) {
    emit('batch-clear')
  } else {
    emit('batch-clear-scope')
  }
}

// ---- 视图的集合顺序：随机在集合视图内禁用，其余按当前排序条件取序 ----
function scopeOrderBy(): string | undefined {
  const o = props.filters.order_by
  if (o === 'random') return undefined  // 集合视图不支持随机 → 保持范围/勾选顺序
  if (o === 'id' || o === 'like' || o === 'text') return o
  return undefined
}

async function reloadSelectedIds() {
  const ids = props.batchScope?.novel_ids ?? []
  if (ids.length === 0) return
  const orderBy = props.filters.order_by
  if (orderBy === 'id') {
    // id 排序本地完成（id 列表本来就在内存里）
    const dir = props.filters.order_direction === 'ASC' ? 1 : -1
    await selectedView.start([...ids].sort((a, b) => dir * (a - b)))
    return
  }
  if (orderBy === 'like' || orderBy === 'text') {
    const result = await novelApi.sortNovelIds(
      ids, orderBy, props.filters.order_direction,
    )
    await selectedView.start(result.ids)
    return
  }
  await selectedView.start(ids)  // 随机/未知 → 勾选顺序
}

let excludedSeq = 0

/** 按当前筛选+排序重取被排除集合并从头展示（进入视图与原地重算共用）。 */
async function loadExcludedView() {
  const seq = ++excludedSeq
  try {
    const result = await novelApi.getBlockedNovelIds({
      keyword: props.filters.keyword.trim() || undefined,
      min_like: props.filters.min_like,
      min_text: props.filters.min_text,
      order_by: scopeOrderBy(),
      order_direction: props.filters.order_direction,
    })
    if (seq !== excludedSeq) return  // 已有更新的请求，丢弃本次结果
    if (result.ids.length === 0) {
      // 新范围没有被排除的小说 → 退回浏览列表
      resetExcludedView()
      await nextTick()
      relayout()
      return
    }
    await excludedView.start(result.ids)
    window.scrollTo({ top: 0 })  // 新集合从头看起，阻断自动翻页级联
    await nextTick()
    relayout()
  } catch (err: unknown) {
    if (seq !== excludedSeq) return
    toast.error(getApiErrorMessage(err, '加载被排除小说失败'))
    resetExcludedView()
    await nextTick()
    relayout()
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
  resetExcludedView()  // 互斥：进入已选视图时退出被排除视图
  viewingSelected.value = true
  try {
    await reloadSelectedIds()
    window.scrollTo({ top: 0 })  // 新视图从头看起，阻断自动翻页级联
    await nextTick()
    relayout()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '加载已选小说失败'))
    resetSelectedView()
    await nextTick()
    relayout()
  }
}

async function toggleViewExcluded() {
  if (viewingExcluded.value) {
    resetExcludedView()
    await nextTick()
    relayout()
    return
  }
  resetSelectedView()  // 互斥：进入被排除视图时退出已选视图
  viewingExcluded.value = true
  await loadExcludedView()
}

function handleViewLoadMore() {
  if (viewingSelected.value) void selectedView.loadMore()
  else if (viewingExcluded.value) void excludedView.loadMore()
}

// 「查看已选」视图是选择集的实时投影：在此视图内取消勾选必须让卡片
// 立即从列表消失（所见即所得），而不是留下一张无蓝框的卡片、直到退
// 出视图才真正移除。浏览列表视图不作此处理——那里仅由 App 维护选择
// 集，卡片本就不一定属于当前筛选范围。
function handleBatchToggleCard(id: number) {
  if (viewingSelected.value && props.isBatchSelected(id)) {
    selectedView.removeId(id)
    // 当前页被摘空但仍有未加载的已选时自动补一页，否则 LoadMore 会因
    // hasData=false 整体消失，把用户卡在空白网格里。
    if (selectedView.novels.value.length === 0 && selectedView.hasMore.value) {
      void selectedView.loadMore()
    }
    void nextTick().then(relayout)
  }
  emit('batch-toggle-card', id)
}

// Leaving batch mode (or an emptied selection) closes the selected view;
// entering batch mode closes the excluded view (批量模式优先互斥).
watch(() => props.batchMode, (on) => {
  if (on) {
    if (viewingExcluded.value) resetExcludedView()
  } else {
    resetSelectedView()
  }
  void nextTick().then(relayout)
})

watch(() => props.hasSelection, (has) => {
  if (!has && viewingSelected.value) {
    resetSelectedView()
    void nextTick().then(relayout)
  }
})

// 视图活跃状态上报给 App → 侧边栏在集合视图内禁用「随机」排序
// immediate：组件挂载/重挂时立即上报 false，避免离开页面后残留 true
watch([viewingSelected, viewingExcluded], ([s, e]) => {
  emit('collection-view', s || e)
}, { immediate: true })

// 筛选条件变化 → 被排除视图原地重算（集合始终与当前范围一致）；
// 关键词清空后该视图无展示意义（条已消失）→ 退回浏览列表。
watch(
  [
    () => props.filters.keyword,
    () => props.filters.min_like,
    () => props.filters.min_text,
  ],
  () => {
    if (!viewingExcluded.value) return
    if (!props.filters.keyword.trim()) {
      resetExcludedView()
      void nextTick().then(relayout)
      return
    }
    void loadExcludedView()
  },
)

// 排序条件变化 → 两个集合视图各自按新排序重排/重取
watch(
  [() => props.filters.order_by, () => props.filters.order_direction],
  ([o, d], [po, pd]) => {
    if (o === po && d === pd) return
    if (viewingSelected.value) {
      reloadSelectedIds()
        .then(async () => {
          window.scrollTo({ top: 0 })
          await nextTick()
          relayout()
        })
        .catch((err: unknown) => {
          toast.error(getApiErrorMessage(err, '重新排序失败'))
          resetSelectedView()
          void nextTick().then(relayout)
        })
    } else if (viewingExcluded.value) {
      void loadExcludedView()
    }
  },
)

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
      <ExclusionBar
        v-if="props.filters.keyword.trim() !== ''"
        :is-viewing-excluded="viewingExcluded"
        :interactive="!props.batchMode"
        @toggle-view-excluded="toggleViewExcluded"
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
            @toggle-batch-select="handleBatchToggleCard"
            @search="(type, value) => emit('card-search', type, value)"
            @state-changed="emit('novel-state-changed', $event)"
          />
        </div>
      </div>

      <EmptyState v-else-if="!props.loading && !viewLoading" :loading="props.loading" />

      <LoadMore
        v-if="!viewingSelected && !viewingExcluded"
        :loading="props.loading"
        :no-more-data="props.noMoreData"
        :has-data="props.novels.length > 0"
        @load-more="emit('load-more')"
      />
      <LoadMore
        v-else
        :loading="viewLoadingMore"
        :no-more-data="!viewHasMore"
        :has-data="viewShownCount > 0"
        @load-more="handleViewLoadMore"
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
