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
    // Tracked column heights — avoids re-reading every column's
    // offsetHeight on each placement (DOM reads force reflow).  The
    // shortest column is found from the JS array (zero DOM reads);
    // only the column that just received an item is re-measured.
    let colHeights: number[]

    if (!isLoadMore) {
      const newColumns: T[][] = Array.from({ length: count }, () => [])
      columns.value = newColumns as T[][]
      await nextTick()
    } else {
      startIndex = totalRendered
    }

    // Seed heights from the actual DOM once (count reads, not count×N).
    // For a fresh layout the columns are empty (height ≈ 0 or padding);
    // for load-more they hold the already-rendered items' heights.
    colHeights = Array.from({ length: count }, (_, i) => {
      const el = columnRefs?.value?.[i]
      return el ? el.offsetHeight : 0
    })

    for (let index = startIndex; index < items.value.length; index++) {
      const item = items.value[index]

      // Find the shortest column from tracked heights — no DOM read.
      let minHeightColIndex = 0
      let minHeight = colHeights[0]
      for (let i = 1; i < count; i++) {
        if (colHeights[i] < minHeight) {
          minHeight = colHeights[i]
          minHeightColIndex = i
        }
      }

      const targetCol = columns.value[minHeightColIndex]
      if (targetCol) {
        targetCol.push(item as T)
      }

      await nextTick()

      // Re-measure only the column that changed — 1 DOM read instead
      // of reading all columns (count reads) per item.
      const colEl = columnRefs?.value?.[minHeightColIndex]
      if (colEl) {
        colHeights[minHeightColIndex] = colEl.offsetHeight
      }
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
