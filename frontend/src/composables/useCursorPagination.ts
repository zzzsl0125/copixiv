import { ref, type Ref } from 'vue'
import { getApiErrorMessage } from '../api'

export interface CursorPaginationResult<T> {
  items: Ref<T[]>
  loading: Ref<boolean>
  error: Ref<string | null>
  cursor: Ref<unknown>
  noMoreData: Ref<boolean>
  loadData: (isLoadMore?: boolean) => Promise<void>
  reset: () => void
  handleLoadMore: () => Promise<void>
  /** 上一次 load-more 因为到达上限而丢掉的头部条数（0 = 什么都没丢）。 */
  lastTrimmed: Ref<number>
}

export interface CursorPaginationOptions {
  /**
   * 列表保留上限（条数）。超过就从**头部**（最老的）裁掉。
   *
   * 为什么需要：`LoadMore.vue` 是哨兵式自动加载（`rootMargin: 100px`，滚到底就
   * 自动再取），所以"滚到底不停"能把列表堆到上千条。实测（真实 Firefox）：
   * 600 张卡 = 21,497 个 DOM 节点、跨列断点重排时主线程单帧卡 1,289ms。
   * 有界之后这些数字有了硬上限。
   *
   * 代价（必须知道）：cursor 是**单向向后**的，所以裁掉的部分往回滚不会自动
   * 回来——重新滚到顶部只能靠再取一次。这是"有界 DOM"换取"恒定开销"的
   * 明码标价，不是 bug。
   */
  maxItems?: number
}

export function useCursorPagination<T>(
  fetcher: (cursor?: unknown) => Promise<{ items: T[]; cursor?: unknown }>,
  options: CursorPaginationOptions = {},
): CursorPaginationResult<T> {
  const items = ref<T[]>([]) as Ref<T[]>
  const loading = ref(false)
  const error = ref<string | null>(null)
  const cursor = ref<unknown>(null)
  const noMoreData = ref(false)
  const lastTrimmed = ref(0)

  const maxItems = options.maxItems

  /**
   * 只裁头部，不动 cursor。
   *
   * cursor 是「下一页从哪开始」的位置，它指向列表**尾部**之后——裁掉头部
   * 不影响它。所以 load-more 仍然从正确的下一页开始，不需要重取。
   */
  const trim = () => {
    if (!maxItems || items.value.length <= maxItems) return 0
    const drop = items.value.length - maxItems
    items.value.splice(0, drop)
    return drop
  }

  // A reload requested while a fetch is in flight invalidates that fetch and
  // is executed once it settles — otherwise a search during the first load
  // would be swallowed by the `loading` guard and stale results would win.
  let requestSeq = 0
  let pendingReload = false

  const loadData = async (isLoadMore = false) => {
    if (loading.value) {
      if (!isLoadMore) {
        requestSeq++
        pendingReload = true
      }
      return
    }
    if (isLoadMore && noMoreData.value) return

    const seq = ++requestSeq
    loading.value = true
    error.value = null
    lastTrimmed.value = 0

    try {
      const currentCursor = isLoadMore ? cursor.value : undefined
      const res = await fetcher(currentCursor)
      if (seq !== requestSeq) return

      const newItems = res.items || []

      if (isLoadMore) {
        items.value.push(...newItems)
        lastTrimmed.value = trim()
      } else {
        items.value = newItems
      }

      if (res.cursor) {
        cursor.value = res.cursor
        noMoreData.value = false
      } else {
        cursor.value = null
        noMoreData.value = true
      }
    } catch (err: unknown) {
      if (seq !== requestSeq) return
      error.value = getApiErrorMessage(err, '加载失败，请检查网络或后端状态')
    } finally {
      if (seq === requestSeq) {
        loading.value = false
      } else if (pendingReload) {
        pendingReload = false
        loading.value = false
        void loadData(false)
      }
    }
  }

  const reset = () => {
    cursor.value = null
    noMoreData.value = false
    lastTrimmed.value = 0
    items.value = []
  }

  const handleLoadMore = (): Promise<void> => loadData(true)

  return { items, loading, error, cursor, noMoreData, loadData, reset, handleLoadMore, lastTrimmed }
}
