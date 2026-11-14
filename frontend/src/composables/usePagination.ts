import { ref, type Ref } from 'vue'
import { getApiErrorMessage } from '../api'

export interface PaginationResult<T> {
  items: Ref<T[]>
  loading: Ref<boolean>
  error: Ref<string | null>
  hasMore: Ref<boolean>
  loadData: (loadMore?: boolean) => Promise<void>
  refresh: (options?: { silent?: boolean }) => Promise<void>
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
  let generation = 0
  let refreshing = false

  const loadData = async (loadMore = false) => {
    if (loading.value) return
    generation += 1
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

  /** Replace the first page IN PLACE — for polling (no flicker).
   *
   * ``loadData()`` clears the list before fetching, which flashes an empty
   * state on every poll.  Instead of replacing every object, this MERGES
   * the fresh page-0 rows by ``id`` into the existing ones while preserving
   * any already-loaded subsequent pages: unchanged rows keep their object
   * identity (and therefore produce zero DOM writes), only genuinely changed
   * fields are patched, and ``silent`` skips the loading flag.
   */
  const refresh = async (options: { silent?: boolean } = {}) => {
    if (loading.value || refreshing) return
    refreshing = true
    const runGen = generation
    if (!options.silent) loading.value = true
    try {
      const result = await fetcher(0, pageSize)
      if (runGen !== generation) return

      const isArrayResult = Array.isArray(result)
      const newItems = isArrayResult ? result : result.items

      const byId = new Map<unknown, T>()
      for (const it of items.value) {
        const id = (it as { id?: unknown }).id
        if (id !== undefined && id !== null) byId.set(id, it)
      }

      // Value comparison: items.value holds reactive PROXIES of the rows,
      // so identity comparison against the fresh raw objects would always
      // report "changed" and defeat the whole point of the merge.
      const sameValue = (a: unknown, b: unknown) =>
        a === b || JSON.stringify(a) === JSON.stringify(b)

      const mergedHead: T[] = []
      const headIds = new Set<unknown>()
      let changed = false

      for (const fresh of newItems) {
        const freshObj = fresh as { id?: unknown }
        if (freshObj.id !== undefined && freshObj.id !== null) {
          headIds.add(freshObj.id)
        }
        const old = freshObj.id !== undefined && freshObj.id !== null
          ? byId.get(freshObj.id)
          : undefined
        if (old !== undefined) {
          const oldObj = old as Record<string, unknown>
          const freshRecord = freshObj as Record<string, unknown>
          for (const key of Object.keys(freshRecord)) {
            if (!sameValue(oldObj[key], freshRecord[key])) {
              oldObj[key] = freshRecord[key]
              changed = true
            }
          }
          mergedHead.push(old)
        } else {
          changed = true
          mergedHead.push(fresh)
        }
      }

      // Keep pages 2+ loaded via LoadMore intact so background polling does
      // not truncate scrolled history back to pageSize.
      const tail: T[] = !isArrayResult && newItems.length >= pageSize
        ? items.value.filter((it) => {
            const id = (it as { id?: unknown }).id
            return id !== undefined && id !== null && !headIds.has(id)
          })
        : []

      const merged = [...mergedHead, ...tail]
      if (
        !changed &&
        (merged.length !== items.value.length ||
          merged.some((it, idx) => it !== items.value[idx]))
      ) {
        changed = true
      }

      if (changed) items.value = merged
      offset.value = merged.length
      hasMore.value = isArrayResult
        ? false
        : result.total !== undefined
          ? offset.value < result.total
          : newItems.length >= pageSize
    } catch (err: unknown) {
      if (runGen === generation) {
        error.value = getApiErrorMessage(err, '加载失败')
      }
    } finally {
      refreshing = false
      if (!options.silent) loading.value = false
    }
  }

  return { items, loading, error, hasMore, loadData, refresh }
}
