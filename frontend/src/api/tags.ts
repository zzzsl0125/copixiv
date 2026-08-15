import { apiClient } from './client'
import type { TagPreference, TagAlias, TagAliasSuggestList } from '../types'

export const tagPreferenceApi = {
  async getTagPreferences() {
    const response = await apiClient.get('/tag-preferences/')
    return response.data as TagPreference[]
  },

  async setTagPreference(tag: string, preference: 'favourite' | 'blocked') {
    const response = await apiClient.post('/tag-preferences/', { tag, preference })
    return response.data
  },

  async deleteTagPreference(prefId: number) {
    const response = await apiClient.delete(`/tag-preferences/${prefId}`)
    return response.data
  },

  async reorderTagPreferences(tagIds: number[]) {
    const response = await apiClient.post('/tag-preferences/reorder', tagIds)
    return response.data
  },
}

export const tagAliasApi = {
  async getTagAliases() {
    const response = await apiClient.get('/tag-aliases/')
    return response.data as TagAlias[]
  },

  async suggestTagAliases(limit = 5, offset = 0, targetTag?: string) {
    const params: Record<string, unknown> = { limit, offset }
    if (targetTag) {
      params.target_tag = targetTag
    }
    const response = await apiClient.get('/tag-aliases/suggest', { params })
    return response.data as TagAliasSuggestList
  },

  async createTagAlias(source: string, target: string) {
    const response = await apiClient.post('/tag-aliases/', { source, target })
    return response.data as TagAlias
  },

  async deleteTagAlias(id: number) {
    const response = await apiClient.delete(`/tag-aliases/${id}`)
    return response.data
  },
}
