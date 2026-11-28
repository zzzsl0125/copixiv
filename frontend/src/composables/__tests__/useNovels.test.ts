import { beforeEach, describe, expect, it, vi } from 'vitest'
import { focusManager, onlineManager, QueryClient } from '@tanstack/vue-query'
import type { Novel, NovelListResult } from '../../types'
import type { SourceShelfDef } from '../../types/sources'
import { excludeBlockedOverride, SECTIONS } from '../../config'
import { flushPromises, mountWithQuery } from '../../test-utils/queryWrapper'

const mocks = vi.hoisted(() => ({
  getNovels: vi.fn<() => Promise<NovelListResult>>(),
  listSources: vi.fn(),
}))

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    novelApi: {
      ...actual.novelApi,
      getNovels: mocks.getNovels,
    },
    sourceApi: {
      ...actual.sourceApi,
      list: mocks.listSources,
    },
  }
})

import { buildNovelQuery, computeSearchFilters, parseNovelQuery, useNovels } from '../index'
import { queryKeys } from '../../api'

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
    can_sync: true,
    external_url: `https://www.pixiv.net/novel/show.php?id=${id}`,
    is_favourite: false,
    is_special_follow: false,
    is_series: false,
    chapter_count: 1,
    tags: [],
    editable_fields: [],
    ...overrides,
  }
}

describe('useNovelQueryState & useNovels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.listSources.mockResolvedValue([])
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

    // Non-empty keyword defaults align between parseNovelQuery and computeSearchFilters
    expect(buildNovelQuery(computeSearchFilters('百合'))).toEqual({ keyword: '百合' })
    expect(parseNovelQuery({ keyword: '百合' })).toEqual(computeSearchFilters('百合'))
  })

  it('computes search ordering across shelf, id, series, author, source, and free search', () => {
    expect(computeSearchFilters('source:local;')).toEqual({
      keyword: 'source:local;',
      order_by: 'id',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    })
    expect(computeSearchFilters('source:pixiv;')).toEqual({
      keyword: 'source:pixiv;',
      order_by: 'id',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    })
    expect(computeSearchFilters('source:esj;')).toEqual({
      keyword: 'source:esj;',
      order_by: 'id',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    })
    expect(computeSearchFilters('source:esj; fantasy')).toMatchObject({
      order_by: 'like',
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
    expect(computeSearchFilters('-1234567')).toMatchObject({
      order_by: 'id',
      order_direction: 'DESC',
    })
    expect(computeSearchFilters('sci-fi')).toMatchObject({
      order_by: 'like',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    })

    const customShelf: SourceShelfDef = {
      section_id: 'custom',
      label: 'Custom',
      keyword: 'custom_tag:vip;',
      min_like: 100,
      min_text: 500,
      order_by: 'create_time',
      order_direction: 'ASC',
      icon: 'book',
      intake_action: 'none',
      intake_placeholder: '',
      intake_button_text: '',
    }
    expect(computeSearchFilters('custom_tag:vip;', [customShelf])).toEqual({
      keyword: 'custom_tag:vip;',
      order_by: 'create_time',
      order_direction: 'ASC',
      min_like: 100,
      min_text: 500,
    })

    expect(excludeBlockedOverride('')).toBeUndefined()
    expect(excludeBlockedOverride('novels')).toBeUndefined()
    expect(excludeBlockedOverride('source:pixiv;')).toBe(false)
    expect(excludeBlockedOverride('source:esj;')).toBe(false)
    expect(excludeBlockedOverride('source:local;')).toBe(false)
    expect(excludeBlockedOverride('source:esj; fantasy')).toBeUndefined()
    expect(excludeBlockedOverride('custom_tag:vip;', [customShelf])).toBe(false)
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

    const invalidateSpy = vi.spyOn(QueryClient.prototype, 'invalidateQueries')
    const { result, queryClient, wrapper } = await mountWithQuery(() => useNovels())
    await flushPromises()

    expect(mocks.getNovels).toHaveBeenCalledTimes(1)
    expect(mocks.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({
        keyword: '',
        exclude_blocked: undefined,
      }),
      expect.any(AbortSignal),
    )
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: queryKeys.searchHistory() })
    invalidateSpy.mockRestore()
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
    result.patchNovelStateInCache({ id: 2, field: 'is_favourite', value: true })
    expect(result.novels.value[0]?.title).toBe('Updated One')
    expect(result.novels.value[1]?.is_favourite).toBe(true)

    result.removeNovelFromCache(1)
    expect(result.novels.value.map((n) => n.id)).toEqual([2])
    expect(mocks.getNovels).toHaveBeenCalledTimes(2)

    wrapper.unmount()
  })

  it('sends free search keyword across all sources and shelf keyword with exclude_blocked=false', async () => {
    const invalidateSpy = vi.spyOn(QueryClient.prototype, 'invalidateQueries')
    mocks.getNovels.mockResolvedValue({
      novels: [makeNovel(5)],
      cursor: null,
      has_excluded: false,
    })

    const searchMount = await mountWithQuery(() => useNovels(), {
      initialRoute: '/?keyword=fantasy',
    })
    await flushPromises()

    expect(mocks.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({
        keyword: 'fantasy',
        order_by: 'like',
        order_direction: 'DESC',
        min_like: 0,
        min_text: 0,
        exclude_blocked: undefined,
      }),
      expect.any(AbortSignal),
    )
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.searchHistory() })
    invalidateSpy.mockRestore()
    searchMount.wrapper.unmount()

    vi.clearAllMocks()
    mocks.listSources.mockResolvedValue([])
    mocks.getNovels.mockResolvedValue({
      novels: [makeNovel(-1, { source: 'esj' })],
      cursor: null,
      has_excluded: false,
    })

    const shelfMount = await mountWithQuery(() => useNovels(), {
      initialRoute: '/?keyword=source%3Aesj%3B',
    })
    await flushPromises()

    expect(mocks.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({
        keyword: 'source:esj;',
        order_by: 'id',
        order_direction: 'DESC',
        min_like: 0,
        min_text: 0,
        exclude_blocked: false,
      }),
      expect.any(AbortSignal),
    )
    shelfMount.wrapper.unmount()
  })

  it('passes only_blocked=true when onlyBlocked option is enabled', async () => {
    mocks.getNovels.mockResolvedValueOnce({
      novels: [makeNovel(10)],
      cursor: null,
      has_excluded: false,
    })

    const { result, wrapper } = await mountWithQuery(() => useNovels({ onlyBlocked: true }))
    await flushPromises()

    expect(mocks.getNovels).toHaveBeenCalledWith(
      expect.objectContaining({
        only_blocked: true,
        exclude_blocked: undefined,
      }),
      expect.any(AbortSignal),
    )
    expect(result.novels.value.map((n) => n.id)).toEqual([10])

    wrapper.unmount()
  })
})
