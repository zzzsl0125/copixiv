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

  /** 清除单条失败记录（解封，下次批量任务自动重试）。 */
  async remove(novelId: number) {
    await apiClient.delete(`/failed-novels/${novelId}`)
  },

  /** 清空整个失败台账。 */
  async clearAll() {
    await apiClient.delete('/failed-novels/')
  },

  /** 入队后台重试任务（任务管理页可见进度）。 */
  async retry(novelIds: number[]) {
    const response = await apiClient.post<{ task_id: number; matched: number }>(
      '/failed-novels/retry',
      { novel_ids: novelIds },
    )
    return response.data
  },
}
