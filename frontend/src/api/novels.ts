import { apiClient, buildApiUrl } from './client'
import type {
  GetNovelsParams, BatchScope, BatchOperation, BatchOperationResult,
  NovelIdsResponse, NovelsByIdsResponse, MatchIdsResult, NovelCountResult,
  NovelListResult,
} from '../types'

/** 同步批量操作上限（与后端 BATCH_MAX_NOVELS 一致）；超过走后台任务。 */
export const BATCH_MAX_NOVELS = 5000

export const novelApi = {
  async getNovels(params: GetNovelsParams) {
    const queryParams: Record<string, unknown> = { ...params }
    if (params.cursor) {
      queryParams.cursor = JSON.stringify(params.cursor)
    }
    const response = await apiClient.get('/novels/', { params: queryParams })
    return response.data as NovelListResult
  },

  async toggleFavourite(novelId: number) {
    await apiClient.post(`/novels/${novelId}/favourite`)
  },

  async toggleSpecialFollow(authorId: number) {
    await apiClient.post(`/novels/author/${authorId}/follow`)
  },

  /**
   * Browser-navigable download URL for a single novel. The endpoint serves
   * the file with `Content-Disposition: attachment`, so a plain navigation
   * triggers the browser/WebView's native download (works where blob: object
   * URLs are unsupported) and the suggested filename is the novel title.
   */
  downloadUrl(novelId: number, format: 'txt' | 'epub' = 'txt') {
    return buildApiUrl(`/novels/${novelId}/download?format=${format}`)
  },

  async countNovels(params: GetNovelsParams) {
    const queryParams: Record<string, unknown> = { ...params }
    const response = await apiClient.get('/novels/count', {
      params: queryParams,
      // Serialize excluded_ids as repeated keys (excluded_ids=1&excluded_ids=2)
      // — FastAPI binds repeated params to list[int]; the axios default
      // (excluded_ids[]=...) would produce a differently-named key.
      paramsSerializer: { indexes: null },
    })
    return response.data as NovelCountResult
  },

  async batchDownload(params: {
    keyword?: string
    order_by?: string
    order_direction?: string
    min_like?: number
    min_text?: number
    limit?: number
    format_mode?: string
    zip_name?: string
    naming_template?: string
    novel_ids?: number[]
    excluded_ids?: number[]
  }) {
    const response = await apiClient.post('/novels/batch-download', params, {
      responseType: 'blob',
      // ZIP 打包耗时随篇数增长——不用全局 30s 超时（大导出走任务端点）。
      timeout: 0,
    })
    return response
  },

  async batchDownloadPreview(
    params: {
      keyword?: string
      order_by?: string
      order_direction?: string
      min_like?: number
      min_text?: number
      format_mode?: string
      naming_template?: string
      novel_ids?: number[]
      excluded_ids?: number[]
    },
    signal?: AbortSignal,
  ) {
    const response = await apiClient.post('/novels/batch-download/preview', params, {
      signal,
    })
    return response.data as { path: string | null }
  },

  /** Batch delete / add_tags / remove_tags against a resolved scope. */
  async batchOperation(params: {
    operation: BatchOperation
    scope: BatchScope
    tags?: string[]
  }) {
    const response = await apiClient.post('/novels/batch', params)
    return response.data as BatchOperationResult
  },

  /** All IDs matching the filters — powers the 「全选匹配」bulk-add action. */
  async getNovelIds(params: GetNovelsParams) {
    const queryParams: Record<string, unknown> = { ...params }
    const response = await apiClient.get('/novels/ids', { params: queryParams })
    return response.data as NovelIdsResponse
  },

  /** Blocked-tag novel IDs within the current scope — 「查看被排除」view. */
  async getBlockedNovelIds(params: GetNovelsParams) {
    const queryParams: Record<string, unknown> = { ...params }
    const response = await apiClient.get('/novels/blocked-ids', {
      params: queryParams,
    })
    return response.data as NovelIdsResponse
  },

  /** Order an explicit id list by a novel column — 「查看已选」排序. */
  async sortNovelIds(
    novelIds: number[],
    orderBy: string,
    orderDirection: string,
  ) {
    const response = await apiClient.post('/novels/sort-ids', {
      novel_ids: novelIds,
      order_by: orderBy,
      order_direction: orderDirection,
    })
    return response.data as NovelIdsResponse
  },

  /** Novel details by explicit ID list — powers the 「查看已选」view. */
  async getNovelsByIds(novelIds: number[]) {
    const response = await apiClient.post('/novels/by-ids', {
      novel_ids: novelIds,
    })
    return response.data as NovelsByIdsResponse
  },

  /** Subset of the selection matching the current scope — scoped clear. */
  async matchNovelIds(params: {
    novel_ids: number[]
    keyword?: string
    min_like?: number
    min_text?: number
  }) {
    const response = await apiClient.post('/novels/match-ids', params)
    return response.data as MatchIdsResult
  },

  /** Enqueue a batch operation into the background task system (any size). */
  async submitBatchTask(params: {
    operation: BatchOperation
    scope: BatchScope
    tags?: string[]
  }) {
    const response = await apiClient.post('/novels/batch-task', params)
    return response.data as { task_id: number; matched: number }
  },

  /** Enqueue a batch export into the background task system (large ZIPs). */
  async submitBatchExport(params: {
    novel_ids: number[]
    format_mode?: string
    zip_name?: string
    naming_template?: string
  }) {
    const response = await apiClient.post('/novels/batch-export', params)
    return response.data as { task_id: number; matched: number }
  },

  /** Browser-navigable download URL for a completed background export ZIP. */
  exportDownloadUrl(taskId: number) {
    return buildApiUrl(`/novels/export/${taskId}/download`)
  },
}
