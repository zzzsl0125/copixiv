import { apiClient, unwrap, unwrapVoid } from './client'
import type {
  ScheduledTask,
  ScheduledTaskCreate,
  ScheduledTaskUpdate,
  TaskHistory,
  TaskMethod,
} from '../types'

export const taskApi = {
  async getTaskMethods(signal?: AbortSignal): Promise<TaskMethod[]> {
    const res = await apiClient.GET('/api/tasks/methods', { signal })
    return unwrap(res, '获取任务函数列表失败') as TaskMethod[]
  },

  async getScheduledTasks(signal?: AbortSignal): Promise<ScheduledTask[]> {
    const res = await apiClient.GET('/api/tasks/scheduled', { signal })
    return unwrap(res, '获取计划任务失败')
  },

  async createScheduledTask(data: ScheduledTaskCreate): Promise<ScheduledTask> {
    const res = await apiClient.POST('/api/tasks/scheduled', { body: data })
    return unwrap(res, '创建计划任务失败')
  },

  async updateScheduledTask(id: number, data: ScheduledTaskUpdate): Promise<ScheduledTask> {
    const res = await apiClient.PUT('/api/tasks/scheduled/{task_id}', {
      params: { path: { task_id: id } },
      body: data,
    })
    return unwrap(res, '更新计划任务失败')
  },

  async deleteScheduledTask(id: number): Promise<void> {
    const res = await apiClient.DELETE('/api/tasks/scheduled/{task_id}', {
      params: { path: { task_id: id } },
    })
    unwrapVoid(res, '删除计划任务失败')
  },

  async runScheduledTask(id: number): Promise<unknown> {
    const res = await apiClient.POST('/api/tasks/scheduled/{task_id}/run', {
      params: { path: { task_id: id } },
    })
    return unwrap(res, '运行计划任务失败')
  },

  async reorderScheduledTasks(taskIds: number[]): Promise<unknown> {
    const res = await apiClient.POST('/api/tasks/scheduled/reorder', { body: taskIds })
    return unwrap(res, '排序失败')
  },

  async getTaskHistory(
    limit = 50,
    offset = 0,
    signal?: AbortSignal,
  ): Promise<{ items: TaskHistory[]; total: number }> {
    const res = await apiClient.GET('/api/tasks/history', {
      params: { query: { limit, offset } },
      signal,
    })
    return unwrap(res, '获取任务历史失败')
  },

  async getBusyTasks(signal?: AbortSignal): Promise<{ funcs: string[] }> {
    const res = await apiClient.GET('/api/tasks/busy', { signal })
    return unwrap(res, '获取活跃任务失败')
  },

  async getTaskLog(taskId: number, signal?: AbortSignal): Promise<{ id: number; log: string }> {
    const res = await apiClient.GET('/api/tasks/history/{task_id}/log', {
      params: { path: { task_id: taskId } },
      signal,
    })
    return unwrap(res, '获取任务日志失败')
  },

  async cancelTask(taskId: number): Promise<{ task_id: number; status: string }> {
    const res = await apiClient.POST('/api/tasks/history/{task_id}/cancel', {
      params: { path: { task_id: taskId } },
    })
    return unwrap(res, '取消任务失败')
  },
}
