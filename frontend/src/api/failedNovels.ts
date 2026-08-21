import { apiClient } from './client'
import type { FailedNovelListResponse, FailedNovelCountResponse } from '../types'

/** 分页大小，与后端默认 limit 一致。 */
export const FAILED_NOVELS_PAGE_SIZE = 100

export const failedNovelApi = {
  async list(offset: number, limit = FAILED_NOVELS_PAGE_SIZE) {
    const response = await apiClient.get<FailedNovelListResponse>('/failed-novels/', {
      params: { offset, limit },
    })
    return response.data
  },

  async count() {
    const response = await apiClient.get<FailedNovelCountResponse>('/failed-novels/count')
    return response.data.count
  },

  /** 重置单条失败记录计数（记录保留，解封后下次批量任务自动重试）。 */
  async resetCount(novelId: number) {
    await apiClient.post(`/failed-novels/${novelId}/reset-count`)
  },

  /** 重置全部失败计数（记录保留）。 */
  async resetAll() {
    await apiClient.post('/failed-novels/reset-count')
  },

  /** 入队后台重试任务（任务管理页可见进度）。 */
  async retry(novelIds: number[]) {
    const response = await apiClient.post<{ task_id: number; matched: number }>(
      '/failed-novels/retry',
      { novel_ids: novelIds },
    )
    return response.data
  },

  /** 入队后台重试任务：重试全部失败记录（服务端取全量台账，与当前加载页无关）。 */
  async retryAll() {
    const response = await apiClient.post<{ task_id: number; matched: number }>(
      '/failed-novels/retry-all',
    )
    return response.data
  },
}
