import { ref, onMounted } from 'vue'
import { systemApi } from '../api'
import type { SystemConfig } from '../types'

const systemConfig = ref<SystemConfig | null>(null)
const loading = ref(false)
const error = ref<unknown>(null)

// Dedupe concurrent callers (App + BatchDownloadModal both call useSystem()):
// they share one in-flight request instead of firing duplicate GETs.
let inflight: Promise<SystemConfig | null> | null = null

export function useSystem() {
  const fetchConfig = (): Promise<SystemConfig | null> => {
    if (systemConfig.value) return Promise.resolve(systemConfig.value)
    if (inflight) return inflight

    loading.value = true
    error.value = null
    inflight = (async () => {
      try {
        systemConfig.value = await systemApi.getConfig()
        return systemConfig.value
      } catch (e) {
        error.value = e
        return null
      } finally {
        loading.value = false
        inflight = null
      }
    })()

    return inflight
  }

  onMounted(fetchConfig)

  return { systemConfig, loading, error, fetchConfig }
}
