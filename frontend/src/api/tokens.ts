import { apiClient, unwrap, unwrapVoid } from './client'
import type { Token, TokenCreate, TokenUpdate } from '../types'

export const tokenApi = {
  async getTokens(signal?: AbortSignal): Promise<Token[]> {
    const res = await apiClient.GET('/api/tokens', { signal })
    return unwrap(res, '获取 Token 列表失败')
  },

  async createToken(data: TokenCreate): Promise<Token> {
    const res = await apiClient.POST('/api/tokens', { body: data })
    return unwrap(res, '创建 Token 失败')
  },

  async updateToken(id: number, data: TokenUpdate): Promise<Token> {
    const res = await apiClient.PUT('/api/tokens/{token_id}', {
      params: { path: { token_id: id } },
      body: data,
    })
    return unwrap(res, '更新 Token 失败')
  },

  async deleteToken(id: number): Promise<void> {
    const res = await apiClient.DELETE('/api/tokens/{token_id}', {
      params: { path: { token_id: id } },
    })
    unwrapVoid(res, '删除 Token 失败')
  },

  async reorderTokens(tokenIds: number[]): Promise<unknown> {
    const res = await apiClient.POST('/api/tokens/reorder', { body: tokenIds })
    return unwrap(res, '保存排序失败')
  },
}
