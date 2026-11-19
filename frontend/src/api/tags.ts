import { apiClient, unwrap, unwrapVoid } from './client'
import type { TagAlias, TagAliasSuggestList, TagPreference } from '../types'

export const tagPreferenceApi = {
  async getTagPreferences(signal?: AbortSignal): Promise<TagPreference[]> {
    const res = await apiClient.GET('/api/tag-preferences', { signal })
    return unwrap(res, '加载标签偏好失败')
  },

  async setTagPreference(
    tag: string,
    preference: 'favourite' | 'blocked',
    sortIndex = 0,
  ): Promise<TagPreference> {
    const res = await apiClient.POST('/api/tag-preferences', {
      body: { tag, preference, sort_index: sortIndex },
    })
    return unwrap(res, '添加标签偏好失败')
  },

  async deleteTagPreference(prefId: number): Promise<void> {
    const res = await apiClient.DELETE('/api/tag-preferences/{pref_id}', {
      params: { path: { pref_id: prefId } },
    })
    unwrapVoid(res, '删除标签偏好失败')
  },

  async reorderTagPreferences(tagIds: number[]): Promise<unknown> {
    const res = await apiClient.POST('/api/tag-preferences/reorder', { body: tagIds })
    return unwrap(res, '标签排序失败')
  },
}

export const tagAliasApi = {
  async getTagAliases(signal?: AbortSignal): Promise<TagAlias[]> {
    const res = await apiClient.GET('/api/tag-aliases', { signal })
    return unwrap(res, '加载标签别名失败')
  },

  async suggestTagAliases(
    limit = 5,
    offset = 0,
    targetTag?: string,
    signal?: AbortSignal,
  ): Promise<TagAliasSuggestList> {
    const res = await apiClient.GET('/api/tag-aliases/suggest', {
      params: {
        query: {
          limit,
          offset,
          target_tag: targetTag || undefined,
        },
      },
      signal,
    })
    return unwrap(res, '获取别名建议失败')
  },

  async createTagAlias(source: string, target: string): Promise<TagAlias> {
    const res = await apiClient.POST('/api/tag-aliases', {
      body: { source, target },
    })
    return unwrap(res, '创建标签别名失败')
  },

  async deleteTagAlias(id: number): Promise<void> {
    const res = await apiClient.DELETE('/api/tag-aliases/{alias_id}', {
      params: { path: { alias_id: id } },
    })
    unwrapVoid(res, '删除标签别名失败')
  },
}
