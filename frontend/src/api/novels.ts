import { apiClient } from './client'
import type { Novel, GetNovelsParams } from '../types'

export const novelApi = {
  async getNovels(params: GetNovelsParams) {
    const queryParams: Record<string, unknown> = { ...params }
    if (params.cursor) {
      queryParams.cursor = JSON.stringify(params.cursor)
    }
    const response = await apiClient.get('/novels/', { params: queryParams })
    return response.data as { novels: Novel[]; cursor: Record<string, unknown> | null }
  },

  async toggleFavourite(novelId: number) {
    await apiClient.post(`/novels/${novelId}/favourite`)
  },

  async toggleSpecialFollow(authorId: number) {
    await apiClient.post(`/novels/author/${authorId}/follow`)
  },

  /** Blob download through the shared client so X-API-Key can be attached. */
  async downloadNovel(novelId: number, format: 'txt' | 'epub' = 'txt') {
    return apiClient.get(`/novels/${novelId}/download`, {
      params: { format },
      responseType: 'blob',
    })
  },

  async countNovels(params: GetNovelsParams) {
    const queryParams: Record<string, unknown> = { ...params }
    const response = await apiClient.get('/novels/count', { params: queryParams })
    return response.data as { total: number }
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
  }) {
    const response = await apiClient.post('/novels/batch-download', params, {
      responseType: 'blob',
    })
    return response
  },

  async batchDownloadPreview(params: {
    keyword?: string
    order_by?: string
    order_direction?: string
    min_like?: number
    min_text?: number
    format_mode?: string
    naming_template?: string
  }) {
    const response = await apiClient.post('/novels/batch-download/preview', params)
    return response.data as { path: string | null }
  },
}
