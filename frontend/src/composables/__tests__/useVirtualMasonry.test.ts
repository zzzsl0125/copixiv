import { defineComponent, h, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MASONRY_GAP_PX, useVirtualMasonry } from '../useVirtualMasonry'

function setWindowSize(width: number, height = 800) {
  Object.defineProperty(window, 'innerWidth', {
    configurable: true,
    writable: true,
    value: width,
  })
  Object.defineProperty(window, 'innerHeight', {
    configurable: true,
    writable: true,
    value: height,
  })
  window.dispatchEvent(new Event('resize'))
}

describe('useVirtualMasonry (@tanstack/vue-virtual)', () => {
  beforeEach(() => {
    setWindowSize(1200, 800)
    Object.defineProperty(window, 'scrollY', {
      configurable: true,
      writable: true,
      value: 0,
    })
  })

  it('computes responsive lane counts, column widths, and bounded virtual window over 1200 items', async () => {
    const items = ref(Array.from({ length: 1200 }, (_, i) => ({ id: i + 100 })))
    let api!: ReturnType<typeof useVirtualMasonry<{ id: number }>>

    const Host = defineComponent({
      setup() {
        api = useVirtualMasonry(items, {
          estimateSize: 200,
          overscan: 4,
          getItemKey: (item) => item.id,
        })
        return () =>
          h(
            'div',
            { ref: api.containerRef },
            api.virtualItems.value.map((vItem) =>
              h('div', {
                key: String(vItem.key),
                'data-index': vItem.index,
                ref: api.measureElement,
                style: api.itemStyle(vItem),
              }),
            ),
          )
      },
    })

    const wrapper = mount(Host)
    await nextTick()

    // 1200px viewport -> 3 lanes; fallback width = 1200 - 64 = 1136; (1136 - 48) / 3 = 362
    expect(api.columnCount.value).toBe(3)
    expect(api.columnWidth.value).toBe(Math.floor((1136 - MASONRY_GAP_PX * 2) / 3))

    // Unbounded corpus (>1000 items) renders only a bounded viewport slice
    const rendered = api.virtualItems.value
    expect(rendered.length).toBeGreaterThan(0)
    expect(rendered.length).toBeLessThan(100)
    // Keys come from novel.id rather than positional index
    expect(rendered[0]?.key).toBe(100)
    // Total virtual height spans 400 rows of 3-column masonry
    expect(api.totalHeight.value).toBeGreaterThan(80_000)

    // Lane 1 item shifts left by columnWidth + gap
    const laneOne = rendered.find((v) => v.lane === 1)
    expect(laneOne).toBeDefined()
    expect(api.itemStyle(laneOne!)).toMatchObject({
      position: 'absolute',
      top: 0,
      left: `${api.columnWidth.value + MASONRY_GAP_PX}px`,
      width: `${api.columnWidth.value}px`,
    })

    // Shrink viewport across breakpoints -> 2 lanes (700px) -> 1 lane (400px)
    setWindowSize(700, 800)
    await nextTick()
    expect(api.columnCount.value).toBe(2)

    setWindowSize(400, 800)
    await nextTick()
    expect(api.columnCount.value).toBe(1)

    wrapper.unmount()
  })

  it('tracks header scrollMargin in itemStyle and forwards Element refs to measureElement', async () => {
    const items = ref([{ id: 42 }])
    let api!: ReturnType<typeof useVirtualMasonry<{ id: number }>>

    const Host = defineComponent({
      setup() {
        api = useVirtualMasonry(items, { estimateSize: 200 })
        return () => h('div', { ref: api.containerRef })
      },
    })

    const wrapper = mount(Host)
    await nextTick()

    const containerEl = api.containerRef.value!
    containerEl.getBoundingClientRect = () =>
      ({
        top: 120,
        bottom: 500,
        left: 0,
        right: 800,
        width: 800,
        height: 380,
        x: 0,
        y: 120,
        toJSON: () => ({}),
      }) as DOMRect

    api.measure()
    await nextTick()

    expect(api.scrollMargin.value).toBe(120)
    const firstItem = api.virtualItems.value[0]!
    const style = api.itemStyle(firstItem)
    expect(style.transform).toBe(`translate3d(0, ${firstItem.start - 120}px, 0)`)

    // Virtualizer disables automatic scroll adjustment on item size changes and
    // updates totalHeight synchronously in the same render pass when items grow.
    const shouldAdjust = api.virtualizer.value.shouldAdjustScrollPositionOnItemSizeChange
    expect(shouldAdjust?.(firstItem, 80, api.virtualizer.value)).toBe(false)

    const initialHeight = api.totalHeight.value
    items.value = Array.from({ length: 30 }, (_, i) => ({ id: i + 1 }))
    expect(api.totalHeight.value).toBeGreaterThan(initialHeight)

    const measureSpy = vi.spyOn(api.virtualizer.value, 'measureElement')
    const cardEl = document.createElement('div')
    cardEl.dataset.index = '0'
    api.measureElement(null)
    expect(measureSpy).not.toHaveBeenCalled()
    api.measureElement(cardEl)
    expect(measureSpy).toHaveBeenCalledWith(cardEl)

    measureSpy.mockRestore()
    wrapper.unmount()
  })

  it('uses measured lane assignment to pack subsequent cards into the shortest lane', async () => {
    const items = ref(Array.from({ length: 5 }, (_, i) => ({ id: i + 1 })))
    let api!: ReturnType<typeof useVirtualMasonry<{ id: number }>>

    const Host = defineComponent({
      setup() {
        api = useVirtualMasonry(items, {
          estimateSize: 200,
          overscan: 4,
          getItemKey: (item) => item.id,
        })
        return () => h('div', { ref: api.containerRef })
      },
    })

    const wrapper = mount(Host)
    await nextTick()

    expect(api.columnCount.value).toBe(3)
    expect(api.virtualizer.value.options.laneAssignmentMode).toBe('measured')

    // Measure first row: lane 0 is much taller than lanes 1 and 2
    api.virtualizer.value.resizeItem(0, 700)
    api.virtualizer.value.resizeItem(1, 200)
    api.virtualizer.value.resizeItem(2, 200)
    api.virtualizer.value.resizeItem(3, 200)
    api.virtualizer.value.resizeItem(4, 200)
    await nextTick()

    const byIndex = new Map(api.virtualItems.value.map((v) => [v.index, v]))
    expect(byIndex.get(0)?.lane).toBe(0)
    expect(byIndex.get(1)?.lane).toBe(1)
    expect(byIndex.get(2)?.lane).toBe(2)
    // Both item 3 and item 4 must pack into shorter lanes 1 and 2 instead of round-robin lane 0
    expect(byIndex.get(3)?.lane).toBe(1)
    expect(byIndex.get(4)?.lane).toBe(2)

    wrapper.unmount()
  })

  it('supports item-aware estimateSize callback and clears lane cache only when prefix changes or items shrink', async () => {
    const items = ref([
      { id: 1, weight: 3 },
      { id: 2, weight: 1 },
      { id: 3, weight: 1 },
      { id: 4, weight: 1 },
    ])
    const estimator = vi.fn((item: { id: number; weight: number }, _index: number, colW: number) => {
      expect(colW).toBeGreaterThan(0)
      return item.weight * 200
    })
    let api!: ReturnType<typeof useVirtualMasonry<{ id: number; weight: number }>>

    const Host = defineComponent({
      setup() {
        api = useVirtualMasonry(items, {
          estimateSize: estimator,
          getItemKey: (item) => item.id,
        })
        return () => h('div', { ref: api.containerRef })
      },
    })

    const wrapper = mount(Host)
    await nextTick()

    expect(estimator).toHaveBeenCalled()
    // Item 0 estimated at 600px, items 1 and 2 at 200px, so item 3 lands in shortest lane (lane 1)
    const initialByIdx = new Map(api.virtualItems.value.map((v) => [v.index, v]))
    expect(initialByIdx.get(0)?.size).toBe(600)
    expect(initialByIdx.get(3)?.lane).toBe(1)

    const measureSpy = vi.spyOn(api.virtualizer.value, 'measure')

    // Pagination append preserves existing prefix keys -> no full re-measure
    items.value = [...items.value, { id: 5, weight: 1 }]
    await nextTick()
    expect(measureSpy).not.toHaveBeenCalled()

    // In-place patch with identical keys -> no full re-measure
    items.value = items.value.map((item) => ({ ...item, weight: item.weight + 1 }))
    await nextTick()
    expect(measureSpy).not.toHaveBeenCalled()

    // Replacing the list (e.g. new search / filter) -> calls measure() to drop stale laneAssignments
    items.value = Array.from({ length: 5 }, (_, i) => ({ id: i + 100, weight: 1 }))
    await nextTick()
    expect(measureSpy).toHaveBeenCalledTimes(1)

    // Shrinking / deleting an item -> calls measure()
    items.value = items.value.slice(1)
    await nextTick()
    expect(measureSpy).toHaveBeenCalledTimes(2)

    measureSpy.mockRestore()
    wrapper.unmount()
  })
})
