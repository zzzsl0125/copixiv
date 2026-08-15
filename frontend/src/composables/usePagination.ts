import { ref, type Ref } from 'vue'
import { getApiErrorMessage } from '../api/errors'

export interface PaginationResult<T> {
  items: Ref<T[]>
  loading: Ref<boolean>
  error: Ref<string | null>
  hasMore: Ref<boolean>
  loadData: (loadMore?: boolean) => Promise<void>
}

export function usePagination<T>(
  fetcher: (offset: number, limit: number) => Promise<T[] | { items: T[]; total?: number }>,
  pageSize = 20,
): PaginationResult<T> {
  const items = ref<T[]>([]) as Ref<T[]>
  const loading = ref(false)
  const error = ref<string | null>(null)
  const hasMore = ref(false)
  const offset = ref(0)

  const loadData = async (loadMore = false) => {
    if (loading.value) return
    if (!loadMore) {
      offset.value = 0
      items.value = []
    }
    if (loadMore && !hasMore.value) return

    loading.value = true
    error.value = null

    try {
      const result = await fetcher(offset.value, pageSize)

      if (Array.isArray(result)) {
        items.value = result
        hasMore.value = false
        return
      }

      const newItems = result.items
      items.value = loadMore ? [...items.value, ...newItems] : newItems
      offset.value += newItems.length
      // Prefer the server's authoritative total; fall back to page size for
      // endpoints that don't return one.
      hasMore.value = result.total !== undefined
        ? offset.value < result.total
        : newItems.length >= pageSize
    } catch (err: unknown) {
      error.value = getApiErrorMessage(err, '加载失败')
    } finally {
      loading.value = false
    }
  }

  return { items, loading, error, hasMore, loadData }
}
