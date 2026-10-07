<script setup lang="ts">
import { computed, ref, toRef, nextTick, onMounted } from 'vue'
import NovelCard, { type NovelStateChange } from '../components/features/NovelCard.vue'
import NovelHeader from '../components/features/NovelHeader.vue'
import LoadMore from '../components/features/LoadMore.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import BatchBar from '../components/features/BatchBar.vue'
import UploadBar from '../components/features/UploadBar.vue'
import BatchDeleteModal from '../components/features/BatchDeleteModal.vue'
import BatchTagModal from '../components/features/BatchTagModal.vue'
import BatchDownloadModal from '../components/features/BatchDownloadModal.vue'
import EditNovelModal from '../components/features/EditNovelModal.vue'
import { useMasonryLayout, useCollectionViews, useToast } from '../composables'
import { tagPreferenceApi, getApiErrorMessage } from '../api'
import ExclusionBar from '../components/features/ExclusionBar.vue'
import type { Novel, NovelFilters, TagPreference, BatchScope, BatchOperationResult } from '../types'
import type { SectionId } from '../config'

// ---- 组合根绑定契约（F1 收敛后的单源类型；App.vue 以 import type 复用）----
/** 列表浏览状态：原 novels/loading/error/noMoreData/hasExcluded 五个 props 打包。 */
export type NovelsState = {
  novels: Novel[]
  loading: boolean
  error: string | null
  noMoreData: boolean
  hasExcluded: boolean
}

/** 批量模式状态：原 9 个 batch props 打包；App 仅首页路由注入，否则 undefined。 */
export type BatchState = {
  mode: boolean
  matchedCount: number
  countLoading: boolean
  selectAllLoading: boolean
  selectedCount: number
  hasSelection: boolean
  hasFilter: boolean
  scope: BatchScope | null
  isSelected: (id: number) => boolean
}

/** 列表域命令联合：原 search/card-search/load-more/update:filters/collection-view 五事件。 */
export type NovelsCommand =
  | { type: 'search'; keyword?: string }
  | { type: 'card-search'; payload: { type: string; value: string | number } }
  | { type: 'load-more' }
  /** 打开「我的上传」上传弹窗（由 UploadBar 触发，App 侧开模态）。 */
  | { type: 'upload' }
  | { type: 'update-filters'; payload: Partial<NovelFilters> }
  | { type: 'collection-view'; payload: boolean }

/** 批量域动作联合：原 batch-toggle-card/select-all/clear/clear-scope/operation-success/task-submitted 六事件。 */
export type BatchAction =
  | { type: 'toggle-card'; id: number }
  | { type: 'select-all' }
  | { type: 'clear' }
  | { type: 'clear-scope' }
  | { type: 'operation-success'; payload: { operation: string; result: BatchOperationResult } }
  | { type: 'task-submitted'; payload: { operation: string; task_id: number; matched: number } }

const props = defineProps<{
  isSidebarOpen: boolean
  filters: NovelFilters
  novelsState: NovelsState
  /** 批量状态：App 仅在首页路由注入（其他路由 undefined）；未注入时按关闭态防御取值。 */
  batchState?: BatchState
  /** 当前书架 section：仅 `uploads` 会多渲染一条上传工具条（见 UploadBar）。 */
  activeSection?: SectionId | null
}>()

const emit = defineEmits<{
  (e: 'toggle-sidebar'): void
  (e: 'logo-click'): void
  (e: 'novel-state-changed', payload: NovelStateChange): void
  /** 元数据编辑保存成功：主列表里的那一本由 App 原地替换（集合视图副本本页自理）。 */
  (e: 'novel-updated', novel: Novel): void
  (e: 'novels-command', command: NovelsCommand): void
  (e: 'batch-action', action: BatchAction): void
}>()

const toast = useToast()
const columnRefs = ref<HTMLElement[]>([])
const activeCardId = ref<number | string | null>(null)
const tagPreferences = ref<TagPreference[]>([])

// ---- 解包粗粒度状态（语义与原 16 props 逐项等价；batchState 未注入时防御取关闭态）----
const novels = computed(() => props.novelsState.novels)
const loading = computed(() => props.novelsState.loading)
const error = computed(() => props.novelsState.error)
const noMoreData = computed(() => props.novelsState.noMoreData)
const hasExcluded = computed(() => props.novelsState.hasExcluded)

