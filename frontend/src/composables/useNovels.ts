import { computed, toValue, type MaybeRefOrGetter } from 'vue'
import { type InfiniteData, useInfiniteQuery, useQueryClient } from '@tanstack/vue-query'
import { getApiErrorMessage, novelApi, queryKeys } from '../api'
import { DEFAULT_PAGE_SIZE, excludeBlockedOverride } from '../config'
import type { Novel, NovelFilters, NovelListResult } from '../types'
import { useNovelQueryState } from './useNovelQueryState'
import { useNovelShellState } from './useNovelShellState'
import { useSources } from './useSources'

export function useNovels(options?: { onlyBlocked?: MaybeRefOrGetter<boolean> }) {
  const queryClient = useQueryClient()
  const { filters, activeSection, navigateToSection, navigateSearch, navigateCardSearch } =
    useNovelQueryState()
  const { refreshNonce } = useNovelShellState()
  const { shelves } = useSources()

  const onlyBlocked = computed(() => Boolean(toValue(options?.onlyBlocked)))
  const extraShelves = computed(() => shelves.value.map((entry) => entry.shelf))
  const effectiveKeyword = computed(() => filters.value.keyword.trim())
  const isSeriesDrilldown = computed(() =>
    /(?:^|[;\s])series_id\s*:/i.test(filters.value.keyword),
  )

  const listParams = computed(() => ({
    keyword: effectiveKeyword.value,
    order_by: filters.value.order_by,
    order_direction: filters.value.order_direction,
    min_like: filters.value.min_like,
    min_text: filters.value.min_text,
    exclude_blocked: onlyBlocked.value
      ? undefined
      : excludeBlockedOverride(filters.value.keyword, extraShelves.value),
    only_blocked: onlyBlocked.value || undefined,
  }))

  const query = useInfiniteQuery({
    queryKey: computed(() => queryKeys.novelsList(listParams.value, refreshNonce.value)),
    queryFn: async ({ pageParam, signal }) => {
      const hasUserKeyword = Boolean(filters.value.keyword.trim())
      const result = await novelApi.getNovels(
        {
          ...listParams.value,
          cursor: pageParam,
          per_page: DEFAULT_PAGE_SIZE,
        },
        signal,
      )
      if (!pageParam && hasUserKeyword) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.searchHistory() })
      }
      return result
    },
    initialPageParam: undefined as Record<string, unknown> | undefined,
    getNextPageParam: (lastPage) =>
      (lastPage.cursor as Record<string, unknown> | null | undefined) ?? undefined,
    // §7.5: random browse must NEVER auto-refetch on tab focus or reconnect.
    staleTime: computed(() => (filters.value.order_by === 'random' ? Infinity : 30_000)),
    refetchOnWindowFocus: false,
    refetchOnReconnect: computed(() => filters.value.order_by !== 'random'),
  })

  const novels = computed<Novel[]>(
    () => query.data.value?.pages.flatMap((page) => page.novels || []) ?? [],
  )
  const hasExcluded = computed(() => query.data.value?.pages[0]?.has_excluded ?? false)
  const loading = computed(() => query.isPending.value || query.isFetchingNextPage.value)
  const noMoreData = computed(() => query.status.value === 'success' && !query.hasNextPage.value)
  const error = computed(() =>
    query.error.value
      ? getApiErrorMessage(query.error.value, '加载失败，请检查网络或后端状态')
      : null,
  )

  const handleLoadMore = async () => {
    if (!query.hasNextPage.value || query.isFetchingNextPage.value || query.isPending.value) return
    await query.fetchNextPage()
  }

  const invalidateNovels = () => queryClient.invalidateQueries({ queryKey: queryKeys.novelsAll() })

  function matchesNovelCard(n: Novel, target: Novel): boolean {
    if (n.id === target.id) return true
    return Boolean(
      target.is_series &&
        target.series_id != null &&
        n.is_series &&
        n.series_id === target.series_id,
    )
  }

  function patchNovelInCache(updated: Novel) {
    queryClient.setQueriesData<InfiniteData<NovelListResult>>(
      { queryKey: ['novels', 'list'] },
      (old) => {
        if (!old) return old
        return {
          ...old,
          pages: old.pages.map((page) => ({
            ...page,
            novels: page.novels.map((n) => (matchesNovelCard(n, updated) ? updated : n)),
          })),
        }
      },
    )
  }

  function patchNovelStateInCache(
    payload: {
      id: number
      field: 'is_favourite' | 'is_special_follow'
      value: boolean
      seriesId?: number | null
    },
    authorId?: number,
  ) {
    queryClient.setQueriesData<InfiniteData<NovelListResult>>(
      { queryKey: ['novels', 'list'] },
      (old) => {
        if (!old) return old
        return {
          ...old,
          pages: old.pages.map((page) => ({
            ...page,
            novels: page.novels.map((n) => {
              if (payload.field === 'is_special_follow' && authorId !== undefined) {
                return n.author_id === authorId ? { ...n, is_special_follow: payload.value } : n
              }
              if (payload.field === 'is_favourite' && payload.seriesId != null) {
                return n.id === payload.id || (n.is_series && n.series_id === payload.seriesId)
                  ? { ...n, is_favourite: payload.value }
                  : n
              }
              return n.id === payload.id ? { ...n, [payload.field]: payload.value } : n
            }),
          })),
        }
      },
    )
  }

  function removeNovelFromCache(novelId: number, seriesId?: number | null) {
    queryClient.setQueriesData<InfiniteData<NovelListResult>>(
      { queryKey: ['novels', 'list'] },
      (old) => {
        if (!old) return old
        return {
          ...old,
          pages: old.pages.map((page) => ({
            ...page,
            novels: page.novels.filter(
              (n) => n.id !== novelId && !(seriesId != null && n.is_series && n.series_id === seriesId),
            ),
          })),
        }
      },
    )
  }

  const handleSearch = (keyword?: string) => navigateSearch(keyword ?? filters.value.keyword)

  const resetFilters = () => navigateToSection('novels')

  return {
    novels,
    loading,
    error,
    noMoreData,
    filters,
    activeSection,
    hasExcluded,
    isSeriesDrilldown,
    handleSearch,
    handleLoadMore,
    handleCardSearch: navigateCardSearch,
    resetFilters,
    invalidateNovels,
    patchNovelInCache,
    patchNovelStateInCache,
    removeNovelFromCache,
  }
}

export type { NovelFilters }
