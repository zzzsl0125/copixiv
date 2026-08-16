import { ref, watch, nextTick, computed, onMounted, onUnmounted, type Ref } from 'vue'

export function useMasonryLayout<T>(
  items: Ref<T[]>,
  columnRefs: Ref<HTMLElement[]>,
) {
  const windowWidth = ref(window.innerWidth)
  const columns = ref<T[][]>([]) as Ref<T[][]>

  const updateWidth = () => {
    windowWidth.value = window.innerWidth
  }

  onMounted(() => {
    window.addEventListener('resize', updateWidth)
  })

  onUnmounted(() => {
    window.removeEventListener('resize', updateWidth)
  })

  const columnCount = computed(() => {
    if (windowWidth.value >= 1000) return 3
    if (windowWidth.value >= 500) return 2
    return 1
  })

  const runLayout = async (force: boolean) => {
    const count = columnCount.value
    const totalRendered = columns.value.reduce((acc, col) => acc + col.length, 0)
    const isLoadMore =
      !force && items.value.length > totalRendered && totalRendered !== 0

    let startIndex = 0

    if (!isLoadMore) {
      const newColumns: T[][] = Array.from({ length: count }, () => [])
      columns.value = newColumns as T[][]
      await nextTick()
    } else {
      startIndex = totalRendered
    }

    // Re-measure after each placement: item heights vary (title/text
    // length), so only a per-item pass keeps columns balanced.  A batched
    // estimate was tried and reverted — mixing pixel heights with item
    // counts is dimensionally wrong on load-more (the whole new page
    // stacked into the single shortest column, leaving the others blank).
    for (let index = startIndex; index < items.value.length; index++) {
      const item = items.value[index]
      let minHeightColIndex = 0
      let minHeight = Infinity

      for (let i = 0; i < count; i++) {
        const colElement = columnRefs?.value?.[i]
        const height = colElement ? colElement.offsetHeight : 0

        if (height < minHeight) {
          minHeight = height
          minHeightColIndex = i
        }
      }

      const targetCol = columns.value[minHeightColIndex]
      if (targetCol) {
        targetCol.push(item as T)
      }

      await nextTick()
    }
  }

  // Serialized runner — only ONE layout loop may run at a time.  A second
  // trigger arriving mid-run is coalesced and re-executed afterwards.
  // Without this, two triggers in the same flush (e.g. two watchers, or a
  // watcher + explicit relayout) interleaved their async loops and each
  // pushed every item into the shared columns → every novel rendered twice.
  let layoutRunning = false
  // null = nothing pending; false = heuristic run; true = forced rebuild
  // (a forced rebuild subsumes a queued heuristic append).
  let layoutPending: boolean | null = null

  const layoutItems = (force = false) => {
    if (layoutRunning) {
      layoutPending = layoutPending === null ? force : layoutPending || force
      return
    }
    layoutRunning = true
    void runLayout(force).finally(() => {
      layoutRunning = false
      if (layoutPending !== null) {
        const next = layoutPending
        layoutPending = null
        layoutItems(next)
      }
    })
  }

  watch(columnCount, () => {
    layoutItems(true)
  })

  // Length changes cover load-more (in-place push → append via the
  // totalRendered heuristic) and the initial load.  Whole-array swaps
  // (e.g. the batch-mode 「查看已选」view) cannot be told apart from
  // load-more by length alone, so callers invoke relayout() explicitly.
  watch(() => items.value.length, () => {
    layoutItems()
  }, { immediate: true })

  const relayout = () => {
    layoutItems(true)
  }

  return { windowWidth, columnCount, columns, relayout }
}
