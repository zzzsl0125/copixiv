import { ref, type Ref } from 'vue'
import { getApiErrorMessage } from '../api/errors'

export interface CursorPaginationResult<T> {
  items: Ref<T[]>
  loading: Ref<boolean>
  error: Ref<string | null>
  cursor: Ref<unknown>
  noMoreData: Ref<boolean>
  loadData: (isLoadMore?: boolean) => Promise<void>
  reset: () => void
  handleLoadMore: () => Promise<void>
}

export function useCursorPagination<T>(
  fetcher: (cursor?: unknown) => Promise<{ items: T[]; cursor?: unknown }>,
): CursorPaginationResult<T> {
  const items = ref<T[]>([]) as Ref<T[]>
  const loading = ref(false)
  const error = ref<string | null>(null)
  const cursor = ref<unknown>(null)
  const noMoreData = ref(false)

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

    try {
      const currentCursor = isLoadMore ? cursor.value : undefined
      const res = await fetcher(currentCursor)
      if (seq !== requestSeq) return

      const newItems = res.items || []

      if (isLoadMore) {
        items.value.push(...newItems)
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
    items.value = []
  }

  const handleLoadMore = (): Promise<void> => loadData(true)

  return { items, loading, error, cursor, noMoreData, loadData, reset, handleLoadMore }
}
