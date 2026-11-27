import { apiClient, unwrap, unwrapVoid } from './client'
import type { FailedNovelListResponse } from '../types'

/** 分页大小，与后端默认 limit 一致。 */
export const FAILED_NOVELS_PAGE_SIZE = 100

export const failedNovelApi = {
  async list(
    offset: number,
    limit = FAILED_NOVELS_PAGE_SIZE,
    signal?: AbortSignal,
  ): Promise<FailedNovelListResponse> {
    const res = await apiClient.GET('/api/failed-novels', {
      params: { query: { offset, limit } },
      signal,
    })
    return unwrap(res, '加载失败记录失败')
  },

  /** 重置单条失败记录计数（记录保留，解封后下次批量任务自动重试）。 */
  async resetCount(failedId: number): Promise<void> {
    const res = await apiClient.POST('/api/failed-novels/{failed_id}/reset-count', {
      params: { path: { failed_id: failedId } },
    })
    unwrapVoid(res, '重置失败')
  },

  /** 重置全部失败计数（记录保留）。 */
  async resetAll(): Promise<void> {
    const res = await apiClient.POST('/api/failed-novels/reset-count')
    unwrapVoid(res, '重置失败')
  },

  /** 入队后台重试任务（任务管理页可见进度）。 */
  async retry(ids: number[]): Promise<{ task_id: number; matched: number }> {
    const res = await apiClient.POST('/api/failed-novels/retry', {
      body: { ids },
    })
    return unwrap(res, '入队重试失败') as { task_id: number; matched: number }
  },

  /** 入队后台重试任务：重试全部失败记录（服务端取全量台账，与当前加载页无关）。 */
  async retryAll(): Promise<{ task_id: number; matched: number }> {
    const res = await apiClient.POST('/api/failed-novels/retry-all')
    return unwrap(res, '入队重试失败') as { task_id: number; matched: number }
  },
}
