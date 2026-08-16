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

  const layoutItems = async () => {
    const count = columnCount.value
    const totalRendered = columns.value.reduce((acc, col) => acc + col.length, 0)
    const isLoadMore = items.value.length > totalRendered && totalRendered !== 0

    let startIndex = 0

    if (!isLoadMore) {
      const newColumns: T[][] = Array.from({ length: count }, () => [])
      columns.value = newColumns as T[][]
      await nextTick()
      startIndex = 0
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

  watch(columnCount, () => {
    columns.value = []
    layoutItems()
  })

  watch(() => items.value.length, () => {
    layoutItems()
  }, { immediate: true })

  return { windowWidth, columnCount, columns }
}
