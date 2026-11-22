import { apiClient, buildApiUrl, unwrap, unwrapVoid } from './client'
import { ApiError, extractFastApiDetail } from './errors'
import type {
  AuthorLookup,
  BatchMetadataRequest,
  BatchMetadataResult,
  BatchOperation,
  BatchOperationResult,
  BatchScope,
  FormatMode,
  GetNovelsParams,
  MatchIdsResult,
  Novel,
  NovelCountResult,
  NovelIdsResponse,
  NovelListResult,
  NovelsByIdsResponse,
  NovelUpdatePayload,
  OrderBy,
  OrderDirection,
  SeriesLookup,
  SortableOrderBy,
} from '../types'

export const novelApi = {
  async getNovels(params: GetNovelsParams, signal?: AbortSignal): Promise<NovelListResult> {
    const res = await apiClient.GET('/api/novels', {
      params: {
        query: {
          keyword: params.keyword,
          min_like: params.min_like,
          min_text: params.min_text,
          exclude_blocked: params.exclude_blocked,
          only_blocked: params.only_blocked,
          order_by: params.order_by,
          order_direction: params.order_direction,
          cursor: params.cursor ? JSON.stringify(params.cursor) : undefined,
          per_page: params.per_page,
        },
      },
      signal,
    })
    return unwrap(res, '获取小说列表失败')
  },

  async toggleFavourite(novelId: number): Promise<void> {
    const res = await apiClient.POST('/api/novels/{novel_id}/favourite', {
      params: { path: { novel_id: novelId } },
    })
    unwrapVoid(res, '操作收藏失败')
  },

  async toggleSpecialFollow(authorId: number): Promise<void> {
    const res = await apiClient.POST('/api/novels/author/{author_id}/follow', {
      params: { path: { author_id: authorId } },
    })
    unwrapVoid(res, '操作特别关注失败')
  },

  /**
   * 编辑单本元数据（卡片「编辑」）——只提交发生变化的字段。
   * 返回更新后的完整对象，调用方原地更新缓存。
   */
  async updateNovel(novelId: number, payload: NovelUpdatePayload): Promise<Novel> {
    const res = await apiClient.PATCH('/api/novels/{novel_id}', {
      params: { path: { novel_id: novelId } },
      body: payload,
    })
    return unwrap(res, '保存小说信息失败')
  },

  /** 按 pixiv id 重新抓取单本小说元数据（无请求体）。 */
  async syncNovel(novelId: number): Promise<Novel> {
    const res = await apiClient.POST('/api/novels/{novel_id}/sync', {
      params: { path: { novel_id: novelId } },
    })
    return unwrap(res, '同步小说元数据失败')
  },

  /** 删除单本小说及其本地文件（DELETE /api/novels/{novelId}）。 */
  async deleteNovel(novelId: number): Promise<void> {
    const res = await apiClient.DELETE('/api/novels/{novel_id}', {
      params: { path: { novel_id: novelId } },
    })
    unwrapVoid(res, '删除小说失败')
  },

  async lookupAuthor(authorId: number, signal?: AbortSignal): Promise<AuthorLookup> {
    const res = await apiClient.GET('/api/novels/author/{author_id}', {
      params: { path: { author_id: authorId } },
      signal,
    })
    return unwrap(res, '查询作者信息失败')
  },

  async lookupSeries(seriesId: number, signal?: AbortSignal): Promise<SeriesLookup> {
    const res = await apiClient.GET('/api/novels/series/{series_id}', {
      params: { path: { series_id: seriesId } },
      signal,
    })
    return unwrap(res, '查询系列信息失败')
  },

  downloadUrl(novelId: number, format: 'txt' | 'epub' = 'txt'): string {
    return buildApiUrl(`/novels/${novelId}/download?format=${format}`)
  },

  async countNovels(params: GetNovelsParams, signal?: AbortSignal): Promise<NovelCountResult> {
    const res = await apiClient.GET('/api/novels/count', {
      params: {
        query: {
          keyword: params.keyword,
          min_like: params.min_like,
          min_text: params.min_text,
          exclude_blocked: params.exclude_blocked,
        },
      },
      signal,
    })
    return unwrap(res, '统计小说数量失败')
  },

  async batchDownload(params: {
    keyword?: string
    order_by?: OrderBy
    order_direction?: OrderDirection
    min_like?: number
    min_text?: number
    limit?: number
    format_mode?: FormatMode
    zip_name?: string
    naming_template?: string
    novel_ids?: number[]
  }): Promise<{ blob: Blob; contentDisposition?: string; missingIds: string[] }> {
    const { data, error, response } = await apiClient.POST('/api/novels/batch-download', {
      body: {
        keyword: params.keyword,
        order_by: params.order_by ?? 'id',
        order_direction: params.order_direction ?? 'DESC',
        min_like: params.min_like,
        min_text: params.min_text,
        limit: params.limit ?? 500,
        format_mode: params.format_mode ?? 'txt',
        zip_name: params.zip_name,
        naming_template: params.naming_template,
        novel_ids: params.novel_ids,
      },
      parseAs: 'blob',
    })
    if (error !== undefined || !response.ok || !(data instanceof Blob)) {
      let body: unknown = error
      if (error instanceof Blob) {
        try {
          body = JSON.parse(await error.text())
        } catch {
          body = null
        }
      }
      throw new ApiError(response.status, body, extractFastApiDetail(body) || '下载失败')
    }
    const missingIdsHeader = response.headers.get('x-batch-missing-ids')
    const missingIds = missingIdsHeader
      ? missingIdsHeader.split(',').map((s) => s.trim()).filter(Boolean)
      : []
    return {
      blob: data,
      contentDisposition: response.headers.get('content-disposition') ?? undefined,
      missingIds,
    }
  },

  async batchDownloadPreview(
    params: {
      keyword?: string
      order_by?: OrderBy
      order_direction?: OrderDirection
      min_like?: number
      min_text?: number
      format_mode?: FormatMode
      naming_template?: string
      novel_ids?: number[]
    },
    signal?: AbortSignal,
  ): Promise<{ path: string | null }> {
    const res = await apiClient.POST('/api/novels/batch-download/preview', {
      body: {
        keyword: params.keyword,
        order_by: params.order_by ?? 'id',
        order_direction: params.order_direction ?? 'DESC',
        min_like: params.min_like,
        min_text: params.min_text,
        limit: 500,
        format_mode: params.format_mode ?? 'txt',
        naming_template: params.naming_template,
        novel_ids: params.novel_ids,
      },
      signal,
    })
    return unwrap(res, '预览失败') as { path: string | null }
  },

  async batchOperation(params: {
    operation: BatchOperation
    scope: BatchScope
    tags?: string[]
  }): Promise<BatchOperationResult> {
    const res = await apiClient.POST('/api/novels/batch', {
      body: {
        operation: params.operation,
        scope: params.scope,
        tags: params.tags ?? [],
      },
    })
    return unwrap(res, '批量操作失败')
  },

  async batchMetadata(params: BatchMetadataRequest): Promise<BatchMetadataResult> {
    const res = await apiClient.POST('/api/novels/batch-metadata', { body: params })
    return unwrap(res, '批量修改元数据失败')
  },

  async getNovelIds(params: GetNovelsParams, signal?: AbortSignal): Promise<NovelIdsResponse> {
    const res = await apiClient.GET('/api/novels/ids', {
      params: {
        query: {
          keyword: params.keyword,
          min_like: params.min_like,
          min_text: params.min_text,
          exclude_blocked: params.exclude_blocked,
        },
      },
      signal,
    })
    return unwrap(res, '获取匹配 ID 列表失败')
  },

  async sortNovelIds(
    novelIds: number[],
    orderBy: SortableOrderBy,
    orderDirection: OrderDirection,
    signal?: AbortSignal,
  ): Promise<NovelIdsResponse> {
    const res = await apiClient.POST('/api/novels/sort-ids', {
      body: {
        novel_ids: novelIds,
        order_by: orderBy,
        order_direction: orderDirection,
      },
      signal,
    })
    return unwrap(res, '排序失败')
  },

  async getNovelsByIds(novelIds: number[], signal?: AbortSignal): Promise<NovelsByIdsResponse> {
    const res = await apiClient.POST('/api/novels/by-ids', {
      body: { novel_ids: novelIds },
      signal,
    })
    return unwrap(res, '获取小说详情失败')
  },

  async matchNovelIds(params: {
    novel_ids: number[]
    keyword?: string
    min_like?: number
    min_text?: number
    exclude_blocked?: boolean
  }): Promise<MatchIdsResult> {
    const { exclude_blocked, ...body } = params
    const res = await apiClient.POST('/api/novels/match-ids', {
      params: {
        query: exclude_blocked === undefined ? undefined : { exclude_blocked },
      },
      body,
    })
    return unwrap(res, '匹配选中范围失败')
  },

  async submitBatchExport(params: {
    novel_ids: number[]
    format_mode?: FormatMode
    zip_name?: string
    naming_template?: string
  }): Promise<{ task_id: number; matched: number }> {
    const res = await apiClient.POST('/api/novels/batch-export', {
      body: {
        novel_ids: params.novel_ids,
        format_mode: params.format_mode ?? 'txt',
        zip_name: params.zip_name,
        naming_template: params.naming_template,
      },
    })
    return unwrap(res, '提交导出任务失败')
  },

  exportDownloadUrl(taskId: number): string {
    return buildApiUrl(`/novels/export/${taskId}/download`)
  },
}
