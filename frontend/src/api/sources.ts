import { apiClient, unwrap, unwrapVoid } from './client'
import type {
  SourceConfigUpdate,
  SourceCredential,
  SourceCredentialCreate,
  SourceCredentialUpdate,
  SourceFetchRequest,
  SourceFetchResponse,
  SourceItem,
  SourceTestResult,
} from '../types'

export const sourceApi = {
  async list(signal?: AbortSignal): Promise<SourceItem[]> {
    const res = await apiClient.GET('/api/sources', { signal })
    return unwrap(res, '获取数据源列表失败')
  },

  async updateConfig(source: string, body: SourceConfigUpdate): Promise<SourceItem> {
    const res = await apiClient.PUT('/api/sources/{source}/config', {
      params: { path: { source } },
      body,
    })
    return unwrap(res, '更新数据源配置失败')
  },

  async testConnection(source: string): Promise<SourceTestResult> {
    const res = await apiClient.POST('/api/sources/{source}/test', {
      params: { path: { source } },
    })
    return unwrap(res, '测试数据源连接失败')
  },

  async listCredentials(source: string, signal?: AbortSignal): Promise<SourceCredential[]> {
    const res = await apiClient.GET('/api/sources/{source}/credentials', {
      params: { path: { source } },
      signal,
    })
    return unwrap(res, '获取凭据列表失败')
  },

  async createCredential(
    source: string,
    body: SourceCredentialCreate,
  ): Promise<SourceCredential> {
    const res = await apiClient.POST('/api/sources/{source}/credentials', {
      params: { path: { source } },
      body,
    })
    return unwrap(res, '创建凭据失败')
  },

  async updateCredential(
    source: string,
    credId: number,
    body: SourceCredentialUpdate,
  ): Promise<SourceCredential> {
    const res = await apiClient.PUT('/api/sources/{source}/credentials/{cred_id}', {
      params: { path: { source, cred_id: credId } },
      body,
    })
    return unwrap(res, '更新凭据失败')
  },

  async deleteCredential(source: string, credId: number): Promise<void> {
    const res = await apiClient.DELETE('/api/sources/{source}/credentials/{cred_id}', {
      params: { path: { source, cred_id: credId } },
    })
    unwrapVoid(res, '删除凭据失败')
  },

  async reorderCredentials(source: string, ids: number[]): Promise<void> {
    const res = await apiClient.POST('/api/sources/{source}/credentials/reorder', {
      params: { path: { source } },
      body: ids,
    })
    unwrapVoid(res, '保存排序失败')
  },

  async fetchRefs(body: SourceFetchRequest): Promise<SourceFetchResponse> {
    const res = await apiClient.POST('/api/sources/fetch', { body })
    return unwrap(res, '提交抓取任务失败')
  },
}
