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
}
