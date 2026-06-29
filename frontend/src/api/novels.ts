import { apiClient, downloadBaseUrl } from './client'
import type { Novel, GetNovelsParams } from '../types'

export const novelApi = {
  async getNovels(params: GetNovelsParams) {
    const queryParams: Record<string, unknown> = { ...params }
    if (params.queries) {
      queryParams.queries = JSON.stringify(params.queries)
    }
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

  downloadUrl(novelId: number, format: 'txt' | 'epub' = 'txt') {
    return `${downloadBaseUrl}/novels/${novelId}/download?format=${format}`
  },

  async countNovels(params: GetNovelsParams) {
    const queryParams: Record<string, unknown> = { ...params }
    if (params.queries) {
      queryParams.queries = JSON.stringify(params.queries)
    }
    const response = await apiClient.get('/novels/count', { params: queryParams })
    return response.data as { total: number }
  },

  async batchDownload(params: {
    queries?: string
    order_by?: string
    order_direction?: string
    min_like?: number
    min_text?: number
    limit?: number
    format_mode?: string
  }) {
    const response = await apiClient.post('/novels/batch-download', params, {
      responseType: 'blob',
    })
    return response
  },
}
