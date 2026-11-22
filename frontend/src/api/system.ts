import { apiClient, unwrap, unwrapVoid } from './client'
import type { SearchHistory, SystemConfig } from '../types'

export const systemApi = {
  async getConfig(signal?: AbortSignal): Promise<SystemConfig> {
    const res = await apiClient.GET('/api/system/config', { signal })
    return unwrap(res, '获取系统配置失败')
  },

  async updateConfig(patch: { exclude_blocked_tag_novels: boolean }): Promise<SystemConfig> {
    const res = await apiClient.PUT('/api/system/config', { body: patch })
    return unwrap(res, '保存系统配置失败')
  },
}

export const searchHistoryApi = {
  async getSearchHistory(signal?: AbortSignal): Promise<SearchHistory[]> {
    const res = await apiClient.GET('/api/search-history', { signal })
    return unwrap(res, '加载搜索历史失败')
  },

  async deleteSearchHistoryItem(historyId: number): Promise<void> {
    const res = await apiClient.DELETE('/api/search-history/{history_id}', {
      params: { path: { history_id: historyId } },
    })
    unwrapVoid(res, '删除搜索历史失败')
  },

  async clearSearchHistory(): Promise<void> {
    const res = await apiClient.DELETE('/api/search-history')
    unwrapVoid(res, '清空搜索历史失败')
  },
}
