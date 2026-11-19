import {
  computed,
  nextTick,
  ref,
  watch,
  type ComponentPublicInstance,
  type CSSProperties,
  type ComputedRef,
  type Ref,
} from 'vue'
import { useWindowVirtualizer, type VirtualItem } from '@tanstack/vue-virtual'
import {
  useDebounceFn,
  useElementSize,
  useEventListener,
  useResizeObserver,
  useWindowSize,
} from '@vueuse/core'

/** Default gap between masonry lanes / cards in pixels (matches `gap-6`). */
export const MASONRY_GAP_PX = 24
/** Initial height estimate (px) before ResizeObserver / DOM measurement settles. */
export const MASONRY_ESTIMATE_HEIGHT = 260

const FALLBACK_CONTENT_MAX = 1280
const FALLBACK_HORIZONTAL_PADDING = 64

export type VirtualItemKey = VirtualItem['key']

export interface UseVirtualMasonryOptions<T> {
  /** Column / row gap in pixels (default 24). */
  gap?: number
  /** Initial card height estimate before measurement (default 260). */
  estimateSize?: number
  /** Overscan items per lane outside the viewport (default 6). */
  overscan?: number
  /** Optional grid container element ref (auto-created when omitted). */
  containerRef?: Ref<HTMLElement | null>
  /** Optional sticky/top header element whose height shifts the masonry container. */
  headerRef?: Ref<HTMLElement | null>
  /** Stable key extractor so dynamic updates/deletions keep measured card heights. */
  getItemKey?: (item: T, index: number) => VirtualItemKey
}

function defaultItemKey<T>(item: T, index: number): VirtualItemKey {
  if (item && typeof item === 'object' && 'id' in item) {
    const id = (item as { id?: unknown }).id
    if (typeof id === 'number' || typeof id === 'string') return id
  }
  return index
}

/**
 * Window-scrolled virtual masonry backed by `@tanstack/vue-virtual` (`lanes` +
 * `measureElement` + `scrollMargin`). Replaces custom full-DOM masonry and
 * scroll-anchor hacks with O(viewport) DOM nodes and unrestricted reverse scroll.
 */
export function useVirtualMasonry<T>(
  items: Readonly<Ref<T[]>> | ComputedRef<T[]>,
  options: UseVirtualMasonryOptions<T> = {},
) {
  const gap = options.gap ?? MASONRY_GAP_PX
  const estimateSize = options.estimateSize ?? MASONRY_ESTIMATE_HEIGHT
  const overscan = options.overscan ?? 6
  const getKey = options.getItemKey ?? defaultItemKey

  const { width: windowWidth } = useWindowSize()
  const containerRef = options.containerRef ?? ref<HTMLElement | null>(null)
  const { width: containerWidth } = useElementSize(containerRef)

  const columnCount = computed(() => {
    if (windowWidth.value >= 1000) return 3
    if (windowWidth.value >= 500) return 2
    return 1
  })

  const effectiveWidth = computed(() => {
    if (containerWidth.value > 0) return containerWidth.value
    return Math.max(0, Math.min(windowWidth.value, FALLBACK_CONTENT_MAX) - FALLBACK_HORIZONTAL_PADDING)
  })

  const columnWidth = computed(() => {
    const count = columnCount.value
    const width = effectiveWidth.value
    if (!width || !count) return 0
    return Math.floor((width - gap * (count - 1)) / count)
  })

  const scrollMargin = ref(0)

  function updateScrollMargin() {
    const el = containerRef.value
    if (!el || typeof window === 'undefined') {
      scrollMargin.value = 0
      return
    }
    const nextMargin = Math.max(0, Math.round(el.getBoundingClientRect().top + window.scrollY))
    if (nextMargin !== scrollMargin.value) {
      scrollMargin.value = nextMargin
    }
  }

  useResizeObserver(containerRef, updateScrollMargin)
  if (options.headerRef) {
    useResizeObserver(options.headerRef, updateScrollMargin)
  }
  useEventListener(typeof window !== 'undefined' ? window : null, 'resize', updateScrollMargin)

  watch(
    containerRef,
    (el) => {
      if (el) void nextTick(updateScrollMargin)
    },
    { flush: 'post' },
  )

  const virtualizerOptions = computed(() => ({
    count: items.value.length,
    estimateSize: () => estimateSize,
    overscan,
    lanes: columnCount.value,
    gap,
    scrollMargin: scrollMargin.value,
    getItemKey: (index: number): VirtualItemKey => {
      const item = items.value[index]
      return item !== undefined ? getKey(item, index) : index
    },
  }))

  const virtualizer = useWindowVirtualizer<Element>(virtualizerOptions)
  // In a multi-lane masonry grid, default single-lane delta adjustments apply
  // `delta` once per card across all lanes (3x vertical drift) and can push
  // `window.scrollY` downward toward the LoadMore sentinel.
  virtualizer.value.shouldAdjustScrollPositionOnItemSizeChange = () => false

  function syncVirtualizerOptions() {
    const opts = virtualizerOptions.value
    const inst = virtualizer.value
    inst.shouldAdjustScrollPositionOnItemSizeChange = () => false
    if (
      inst.options.count !== opts.count ||
      inst.options.lanes !== opts.lanes ||
      inst.options.scrollMargin !== opts.scrollMargin
    ) {
      inst.setOptions({
        ...inst.options,
        ...opts,
      })
      inst._willUpdate()
    }
    return inst
  }

  const virtualItems = computed(() => syncVirtualizerOptions().getVirtualItems())
  const totalHeight = computed(() => syncVirtualizerOptions().getTotalSize())

  function measureElement(el: Element | ComponentPublicInstance | null) {
    if (el instanceof Element) {
      virtualizer.value.measureElement(el)
    }
  }

  function itemStyle(vItem: VirtualItem): CSSProperties {
    const width = columnWidth.value
    const left = vItem.lane * (width + gap)
    const top = vItem.start - virtualizer.value.options.scrollMargin
    return {
      position: 'absolute',
      top: 0,
      left: `${left}px`,
      width: width > 0 ? `${width}px` : '100%',
      transform: `translate3d(0, ${top}px, 0)`,
    }
  }

  function measure() {
    updateScrollMargin()
    virtualizer.value.measure()
  }

  const debouncedMeasure = useDebounceFn(measure, 100)

  watch(columnCount, () => {
    void nextTick(measure)
  })

  watch(columnWidth, (next, prev) => {
    if (next > 0 && prev > 0 && next !== prev) {
      void debouncedMeasure()
    }
  })

  return {
    containerRef,
    columnCount,
    columnWidth,
    scrollMargin,
    virtualItems,
    totalHeight,
    measureElement,
    itemStyle,
    measure,
    virtualizer,
  }
}
