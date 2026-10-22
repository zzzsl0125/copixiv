import { apiClient } from './client'
import type { TaskMethod, ScheduledTask, ScheduledTaskCreate, ScheduledTaskUpdate, TaskHistory } from '../types'

export const taskApi = {
  async getTaskMethods() {
    const response = await apiClient.get('/tasks/methods')
    return response.data as TaskMethod[]
  },

  async getScheduledTasks() {
    const response = await apiClient.get('/tasks/scheduled')
    return response.data as ScheduledTask[]
  },

  async createScheduledTask(data: ScheduledTaskCreate) {
    const response = await apiClient.post('/tasks/scheduled', data)
    return response.data as ScheduledTask
  },

  async updateScheduledTask(id: number, data: ScheduledTaskUpdate) {
    const response = await apiClient.put(`/tasks/scheduled/${id}`, data)
    return response.data as ScheduledTask
  },

  async deleteScheduledTask(id: number) {
    const response = await apiClient.delete(`/tasks/scheduled/${id}`)
    return response.data
  },

  async runScheduledTask(id: number) {
    const response = await apiClient.post(`/tasks/scheduled/${id}/run`)
    return response.data
  },

  async reorderScheduledTasks(taskIds: number[]) {
    const response = await apiClient.post('/tasks/scheduled/reorder', taskIds)
    return response.data
  },

  async getTaskHistory(limit = 50, offset = 0) {
    const response = await apiClient.get('/tasks/history', { params: { limit, offset } })
    return response.data as { items: TaskHistory[]; total: number }
  },

  /**
   * Task functions that currently have a pending/running row.
   *
   * Hundreds of bytes. The busy flags used to be derived from
   * ``getTaskHistory(100, 0)`` — a multi-megabyte page polled every 5 s while
   * a task was running, and blind past the first 100 rows.
   */
  async getBusyTasks() {
    const response = await apiClient.get('/tasks/busy')
    return response.data as { funcs: string[] }
  },

  /**
   * Fetch one row's execution log on demand.
   *
   * The list endpoint no longer inlines it: it was 96 % of a 7.4 MB page and
   * the poller re-read the whole page every 5 s while a task was running.
   * The log is only needed after the user clicks 「日志」.
   */
  async getTaskLog(taskId: number) {
    const response = await apiClient.get(`/tasks/history/${taskId}/log`)
    return response.data as { id: number; log: string }
  },
}
