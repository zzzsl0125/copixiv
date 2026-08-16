import { describe, it, expect, vi, beforeEach } from 'vitest'
import { defineComponent, reactive } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { useBatchMode } from '../../src/composables/useBatchMode'
import type { NovelFilters } from '../../src/types'

const novelApiMock = vi.hoisted(() => ({
  countNovels: vi.fn(),
  getNovelIds: vi.fn(),
  matchNovelIds: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  novelApi: novelApiMock,
}))

function mountUseBatchMode(filters: NovelFilters) {
  const wrapper = mount(
    defineComponent({
      setup() {
        const state = useBatchMode(reactive({ ...filters }))
        return { state }
      },
      template: '<div />',
    }),
  )
  return wrapper.vm.state as ReturnType<typeof useBatchMode>
}

function defaultFilters(): NovelFilters {
  return {
    keyword: '',
    order_by: 'random',
    order_direction: 'DESC',
    min_like: undefined,
    min_text: undefined,
  }
}

describe('useBatchMode', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    novelApiMock.countNovels.mockResolvedValue({ total: 10 })
    novelApiMock.getNovelIds.mockResolvedValue({
      ids: [1, 2, 3], total: 3, truncated: false,
    })
    novelApiMock.matchNovelIds.mockResolvedValue({
      matching_ids: [], truncated: false,
    })
  })

  it('starts inactive with no selection and no scope', () => {
    const state = mountUseBatchMode(defaultFilters())

    expect(state.isBatchMode.value).toBe(false)
    expect(state.selectedCount.value).toBe(0)
    expect(state.hasSelection.value).toBe(false)
    expect(state.scope.value).toBeNull()
  })

  it('enter() activates the mode and fetches the matched count', async () => {
    const state = mountUseBatchMode(defaultFilters())
    state.enter()
    await flushPromises()

    expect(state.isBatchMode.value).toBe(true)
    expect(novelApiMock.countNovels).toHaveBeenCalled()
    expect(state.matchedCount.value).toBe(10)
    expect(state.hasFilter.value).toBe(false)
  })

  it('card clicks toggle an id set and expose an ids scope', () => {
    const state = mountUseBatchMode(defaultFilters())
    state.enter()

    state.toggleCard(3)
    state.toggleCard(7)
    expect(state.selectedCount.value).toBe(2)
    expect(state.isCardSelected(3)).toBe(true)
    expect(state.isCardSelected(4)).toBe(false)
    expect(state.scope.value).toEqual({
      mode: 'ids',
      novel_ids: [3, 7],
      excluded_ids: [],
    })

    state.toggleCard(3)
    expect(state.isCardSelected(3)).toBe(false)
    expect(state.selectedCount.value).toBe(1)

    state.toggleCard(7)
    expect(state.hasSelection.value).toBe(false)
    expect(state.scope.value).toBeNull()
  })

  it('selection survives filter changes — search is only a picking surface', async () => {
    const filters = { ...defaultFilters(), keyword: 'A' }
    const wrapper = mount(
      defineComponent({
        setup() {
          const state = useBatchMode(reactive(filters))
          return { state }
        },
        template: '<div />',
      }),
    )
    const state = wrapper.vm.state as ReturnType<typeof useBatchMode>
    state.enter()
    state.toggleCard(11)
    state.toggleCard(22)
    expect(state.selectedCount.value).toBe(2)

    // Switch the search surface — the selection must stay intact.
    filters.keyword = 'B'
    await flushPromises()

    expect(state.selectedCount.value).toBe(2)
    expect(state.isCardSelected(11)).toBe(true)
    expect(state.isCardSelected(22)).toBe(true)
    expect(state.scope.value?.novel_ids).toEqual([11, 22])
    expect(novelApiMock.countNovels).toHaveBeenLastCalledWith(
      expect.objectContaining({ keyword: 'B' }),
    )
  })

  it('selectAllMatched unions the matched ids into the selection', async () => {
    const state = mountUseBatchMode({ ...defaultFilters(), keyword: 'R-18' })
    state.enter()
    state.toggleCard(99) // pre-existing pick from another search

    const result = await state.selectAllMatched()

    expect(novelApiMock.getNovelIds).toHaveBeenCalledWith(
      expect.objectContaining({ keyword: 'R-18' }),
    )
    expect(result).toEqual({ added: 3, total: 3, truncated: false })
    expect(state.selectedCount.value).toBe(4)
    expect(state.isCardSelected(99)).toBe(true)
    expect(state.isCardSelected(1)).toBe(true)
    expect(state.isCardSelected(3)).toBe(true)
  })

  it('selectAllMatched reports truncation without losing existing picks', async () => {
    novelApiMock.getNovelIds.mockResolvedValue({
      ids: [1, 2], total: 500, truncated: true,
    })
    const state = mountUseBatchMode(defaultFilters())
    state.enter()
    state.toggleCard(7)

    const result = await state.selectAllMatched()

    expect(result?.truncated).toBe(true)
    expect(result?.added).toBe(2)
    expect(state.isCardSelected(7)).toBe(true)
  })

  it('exit() clears the selection and deactivates the mode', () => {
    const state = mountUseBatchMode(defaultFilters())
    state.enter()
    state.toggleCard(1)
    state.exit()

    expect(state.isBatchMode.value).toBe(false)
    expect(state.hasSelection.value).toBe(false)
    expect(state.scope.value).toBeNull()
  })

  it('clearSelectionInScope removes only picks inside the current scope', async () => {
    novelApiMock.matchNovelIds.mockResolvedValue({
      matching_ids: [2, 3], truncated: false,
    })
    const state = mountUseBatchMode({ ...defaultFilters(), keyword: 'B' })
    state.enter()
    state.toggleCard(1) // from another search
    state.toggleCard(2) // inside B
    state.toggleCard(3) // inside B

    const result = await state.clearSelectionInScope()

    expect(novelApiMock.matchNovelIds).toHaveBeenCalledWith(
      expect.objectContaining({ keyword: 'B', novel_ids: [1, 2, 3] }),
    )
    expect(result).toEqual({ removed: 2, remaining: 1 })
    expect(state.isCardSelected(1)).toBe(true)
    expect(state.isCardSelected(2)).toBe(false)
    expect(state.isCardSelected(3)).toBe(false)
  })

  it('clearSelectionInScope without filters clears everything', async () => {
    const state = mountUseBatchMode(defaultFilters())
    state.enter()
    state.toggleCard(1)
    state.toggleCard(2)

    const result = await state.clearSelectionInScope()

    expect(novelApiMock.matchNovelIds).not.toHaveBeenCalled()
    expect(result).toEqual({ removed: 2, remaining: 0 })
    expect(state.hasSelection.value).toBe(false)
  })

  it('clearSelectionInScope with empty selection is a no-op', async () => {
    const state = mountUseBatchMode(defaultFilters())
    state.enter()

    expect(await state.clearSelectionInScope()).toBeNull()
    expect(novelApiMock.matchNovelIds).not.toHaveBeenCalled()
  })
})
