import { computed } from 'vue'
import { type InfiniteData, useInfiniteQuery, useQueryClient } from '@tanstack/vue-query'
import { getApiErrorMessage, novelApi, queryKeys } from '../api'
import { DEFAULT_PAGE_SIZE, SECTIONS, shouldExcludeBlocked } from '../config'
import type { Novel, NovelFilters, NovelListResult } from '../types'
import { useNovelQueryState } from './useNovelQueryState'
import { useNovelShellState } from './useNovelShellState'
import { useSystem } from './useSystem'

export function useNovels() {
  const queryClient = useQueryClient()
  const { filters, activeSection, navigateToSection, navigateSearch, navigateCardSearch } =
    useNovelQueryState()
  const { refreshNonce } = useNovelShellState()
  const { systemConfig, isConfigReady } = useSystem()

  const excludeBlocked = () => shouldExcludeBlocked(filters.value.keyword, systemConfig.value)

  const listParams = computed(() => ({
    keyword: filters.value.keyword.trim() || undefined,
    order_by: filters.value.order_by,
    order_direction: filters.value.order_direction,
    min_like: filters.value.min_like,
    min_text: filters.value.min_text,
    exclude_blocked: excludeBlocked(),
  }))

  // Shelf sections set explicit `exclude_blocked: false` without waiting on system config;
  // browse/search queries wait until system config settles so they don't fire twice.
  const sectionNeedsConfig = computed(() => {
    const sec = activeSection.value
    return sec ? SECTIONS[sec].exclude_blocked : true
  })

  const queryEnabled = computed(() => !sectionNeedsConfig.value || (isConfigReady?.value ?? true))

  const query = useInfiniteQuery({
    queryKey: computed(() => queryKeys.novelsList(listParams.value, refreshNonce.value)),
    queryFn: ({ pageParam, signal }) =>
      novelApi.getNovels(
        {
          ...listParams.value,
          cursor: pageParam,
          per_page: DEFAULT_PAGE_SIZE,
        },
        signal,
      ),
    initialPageParam: undefined as Record<string, unknown> | undefined,
    getNextPageParam: (lastPage) =>
      (lastPage.cursor as Record<string, unknown> | null | undefined) ?? undefined,
    enabled: queryEnabled,
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
  const loadingMore = computed(() => query.isFetchingNextPage.value)
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

  function patchNovelInCache(updated: Novel) {
    queryClient.setQueriesData<InfiniteData<NovelListResult>>(
      { queryKey: ['novels', 'list'] },
      (old) => {
        if (!old) return old
        return {
          ...old,
          pages: old.pages.map((page) => ({
            ...page,
            novels: page.novels.map((n) => (n.id === updated.id ? updated : n)),
          })),
        }
      },
    )
  }

  function patchNovelStateInCache(
    payload: { id: number; field: 'is_favourite' | 'is_special_follow'; value: number },
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
              return n.id === payload.id ? { ...n, [payload.field]: payload.value } : n
            }),
          })),
        }
      },
    )
  }

  function removeNovelFromCache(novelId: number) {
    queryClient.setQueriesData<InfiniteData<NovelListResult>>(
      { queryKey: ['novels', 'list'] },
      (old) => {
        if (!old) return old
        return {
          ...old,
          pages: old.pages.map((page) => ({
            ...page,
            novels: page.novels.filter((n) => n.id !== novelId),
          })),
        }
      },
    )
  }

  const handleSearch = (keyword?: string, options?: { setOrdering?: boolean }) =>
    navigateSearch(keyword ?? filters.value.keyword, options)

  const resetFilters = () => navigateToSection('novels')

  return {
    novels,
    loading,
    loadingMore,
    error,
    noMoreData,
    filters,
    activeSection,
    hasExcluded,
    excludeBlocked,
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
