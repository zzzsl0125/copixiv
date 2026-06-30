import { apiClient } from './client'
import type { Token, TokenCreate, TokenUpdate } from '../types'

export const tokenApi = {
  async getTokens() {
    const response = await apiClient.get('/tokens/')
    return response.data as Token[]
  },

  async createToken(data: TokenCreate) {
    const response = await apiClient.post('/tokens/', data)
    return response.data as Token
  },

  async updateToken(id: number, data: TokenUpdate) {
    const response = await apiClient.put(`/tokens/${id}/`, data)
    return response.data as Token
  },

  async deleteToken(id: number) {
    const response = await apiClient.delete(`/tokens/${id}/`)
    return response.data
  },

  async reorderTokens(tokenIds: number[]) {
    const response = await apiClient.post('/tokens/reorder/', tokenIds)
    return response.data
  },
}