const batchMode = computed(() => props.batchState?.mode ?? false)
const matchedCount = computed(() => props.batchState?.matchedCount ?? 0)
const countLoading = computed(() => props.batchState?.countLoading ?? false)
const selectAllLoading = computed(() => props.batchState?.selectAllLoading ?? false)
const selectedCount = computed(() => props.batchState?.selectedCount ?? 0)
const hasSelection = computed(() => props.batchState?.hasSelection ?? false)
const hasFilter = computed(() => props.batchState?.hasFilter ?? false)
const batchScope = computed(() => props.batchState?.scope ?? null)
const isBatchSelected = (id: number) => props.batchState?.isSelected(id) ?? false

// ---- batch operation modal states ----
const deleteOpen = ref(false)
const tagModalOpen = ref(false)
const tagOperation = ref<'add_tags' | 'remove_tags'>('add_tags')
const exportOpen = ref(false)

// ---- 元数据编辑：弹窗宿主放在本页（初值来自 displayNovels 里的那张卡），
// 保存成功后本页更新集合视图副本、App 更新主列表（见 handleNovelUpdated）。
const editingNovel = ref<Novel | null>(null)

// ---- 「查看已选」/「查看被排除」集合视图编排（F2 产物，编排逻辑不动，
// 仅换取值通路：props.batchScope → batchScope、props.batchMode → batchMode 等）----
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
} = useCollectionViews({
  filters: toRef(props, 'filters'),
  batchScope,
  batchMode,
  hasSelection,
  novels,
  onActiveChange: (active) => emit('novels-command', { type: 'collection-view', payload: active }),
  afterLayout: () => relayout(),
})

const { columns, relayout } = useMasonryLayout<Novel>(
  toRef(displayNovels),
  columnRefs,
)

const scopeLabel = computed(() => `已勾选的 ${selectedCount.value} 篇`)

// 清除按钮：列表视图 → 仅清当前筛选范围；查看已选视图 → 清空全部。
function handleBarClear() {
  if (viewingSelected.value) {
    emit('batch-action', { type: 'clear' })
  } else {
    emit('batch-action', { type: 'clear-scope' })
  }
}

// 「查看已选」视图是选择集的实时投影：在此视图内取消勾选必须让卡片
// 立即从列表消失（所见即所得），而不是留下一张无蓝框的卡片、直到退
// 出视图才真正移除。浏览列表视图不作此处理——那里仅由 App 维护选择
// 集，卡片本就不一定属于当前筛选范围。
function handleBatchToggleCard(id: number) {
  if (viewingSelected.value && isBatchSelected(id)) {
    selectedView.removeId(id)
    // 当前页被摘空但仍有未加载的已选时自动补一页，否则 LoadMore 会因
    // hasData=false 整体消失，把用户卡在空白网格里。
    if (selectedView.novels.value.length === 0 && selectedView.hasMore.value) {
      void selectedView.loadMore()
    }
    void nextTick().then(relayout)
  }
  emit('batch-action', { type: 'toggle-card', id })
}

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

/** 编辑保存成功：卡片上的初值对象换成响应值，并同步两份副本。
 *
 * - 弹窗还开着时（「同步」路径）把它手里的初值也换成响应值：弹窗的 diff 基线、
 *   字段策略、解析 hint 读的都是 `props.novel`，不换的话同步完保存按钮会莫名
 *   亮起来（其实一个字段都没改）。保存路径上这行是空转——随即 `close`。
 * - 集合视图（查看已选 / 被排除）的列表是**本页**持有的副本 → patchNovel；
 * - 主列表由 App 拥有 → 上抛 `novel-updated`（与收藏/追更的
 *   `novel-state-changed` 同一条"原地 patch、不重取"的路）。
 * 标题/标签变化会改卡片高度，所以补一次瀑布流重排。 */
