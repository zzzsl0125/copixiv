import { ref, onMounted } from 'vue'
import { tokenApi } from '../api'
import type { Token } from '../types'

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
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } }
        error.value = axiosErr.response?.data?.detail || '获取 Token 列表失败'
      } else if (err instanceof Error) {
        error.value = err.message
      } else {
        error.value = '获取 Token 列表失败'
      }
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
    if (data.id) {
      await tokenApi.updateToken(data.id, {
        name: data.name,
        token: data.token,
        premium: data.premium,
        valid: data.valid,
      })
    } else {
      await tokenApi.createToken({
        name: data.name,
        token: data.token,
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
