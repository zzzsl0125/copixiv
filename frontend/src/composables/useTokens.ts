import { ref, onMounted } from 'vue'
import { tokenApi } from '../api'
import { getApiErrorMessage } from '../api/errors'
import type { Token, TokenUpdate } from '../types'

/** Backend masks responses as ``****`` (≤4 chars) or ``****`` + last 4 chars. */
const isMaskedToken = (value: string) => value === '****' || /^\*{4}.{4}$/.test(value)

export function useTokens() {
  const tokens = ref<Token[]>([])
  const loading = ref(true)
  const error = ref<string | null>(null)

  const loadTokens = async () => {
    loading.value = true
    error.value = null
    try {
      tokens.value = await tokenApi.getTokens()
    } catch (err: unknown) {
      error.value = getApiErrorMessage(err, '获取 Token 列表失败')
    } finally {
      loading.value = false
    }
  }

  const toggleField = async (token: Token, field: 'premium' | 'valid') => {
    await tokenApi.updateToken(token.id, { [field]: !token[field] })
    await loadTokens()
  }

  const reorder = async (newTokens: Token[]) => {
    tokens.value = newTokens
    const tokenIds = newTokens.map(t => t.id)
    await tokenApi.reorderTokens(tokenIds)
  }

  const save = async (data: {
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
      // Editing: never send the masked value back — it would overwrite the
      // real refresh token. Only include `token` when the user typed a new one.
      if (trimmedToken && !isMaskedToken(trimmedToken)) {
        payload.token = trimmedToken
      }
      await tokenApi.updateToken(data.id, payload)
    } else {
      await tokenApi.createToken({
        name: data.name,
        token: trimmedToken,
        premium: data.premium || false,
        valid: data.valid !== false,
      })
    }
    await loadTokens()
  }

  const remove = async (id: number) => {
    await tokenApi.deleteToken(id)
    await loadTokens()
  }

  onMounted(() => {
    loadTokens()
  })

  return { tokens, loading, error, loadTokens, toggleField, reorder, save, remove }
}
