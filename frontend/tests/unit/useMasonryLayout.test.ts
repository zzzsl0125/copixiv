import { describe, it, expect, vi, afterEach } from 'vitest'
import { defineComponent, ref, nextTick } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { useMasonryLayout } from '../../src/composables/useMasonryLayout'

function setInnerWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true })
}

function mountMasonry<T>(items: T[], heights: number[] = []) {
  const itemsRef = ref(items)
  const columnRefs = ref(
    heights.map((h) => ({ offsetHeight: h })) as HTMLElement[],
  )
  const wrapper = mount(
    defineComponent({
      setup() {
        const state = useMasonryLayout(itemsRef, columnRefs)
        return { state, itemsRef, columnRefs }
      },
      template: '<div />',
    }),
  )
  // Return the local ref (vm auto-unwraps top-level refs, losing .value).
  return {
    wrapper,
    itemsRef,
    state: wrapper.vm.state as ReturnType<typeof useMasonryLayout<unknown>>,
  }
}

describe('useMasonryLayout', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('derives column counts from window width', () => {
    setInnerWidth(1200)
    expect(mountMasonry([]).state.columnCount.value).toBe(3)

    setInnerWidth(800)
    expect(mountMasonry([]).state.columnCount.value).toBe(2)

    setInnerWidth(400)
    expect(mountMasonry([]).state.columnCount.value).toBe(1)
  })

  it('responds to window resize events', async () => {
    setInnerWidth(1200)
    const { state } = mountMasonry([])
    expect(state.columnCount.value).toBe(3)

    setInnerWidth(400)
    window.dispatchEvent(new Event('resize'))
    await nextTick()

    expect(state.windowWidth.value).toBe(400)
    expect(state.columnCount.value).toBe(1)
  })

  it('distributes items into the shortest column by measured height', async () => {
    setInnerWidth(1200)
    // Static heights: column 2 starts shortest, so the first item lands
    // there; heights never change (fake elements), so the rest follow.
    const { state } = mountMasonry([1, 2, 3], [100, 200, 10])
    await flushPromises()

    const total = state.columns.value.reduce((n, col) => n + col.length, 0)
    expect(total).toBe(3)
    expect(state.columns.value[2]).toContain(1)
  })

  it('appends new items without rebuilding columns on load-more', async () => {
    setInnerWidth(1200)
    const { state, itemsRef } = mountMasonry([1, 2], [0, 0])
    await flushPromises()
    const firstColumn = state.columns.value[0]
    expect(firstColumn).toEqual([1, 2])

    itemsRef.value.push(3, 4)
    await flushPromises()

    // Same array identity per column, extra items appended.
    expect(state.columns.value[0]).toBe(firstColumn)
    expect(state.columns.value[0]).toEqual([1, 2, 3, 4])
  })

  it('replacing the items array relayouts without duplicating items', async () => {
    setInnerWidth(1200)
    const { state, itemsRef } = mountMasonry([1, 2, 3], [0, 0, 0])
    await flushPromises()

    // Simulate a full list replacement (initial load / search / view swap)
    // — historically two concurrent layout loops each pushed all items,
    // rendering every novel twice.
    itemsRef.value = [4, 5, 6]
    await flushPromises()
    state.relayout()
    await flushPromises()

    const flattened = state.columns.value.flat()
    expect(flattened).toHaveLength(3)
    expect(new Set(flattened).size).toBe(3)
    expect([...flattened].sort()).toEqual([4, 5, 6])
  })

  it('relayout() forces a rebuild even when the length stays equal', async () => {
    setInnerWidth(1200)
    const { state, itemsRef } = mountMasonry([1, 2, 3], [0, 0, 0])
    await flushPromises()

    // Same-length replacement — the load-more heuristic would misread it
    // as an append; an explicit forced relayout must rebuild from scratch.
    itemsRef.value = [7, 8, 9]
    await flushPromises()
    state.relayout()
    await flushPromises()

    const flattened = state.columns.value.flat()
    expect(flattened).toHaveLength(3)
    expect([...flattened].sort()).toEqual([7, 8, 9])
  })
})
