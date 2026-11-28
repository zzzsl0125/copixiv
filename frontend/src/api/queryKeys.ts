import type { GetNovelsParams } from '../types'

export const queryKeys = {
  systemConfig: () => ['system', 'config'] as const,
  sources: () => ['sources'] as const,
  sourceCredentials: (source: string) => ['sources', source, 'credentials'] as const,
  authorSearch: (q: string) => ['novels', 'author', 'search', q] as const,
  seriesSearch: (q: string) => ['novels', 'series', 'search', q] as const,
  tagPreferences: () => ['tag-preferences'] as const,
  tagAliases: () => ['tag-aliases'] as const,
  searchHistory: () => ['search-history'] as const,
  tasksScheduled: () => ['tasks', 'scheduled'] as const,
  tasksMethods: () => ['tasks', 'methods'] as const,
  tasksHistory: () => ['tasks', 'history'] as const,
  failedNovels: () => ['failed-novels'] as const,
  novelsAll: () => ['novels'] as const,
  novelsList: (
    params: Omit<GetNovelsParams, 'cursor' | 'per_page'>,
    refreshNonce = 0,
  ) => ['novels', 'list', params, refreshNonce] as const,
  novelsCount: (params: Omit<GetNovelsParams, 'cursor' | 'per_page'>) =>
    ['novels', 'count', params] as const,
}
