import { ref, computed, watch, nextTick, type Ref } from 'vue'
import { novelApi, getApiErrorMessage } from '../api'
import type { Novel, NovelFilters, BatchScope } from '../types'
import { usePagedNovelIdView } from './usePagedNovelIdView'
import { useToast } from './useToast'

export interface UseCollectionViewsOptions {
  /** 当前筛选条件（来自 App 的 useNovels 状态，经页面 props 透传）。 */
  filters: Ref<NovelFilters>
  /** 批量范围（selected 视图的 id 集来源）。 */
  batchScope: Ref<BatchScope | null>
  /** 批量模式开关（离开批量模式 → 关闭已选视图；进入 → 关闭被排除视图）。 */
  batchMode: Ref<boolean>
  /** 是否已有勾选（选集清空 → 关闭已选视图）。 */
  hasSelection: Ref<boolean>
  /** 浏览列表（集合视图非激活时的 displayNovels fallback）。 */
  novels: Ref<Novel[]>
  /** 视图活跃状态上报（页面转发为 emit('collection-view')）。 */
  onActiveChange: (active: boolean) => void
  /** 布局重排（页面传入 useMasonryLayout 的 relayout）。 */
  afterLayout: () => void
}

/**
 * 「查看已选」/「查看被排除」两个集合视图的编排（自 Novels.vue L68–307
 * 逐行搬移，语义不变）：
 *
 * - 内部实例化两个 usePagedNovelIdView（selectedView / excludedView），
 *   二者互斥：进入一个先 reset 另一个（批量模式优先）；
 * - 视图重算/重排/回顶的时序（含大量 `await nextTick(); relayout()`）原样保留，
 *   布局重排经注入的 afterLayout 回调交给页面；
 * - 5 类 watch 全部内聚：batchMode 互斥关闭 / hasSelection 清空关闭 /
 *   viewing 活跃上报（immediate 首报 false）/ 筛选变化触发被排除视图重算 /
 *   排序变化触发两个集合视图重排重取；
 * - excluded 侧重取带 seq 防竞态（loadExcludedView），error toast 走模块级
 *   useToast 单例，与现有风格一致。
 */
