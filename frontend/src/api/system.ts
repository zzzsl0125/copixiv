import { apiClient } from './client'
import type { SystemConfig, SearchHistory } from '../types'

export const systemApi = {
  async getConfig() {
    const response = await apiClient.get('/system/config')
    return response.data as SystemConfig
  },

  async updateConfig(patch: Partial<SystemConfig>) {
    const response = await apiClient.put('/system/config', patch)
    return response.data as SystemConfig
  },
}

export const searchHistoryApi = {
  async getSearchHistory() {
    const response = await apiClient.get('/search-history/')
    return response.data as SearchHistory[]
  },

  async deleteSearchHistoryItem(historyId: number) {
    await apiClient.delete(`/search-history/${historyId}`)
  },

  async clearSearchHistory() {
    await apiClient.delete('/search-history/')
  },
}
