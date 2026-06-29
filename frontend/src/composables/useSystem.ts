import { ref, onMounted } from 'vue'
import { systemApi } from '../api'
import type { SystemConfig } from '../types'

const systemConfig = ref<SystemConfig | null>(null)
const loading = ref(false)
const error = ref<unknown>(null)

export function useSystem() {
  const fetchConfig = async () => {
    if (systemConfig.value) return

    loading.value = true
    error.value = null
    try {
      systemConfig.value = await systemApi.getConfig()
    } catch (e) {
      error.value = e
    } finally {
      loading.value = false
    }
  }

  onMounted(fetchConfig)

  return { systemConfig, loading, error, fetchConfig }
}