const handleNovelUpdated = (updated: Novel) => {
  if (editingNovel.value?.id === updated.id) editingNovel.value = updated
  patchNovel(updated)
  void nextTick().then(relayout)
  emit('novel-updated', updated)
}
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full" @click="activeCardId = null">
    <div class="sticky top-0 z-20">
      <NovelHeader
        :filters="props.filters"
        @search="(keyword?: string) => emit('novels-command', { type: 'search', keyword })"
        @update:filters="emit('novels-command', { type: 'update-filters', payload: $event })"
        @toggle-sidebar="emit('toggle-sidebar')"
        @logo-click="emit('logo-click')"
      />
      <ExclusionBar
        v-if="props.filters.keyword.trim() !== '' && hasExcluded"
        :is-viewing-excluded="viewingExcluded"
        :interactive="!batchMode"
        @toggle-view-excluded="toggleViewExcluded"
      />
      <BatchBar
        v-if="batchMode"
        :matched-count="matchedCount"
        :count-loading="countLoading"
        :select-all-loading="selectAllLoading"
        :selected-count="selectedCount"
        :has-selection="hasSelection"
        :has-filter="hasFilter"
        :is-viewing-selected="viewingSelected"
        @select-all="emit('batch-action', { type: 'select-all' })"
        @clear="handleBarClear"
        @toggle-view-selected="toggleViewSelected"
        @export="exportOpen = true"
        @add-tags="tagOperation = 'add_tags'; tagModalOpen = true"
        @remove-tags="tagOperation = 'remove_tags'; tagModalOpen = true"
        @delete="deleteOpen = true"
      />
      <!-- 我的上传工具条：与批量栏互斥——两条栏抢的是同一条 sticky 槽位，
           批量模式优先（模式栏语义更强：此时用户在操作已有书，不在收新书）。
           空书架时这条栏就在空状态正上方，仍是最显眼的入口。 -->
      <UploadBar
        v-if="props.activeSection === 'uploads' && !batchMode"
        @upload="emit('novels-command', { type: 'upload' })"
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

      <div v-if="displayNovels.length > 0" class="flex gap-6 items-start">
        <div v-for="(col, colIndex) in columns" :key="colIndex" :ref="el => { if (el) columnRefs[colIndex] = el as HTMLElement }" class="flex-1 flex flex-col gap-6 w-full min-w-0">
          <NovelCard
            v-for="novel in col"
            :key="novel.id"
            :novel="novel"
            :is-active="activeCardId === novel.id"
            :tag-preferences="tagPreferences"
            :batch-mode="batchMode"
            :batch-selected="isBatchSelected(novel.id)"
            @toggle-active="handleToggleActive"
            @toggle-batch-select="handleBatchToggleCard"
            @search="(type, value) => emit('novels-command', { type: 'card-search', payload: { type, value } })"
            @state-changed="emit('novel-state-changed', $event)"
            @edit="editingNovel = $event"
          />
        </div>
      </div>

      <EmptyState v-else-if="!loading && !viewLoading" :loading="loading" />

      <LoadMore
        v-if="!viewingSelected && !viewingExcluded"
        :loading="loading"
        :no-more-data="noMoreData"
        :has-data="novels.length > 0"
        @load-more="emit('novels-command', { type: 'load-more' })"
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
      :scope="batchScope"
      :scope-label="scopeLabel"
      @close="deleteOpen = false"
      @success="(payload) => emit('batch-action', { type: 'operation-success', payload: { operation: 'delete', result: payload } })"
      @task-submitted="(payload) => emit('batch-action', { type: 'task-submitted', payload: { operation: 'delete', ...payload } })"
      @error="toast.error"
    />
    <BatchTagModal
      :is-open="tagModalOpen"
      :operation="tagOperation"
      :scope="batchScope"
      :scope-label="scopeLabel"
      @close="tagModalOpen = false"
      @success="(payload) => emit('batch-action', { type: 'operation-success', payload: { operation: tagOperation, result: payload } })"
      @task-submitted="(payload) => emit('batch-action', { type: 'task-submitted', payload: { operation: tagOperation, ...payload } })"
      @error="toast.error"
    />
    <BatchDownloadModal
      :is-open="exportOpen"
      :keyword="props.filters.keyword"
      :order_by="props.filters.order_by"
      :order_direction="props.filters.order_direction"
      :min_like="props.filters.min_like"
      :min_text="props.filters.min_text"
      :novel-ids="batchScope?.novel_ids"
      @close="exportOpen = false"
      @download-success="toast.success(`已开始下载：${$event}`)"
      @download-error="toast.error"
      @task-submitted="(payload) => emit('batch-action', { type: 'task-submitted', payload: { operation: 'export', ...payload } })"
    />

    <!-- 元数据编辑（卡片底部操作条「编辑」）：宿主放在本页——初值就是
         displayNovels 里的那张卡，保存后本页更新集合视图副本、App 更新主列表，
         两处都原地 patch、不整页重取（§6.3）。 -->
    <EditNovelModal
      :is-open="editingNovel !== null"
      :novel="editingNovel"
      @close="editingNovel = null"
      @updated="handleNovelUpdated"
      @error="toast.error"
    />
  </div>
</template>