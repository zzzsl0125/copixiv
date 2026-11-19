import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { focusManager, onlineManager } from '@tanstack/vue-query'
import type { Novel, NovelListResult } from '../../types'
import { SECTIONS } from '../../config'
import { flushPromises, mountWithQuery } from '../../test-utils/queryWrapper'

const mocks = vi.hoisted(() => ({
  getNovels: vi.fn<() => Promise<NovelListResult>>(),
}))

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    novelApi: {
      ...actual.novelApi,
      getNovels: mocks.getNovels,
    },
  }
})

vi.mock('../useSystem', () => ({
  useSystem: () => ({
    systemConfig: ref({
      exclude_blocked_tag_novels: true,
      batch_download_naming: '',
    }),
    isConfigReady: ref(true),
  }),
}))

import { buildNovelQuery, computeSearchFilters, parseNovelQuery, useNovels } from '../index'

function makeNovel(id: number, overrides: Partial<Novel> = {}): Novel {
  return {
    id,
    title: `Novel ${id}`,
    author_id: 100 + Math.abs(id),
    author_name: `Author ${id}`,
    series_id: null,
    series_name: null,
    series_index: null,
    like: 600,
    view: 1000,
    text: 4000,
    has_epub: 0,
    create_time: null,
    source: 'pixiv',
    is_favourite: 0,
    is_special_follow: 0,
    tags: [],
    editable_fields: [],
    ...overrides,
  }
}

describe('useNovelQueryState & useNovels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('canonicalizes route query parameters and ignores invalid enums', () => {
    expect(buildNovelQuery(SECTIONS.novels)).toEqual({})
    expect(buildNovelQuery(SECTIONS.favourites)).toEqual({ keyword: 'is_favourite:true;' })
    expect(
      buildNovelQuery({
        ...SECTIONS.novels,
        min_like: 0,
        order_by: 'like',
      }),
    ).toEqual({
      order_by: 'like',
      min_like: '0',
    })

    const parsedInvalid = parseNovelQuery({
      order_by: 'bogus_col',
      order_direction: 'SIDEWAYS',
      min_like: '-5',
    })
    expect(parsedInvalid).toEqual({
      keyword: '',
      order_by: 'random',
      order_direction: 'DESC',
      min_like: 500,
      min_text: 3000,
    })

    const parsedOverride = parseNovelQuery({
      keyword: 'fantasy',
      order_by: 'like',
      min_like: '0',
      min_text: '0',
    })
    expect(parsedOverride).toEqual({
      keyword: 'fantasy',
      order_by: 'like',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    })
  })

  it('computes search ordering across shelf, id, series, author, and free search', () => {
    expect(computeSearchFilters('source:local;')).toEqual({
      keyword: 'source:local;',
      order_by: 'id',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    })
    expect(computeSearchFilters('series_id:42;')).toMatchObject({
      order_by: 'id',
      order_direction: 'ASC',
      min_like: 0,
      min_text: 0,
    })
    expect(computeSearchFilters('1234567')).toMatchObject({
      order_by: 'id',
      order_direction: 'DESC',
    })
    expect(computeSearchFilters('sci-fi')).toMatchObject({
      order_by: 'like',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    })
  })

  it('keeps order_by=random staleTime=Infinity and never refetches on focus or reconnect (§7.5)', async () => {
    mocks.getNovels
      .mockResolvedValueOnce({
        novels: [makeNovel(1)],
        cursor: { seed: 'abc', h: 'def' },
        has_excluded: false,
      })
      .mockResolvedValueOnce({
        novels: [makeNovel(2)],
        cursor: null,
        has_excluded: false,
      })

    const { result, queryClient, wrapper } = await mountWithQuery(() => useNovels())
    await flushPromises()

    expect(mocks.getNovels).toHaveBeenCalledTimes(1)
    expect(result.novels.value.map((n) => n.id)).toEqual([1])
    expect(result.noMoreData.value).toBe(false)

    // Verify observer options on random browse query
    const queries = queryClient.getQueryCache().findAll({ queryKey: ['novels', 'list'] })
    expect(queries).toHaveLength(1)
    const obsOpts = queries[0]!.observers[0]!.options
    expect(obsOpts.staleTime).toBe(Infinity)
    expect(obsOpts.refetchOnWindowFocus).toBe(false)
    expect(obsOpts.refetchOnReconnect).toBe(false)

    // Window focus & online reconnect events must NOT re-trigger random fetch
    focusManager.setFocused(false)
    focusManager.setFocused(true)
    onlineManager.setOnline(false)
    onlineManager.setOnline(true)
    await flushPromises()
    expect(mocks.getNovels).toHaveBeenCalledTimes(1)

    // Cursor load-more appends next page and reaches noMoreData
    await result.handleLoadMore()
    await flushPromises()
    expect(mocks.getNovels).toHaveBeenCalledTimes(2)
    expect(result.novels.value.map((n) => n.id)).toEqual([1, 2])
    expect(result.noMoreData.value).toBe(true)

    // In-place cache patch/state/remove update active view without refetching
    result.patchNovelInCache(makeNovel(1, { title: 'Updated One' }))
    result.patchNovelStateInCache({ id: 2, field: 'is_favourite', value: 1 })
    expect(result.novels.value[0]?.title).toBe('Updated One')
    expect(result.novels.value[1]?.is_favourite).toBe(1)

    result.removeNovelFromCache(1)
    expect(result.novels.value.map((n) => n.id)).toEqual([2])
    expect(mocks.getNovels).toHaveBeenCalledTimes(2)

    wrapper.unmount()
  })
})
