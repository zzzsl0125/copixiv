import { defineComponent, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BatchScope, Novel, NovelFilters } from '../../types'

const apiMocks = vi.hoisted(() => ({
  sortNovelIds: vi.fn(),
  getNovelsByIds: vi.fn(),
}))

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    novelApi: {
      ...actual.novelApi,
      sortNovelIds: apiMocks.sortNovelIds,
      getNovelsByIds: apiMocks.getNovelsByIds,
    },
  }
})

import { useCollectionViews } from '../useCollectionViews'
import { usePagedNovelIdView } from '../usePagedNovelIdView'

function makeNovel(id: number): Novel {
  return {
    id,
    title: `Novel ${id}`,
    author_id: 1,
    author_name: 'Author',
    series_id: null,
    series_name: null,
    series_index: null,
    like: 100,
    view: 200,
    text: 1000,
    has_epub: 0,
    create_time: null,
    source: 'pixiv',
    is_favourite: false,
    is_special_follow: false,
    tags: [],
    editable_fields: [],
  }
}

describe('useCollectionViews & usePagedNovelIdView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('scrollTo', vi.fn())
  })

  it('keeps viewLoading true while sortNovelIds is pending and guards stale sort responses with seq', async () => {
    let resolveFirstSort!: (val: { ids: number[]; total: number }) => void
    let resolveSecondSort!: (val: { ids: number[]; total: number }) => void

    apiMocks.sortNovelIds
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirstSort = resolve
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecondSort = resolve
          }),
      )

    apiMocks.getNovelsByIds.mockResolvedValue({ novels: [makeNovel(2), makeNovel(1)] })

    const filters = ref<NovelFilters>({
      keyword: '',
      order_by: 'like',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    })
    const batchScope = ref<BatchScope | null>({
      mode: 'ids',
      novel_ids: [1, 2],
      excluded_ids: [],
    })
    const batchMode = ref(true)
    const hasSelection = ref(true)
    const novels = ref<Novel[]>([])

    let views!: ReturnType<typeof useCollectionViews>
    const Host = defineComponent({
      setup() {
        views = useCollectionViews({
          filters,
          batchScope,
          batchMode,
          hasSelection,
          novels,
          onActiveChange: vi.fn(),
          afterLayout: vi.fn(),
        })
        return {}
      },
      template: '<div />',
    })
    const wrapper = mount(Host)

    const togglePromise = views.toggleViewSelected()
    await nextTick()

    // While sortNovelIds is in-flight, viewLoading must be true (prevents EmptyState / 0/0 flash)
    expect(views.viewingSelected.value).toBe(true)
    expect(views.viewLoading.value).toBe(true)

    // Trigger a second sort before the first resolves
    filters.value = { ...filters.value, order_direction: 'ASC' }
    await nextTick()

    // Resolve the second (newer) sort first, then the stale first sort
    resolveSecondSort({ ids: [2, 1], total: 2 })
    await Promise.resolve()
    await Promise.resolve()

    resolveFirstSort({ ids: [1, 2], total: 2 })
    await togglePromise
    await nextTick()

    expect(apiMocks.getNovelsByIds).toHaveBeenCalledTimes(1)
    expect(apiMocks.getNovelsByIds).toHaveBeenCalledWith([2, 1])
    expect(views.viewLoading.value).toBe(false)
    expect(views.displayNovels.value.map((n) => n.id)).toEqual([2, 1])

    wrapper.unmount()
  })

  it('discards stale getNovelsByIds response in usePagedNovelIdView when reset/restarted', async () => {
    let resolveFirst!: (val: { novels: Novel[] }) => void
    let resolveSecond!: (val: { novels: Novel[] }) => void

    apiMocks.getNovelsByIds
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecond = resolve
          }),
      )

    const view = usePagedNovelIdView()
    const firstStart = view.start([1])
    const secondStart = view.start([2])

    resolveSecond({ novels: [makeNovel(2)] })
    await secondStart

    resolveFirst({ novels: [makeNovel(1)] })
    await firstStart

    expect(view.novels.value.map((n) => n.id)).toEqual([2])
  })
})