export function useCollectionViews(options: UseCollectionViewsOptions) {
  const {
    filters,
    batchScope,
    batchMode,
    hasSelection,
    novels,
    onActiveChange,
    afterLayout,
  } = options
  const toast = useToast()

  // ---- 「查看已选」/「查看被排除」views: the grid swaps to an explicit
  // id list, fetched in pages so huge lists stay smooth (infinite scroll).
  // Shared paging lives in usePagedNovelIdView; the two views are mutually
  // exclusive (批量模式优先) and exit on new searches. ----
  const selectedView = usePagedNovelIdView()
  const excludedView = usePagedNovelIdView()
  const viewingSelected = ref(false)
  const viewingExcluded = ref(false)

  const displayNovels = computed<Novel[]>(() =>
    viewingSelected.value
      ? selectedView.novels.value
      : viewingExcluded.value
        ? excludedView.novels.value
        : novels.value,
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

  // ---- 视图的集合顺序：随机在集合视图内禁用，其余按当前排序条件取序 ----
  function scopeOrderBy(): string | undefined {
    const o = filters.value.order_by
    if (o === 'random') return undefined  // 集合视图不支持随机 → 保持范围/勾选顺序
    if (o === 'id' || o === 'like' || o === 'text') return o
    return undefined
  }

  async function reloadSelectedIds() {
    const ids = batchScope.value?.novel_ids ?? []
    if (ids.length === 0) return
    const orderBy = filters.value.order_by
    if (orderBy === 'id') {
      // id 排序本地完成（id 列表本来就在内存里）
      const dir = filters.value.order_direction === 'ASC' ? 1 : -1
      await selectedView.start([...ids].sort((a, b) => dir * (a - b)))
      return
    }
    if (orderBy === 'like' || orderBy === 'text') {
      const result = await novelApi.sortNovelIds(
        ids, orderBy, filters.value.order_direction,
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
        keyword: filters.value.keyword.trim() || undefined,
        min_like: filters.value.min_like,
        min_text: filters.value.min_text,
        order_by: scopeOrderBy(),
        order_direction: filters.value.order_direction,
      })
      if (seq !== excludedSeq) return  // 已有更新的请求，丢弃本次结果
      if (result.ids.length === 0) {
        // 新范围没有被排除的小说 → 退回浏览列表
        resetExcludedView()
        await nextTick()
        afterLayout()
        return
      }
      await excludedView.start(result.ids)
      window.scrollTo({ top: 0 })  // 新集合从头看起，阻断自动翻页级联
      await nextTick()
      afterLayout()
    } catch (err: unknown) {
      if (seq !== excludedSeq) return
      toast.error(getApiErrorMessage(err, '加载被排除小说失败'))
      resetExcludedView()
      await nextTick()
      afterLayout()
    }
  }

  async function toggleViewSelected() {
    if (viewingSelected.value) {
      resetSelectedView()
      await nextTick()
      afterLayout()
      return
    }
    const ids = batchScope.value?.novel_ids ?? []
    if (ids.length === 0) return
    resetExcludedView()  // 互斥：进入已选视图时退出被排除视图
    viewingSelected.value = true
    try {
      await reloadSelectedIds()
      window.scrollTo({ top: 0 })  // 新视图从头看起，阻断自动翻页级联
      await nextTick()
      afterLayout()
    } catch (err: unknown) {
      toast.error(getApiErrorMessage(err, '加载已选小说失败'))
      resetSelectedView()
      await nextTick()
      afterLayout()
    }
  }

  async function toggleViewExcluded() {
    if (viewingExcluded.value) {
      resetExcludedView()
      await nextTick()
      afterLayout()
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

  // Leaving batch mode (or an emptied selection) closes the selected view;
  // entering batch mode closes the excluded view (批量模式优先互斥).
  watch(() => batchMode.value, (on) => {
    if (on) {
      if (viewingExcluded.value) resetExcludedView()
    } else {
      resetSelectedView()
    }
    void nextTick().then(afterLayout)
  })

  watch(() => hasSelection.value, (has) => {
    if (!has && viewingSelected.value) {
      resetSelectedView()
      void nextTick().then(afterLayout)
    }
  })

  // 视图活跃状态上报给 App → 侧边栏在集合视图内禁用「随机」排序
  // immediate：组件挂载/重挂时立即上报 false，避免离开页面后残留 true
  watch([viewingSelected, viewingExcluded], ([s, e]) => {
    onActiveChange(s || e)
  }, { immediate: true })

  // 筛选条件变化 → 被排除视图原地重算（集合始终与当前范围一致）；
  // 关键词清空后该视图无展示意义（条已消失）→ 退回浏览列表。
  watch(
    [
      () => filters.value.keyword,
      () => filters.value.min_like,
      () => filters.value.min_text,
    ],
    () => {
      if (!viewingExcluded.value) return
      if (!filters.value.keyword.trim()) {
        resetExcludedView()
        void nextTick().then(afterLayout)
        return
      }
      void loadExcludedView()
    },
  )

  // 排序条件变化 → 两个集合视图各自按新排序重排/重取
  watch(
    [() => filters.value.order_by, () => filters.value.order_direction],
    ([o, d], [po, pd]) => {
      if (o === po && d === pd) return
      if (viewingSelected.value) {
        reloadSelectedIds()
          .then(async () => {
            window.scrollTo({ top: 0 })
            await nextTick()
            afterLayout()
          })
          .catch((err: unknown) => {
            toast.error(getApiErrorMessage(err, '重新排序失败'))
            resetSelectedView()
            void nextTick().then(afterLayout)
          })
      } else if (viewingExcluded.value) {
        void loadExcludedView()
      }
    },
  )

  return {
    viewingSelected,
    viewingExcluded,
    selectedView,
    excludedView,
    displayNovels,
    viewLoading,
    viewLoadingMore,
    viewHasMore,
    viewShownCount,
    viewTotalCount,
    toggleViewSelected,
    toggleViewExcluded,
    handleViewLoadMore,
  }
}