import { ref, computed, watch, nextTick, type Ref } from 'vue'
import { novelApi, getApiErrorMessage } from '../api'
import type { Novel, NovelFilters, BatchScope, OrderBy } from '../types'
import { usePagedNovelIdView } from './usePagedNovelIdView'
import { useToast } from './useToast'

export interface UseCollectionViewsOptions {
  /** 当前筛选条件（经 vue-router query 派生）。 */
  filters: Readonly<Ref<NovelFilters>>
  /** 批量范围（selected 视图的 id 集来源）。 */
  batchScope: Ref<BatchScope | null>
  /** 批量模式开关（离开批量模式 → 关闭已选视图）。 */
  batchMode: Ref<boolean>
  /** 是否已有勾选（选集清空 → 关闭已选视图）。 */
  hasSelection: Ref<boolean>
  /** 浏览列表（集合视图非激活时的 displayNovels fallback）。 */
  novels: Ref<Novel[]>
  /** 视图活跃状态上报（页面转发为 emit('collection-view')）。 */
  onActiveChange: (active: boolean) => void
  /** 布局重测（页面传入 useVirtualMasonry 的 measure）。 */
  afterLayout: () => void
}

/**
 * 「查看已选」集合视图编排：
 * - 实例化 `selectedView` (`usePagedNovelIdView`)；
 * - 带 `selectedSorting` + `selectedSeq` 防竞态与首屏 `EmptyState`/`0 / 0` 闪烁；
 * - 离开批量模式 / 选集清空 / 排序变化时自动同步。
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

  const selectedView = usePagedNovelIdView()
  const viewingSelected = ref(false)
  const selectedSorting = ref(false)
  let selectedSeq = 0

  const displayNovels = computed<Novel[]>(() =>
    viewingSelected.value ? selectedView.novels.value : novels.value,
  )

  const viewLoading = computed(() => selectedSorting.value || selectedView.loading.value)
  const viewLoadingMore = computed(() => selectedView.loadingMore.value)
  const viewHasMore = computed(() => selectedView.hasMore.value)
  const viewShownCount = computed(() => selectedView.novels.value.length)
  const viewTotalCount = computed(() => selectedView.totalIds.value)

  function resetSelectedView() {
    selectedSeq++
    selectedSorting.value = false
    viewingSelected.value = false
    selectedView.reset()
  }

  /** 关闭集合视图的统一收尾：先 reset，再等 DOM 更新后重排。 */
  async function closeView() {
    resetSelectedView()
    await nextTick()
    afterLayout()
  }

  /**
   * 集合视图可用的排序子集：`random` 在集合内禁用（保持勾选顺序），
   * 其余原样透传。
   */
  function scopeOrderBy(): Exclude<OrderBy, 'random'> | undefined {
    const o = filters.value.order_by
    return o === 'random' ? undefined : o
  }

  async function reloadSelectedIds(): Promise<boolean> {
    const seq = ++selectedSeq
    const ids = batchScope.value?.novel_ids ?? []
    if (ids.length === 0) return false
    const orderBy = scopeOrderBy()
    selectedSorting.value = true
    try {
      let sortedIds = ids
      if (orderBy === 'id') {
        const dir = filters.value.order_direction === 'ASC' ? 1 : -1
        sortedIds = [...ids].sort((a, b) => dir * (Math.abs(a) - Math.abs(b)))
      } else if (orderBy) {
        const result = await novelApi.sortNovelIds(
          ids,
          orderBy,
          filters.value.order_direction,
        )
        if (seq !== selectedSeq) return false
        sortedIds = result.ids
      }
      await selectedView.start(sortedIds)
      return seq === selectedSeq
    } finally {
      if (seq === selectedSeq) selectedSorting.value = false
    }
  }

  async function toggleViewSelected() {
    if (viewingSelected.value) {
      await closeView()
      return
    }
    const ids = batchScope.value?.novel_ids ?? []
    if (ids.length === 0) return
    viewingSelected.value = true
    const seqSnapshot = selectedSeq + 1
    try {
      const active = await reloadSelectedIds()
      if (!active) return
      window.scrollTo({ top: 0 })
      await nextTick()
      afterLayout()
    } catch (err: unknown) {
      if (seqSnapshot !== selectedSeq) return
      toast.error(getApiErrorMessage(err, '加载已选小说失败'))
      await closeView()
    }
  }

  function handleViewLoadMore() {
    if (viewingSelected.value) void selectedView.loadMore()
  }

  /** 元数据编辑保存后原地替换集合视图里的那一本（不重取）。 */
  function patchNovel(updated: Novel) {
    const list = selectedView.novels.value
    const index = list.findIndex((novel) => novel.id === updated.id)
    if (index >= 0) list[index] = updated
  }

  /** Toggle a novel's state field (or author-wide special follow) in active view. */
  function patchNovelState(
    payload: { id: number; field: 'is_favourite' | 'is_special_follow'; value: boolean },
    authorId?: number,
  ) {
    for (const novel of selectedView.novels.value) {
      if (payload.field === 'is_special_follow' && authorId !== undefined) {
        if (novel.author_id === authorId) {
          novel.is_special_follow = payload.value
        }
      } else if (novel.id === payload.id) {
        novel[payload.field] = payload.value
      }
    }
  }

  /** Remove a deleted novel from the selected collection view cleanly. */
  function removeNovel(novelId: number) {
    selectedView.removeId(novelId)
  }

  watch(
    () => batchMode.value,
    (on) => {
      if (on) {
        void nextTick().then(afterLayout)
        return
      }
      void closeView()
    },
  )

  watch(
    () => hasSelection.value,
    (has) => {
      if (!has && viewingSelected.value) void closeView()
    },
  )

  watch(
    viewingSelected,
    (active) => {
      onActiveChange(active)
    },
    { immediate: true },
  )

  watch(
    [() => filters.value.order_by, () => filters.value.order_direction],
    ([o, d], [po, pd]) => {
      if (o === po && d === pd) return
      if (!viewingSelected.value) return
      const seqSnapshot = selectedSeq + 1
      reloadSelectedIds()
        .then(async (active) => {
          if (!active) return
          window.scrollTo({ top: 0 })
          await nextTick()
          afterLayout()
        })
        .catch((err: unknown) => {
          if (seqSnapshot !== selectedSeq) return
          toast.error(getApiErrorMessage(err, '重新排序失败'))
          void closeView()
        })
    },
  )

  return {
    viewingSelected,
    selectedView,
    displayNovels,
    viewLoading,
    viewLoadingMore,
    viewHasMore,
    viewShownCount,
    viewTotalCount,
    toggleViewSelected,
    handleViewLoadMore,
    patchNovel,
    patchNovelState,
    removeNovel,
  }
}
