import { computed } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { getApiErrorMessage, queryKeys, tokenApi } from '../api'
import type { Token, TokenUpdate } from '../types'

/** Backend masks responses as ``****`` (<=4 chars) or ``****`` + last 4 chars. */
export const isMaskedToken = (value: string) => value === '****' || /^\*{4}.{4}$/.test(value)

export function useTokens() {
  const queryClient = useQueryClient()

  const query = useQuery({
    queryKey: queryKeys.tokens(),
    queryFn: ({ signal }) => tokenApi.getTokens(signal),
  })

  const tokens = computed<Token[]>(() => query.data.value ?? [])
  const loading = computed(() => query.isPending.value)
  const error = computed(() =>
    query.error.value ? getApiErrorMessage(query.error.value, '获取 Token 列表失败') : null,
  )

  const invalidateTokens = () => queryClient.invalidateQueries({ queryKey: queryKeys.tokens() })

  const toggleMutation = useMutation({
    mutationFn: ({ token, field }: { token: Token; field: 'premium' | 'valid' | 'is_follow' }) =>
      tokenApi.updateToken(token.id, { [field]: !token[field] }),
    onSuccess: invalidateTokens,
  })

  const reorderMutation = useMutation({
    mutationFn: (newTokens: Token[]) => tokenApi.reorderTokens(newTokens.map((t) => t.id)),
    onMutate: async (newTokens) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.tokens() })
      const previous = queryClient.getQueryData<Token[]>(queryKeys.tokens())
      queryClient.setQueryData(queryKeys.tokens(), newTokens)
      return { previous }
    },
    onError: (_err, _newTokens, ctx) => {
      if (ctx?.previous !== undefined) {
        queryClient.setQueryData(queryKeys.tokens(), ctx.previous)
      }
    },
    onSettled: invalidateTokens,
  })

  const saveMutation = useMutation({
    mutationFn: async (data: {
      id?: number
      name: string
      token: string
      premium: boolean
      valid: boolean
    }) => {
      const trimmedToken = data.token.trim()
      if (data.id) {
        const payload: TokenUpdate = {
          name: data.name,
          premium: data.premium,
          valid: data.valid,
        }
        if (trimmedToken && !isMaskedToken(trimmedToken)) {
          payload.token = trimmedToken
        }
        return tokenApi.updateToken(data.id, payload)
      }
      return tokenApi.createToken({
        name: data.name,
        token: trimmedToken,
        premium: data.premium || false,
        valid: data.valid !== false,
        is_follow: false,
      })
    },
    onSuccess: invalidateTokens,
  })

  const removeMutation = useMutation({
    mutationFn: (id: number) => tokenApi.deleteToken(id),
    onSuccess: invalidateTokens,
  })

  return {
    tokens,
    loading,
    error,
    loadTokens: () => query.refetch(),
    toggleField: (token: Token, field: 'premium' | 'valid' | 'is_follow') =>
      toggleMutation.mutateAsync({ token, field }),
    reorder: (newTokens: Token[]) => reorderMutation.mutateAsync(newTokens),
    save: (data: { id?: number; name: string; token: string; premium: boolean; valid: boolean }) =>
      saveMutation.mutateAsync(data),
    remove: (id: number) => removeMutation.mutateAsync(id),
  }
}
