import { apiClient, buildApiUrl } from './client'
import type {
  Novel, NovelUpdatePayload, AuthorLookup, SeriesLookup,
  GetNovelsParams, BatchScope, BatchOperation, BatchOperationResult,
  NovelIdsResponse, NovelsByIdsResponse, MatchIdsResult, NovelCountResult,
  NovelListResult, BatchMetadataRequest, BatchMetadataResult,
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
   * 编辑单本元数据（卡片「编辑」）——只提交发生变化的字段。
   *
   * 返回**更新后的完整对象**，调用方据此原地替换列表里的那一本（不整页重取）。
   * 服务端两道拒绝（调用方按错误提示呈现即可）：
   * - 400：pixiv 书提交 `title` / `author_id` / `series_id` / `caption`（仅本地书
   *   可编辑，理由与文案见 `field_policy.py`——title / series_id / caption 是
   *   "来源会覆盖"，author_id 是"作者身份以来源为准"）；
   * - 422：`series_name` / `author_name` 以及 `source` 等结构性字段压根不在
   *   可提交集合里，`extra="forbid"` 直接拒掉。
   */
  async updateNovel(novelId: number, payload: NovelUpdatePayload) {
    const response = await apiClient.patch(`/novels/${novelId}`, payload)
    return response.data as Novel
  },

  /**
   * 按 pixiv id 重新抓取这本小说的元数据（编辑器左下角「同步」，无请求体）。
   *
   * 返回与 PATCH 同形状的**完整对象**（含 `editable_fields`），调用方据此原地
   * 替换卡片并重装弹窗初值。失败语义由服务端给：本地书 → 400（没有来源可同步）、
   * pixiv 上已不存在 / 受限 → 404；调用方一律按错误提示呈现。
   */
  async syncNovel(novelId: number) {
    const response = await apiClient.post(`/novels/${novelId}/sync`)
    return response.data as Novel
  },

  /**
   * 作者 ID 预览（编辑器「作者」一栏的即时反馈）。
   *
   * 只读、不落库：库内已知就直接返回（离线可用），否则服务端问一次 pixiv。
   * 返回 `source: 'unknown'` **不是错误**——id 才是身份，拿不到名字照样能保存。
   * 调用方要能接受失败（网络抖动不该让一个提示框变成报错弹窗）。
   */
  async lookupAuthor(authorId: number) {
    const response = await apiClient.get(`/novels/author/${authorId}`)
    return response.data as AuthorLookup
  },

  /**
   * 系列 ID 预览（编辑器「系列」一栏的即时反馈）—— 与 `lookupAuthor` 同构。
   *
   * 名字来源链：`series` 行 → 同系列 novel 行 → pixiv；前两步都在本地库完成，
   * 所以大多数情况下这个请求是**离线可用**的。
   */
  async lookupSeries(seriesId: number) {
    const response = await apiClient.get(`/novels/series/${seriesId}`)
    return response.data as SeriesLookup
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

  /**
   * 批量编辑「作者」/「系列」（批量栏「编辑」）——`apply: false` 先预演报数，
   * `apply: true` 才落库；两次的响应形状相同（预演时 `affected = 0`）。
   *
   * 只作用于**本地书**：pixiv 书的这两列是"身份以来源为准"，服务端一律跳过，
   * `skipped` 就是那个数（单本 PATCH 对它们硬拒 400，批量这里改成预先报数）。
   * 请求体里**键不出现 = 不修改**，所以"留空"不能靠空串表达，解除关联必须由
   * 调用方显式给 `null`。
   */
  async batchMetadata(params: BatchMetadataRequest) {
    const response = await apiClient.post('/novels/batch-metadata', params)
    return response.data as BatchMetadataResult
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
