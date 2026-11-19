import { computed } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { queryKeys, systemApi } from '../api'
import type { SystemConfig } from '../types'

export function useSystem() {
  const queryClient = useQueryClient()

  const query = useQuery({
    queryKey: queryKeys.systemConfig(),
    queryFn: ({ signal }) => systemApi.getConfig(signal),
    staleTime: 5 * 60_000,
  })

  const systemConfig = computed<SystemConfig | null>(() => query.data.value ?? null)
  const loading = computed(() => query.isPending.value)
  const error = computed(() => query.error.value ?? null)
  const isConfigReady = computed(() => !query.isPending.value)

  const updateConfigMutation = useMutation({
    mutationFn: (patch: Partial<SystemConfig>) => systemApi.updateConfig(patch),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.systemConfig(), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.novelsAll() })
    },
  })

  const fetchConfig = async (): Promise<SystemConfig | null> => {
    if (query.data.value) return query.data.value
    const result = await query.refetch()
    return result.data ?? null
  }

  const updateConfig = (patch: Partial<SystemConfig>) => updateConfigMutation.mutateAsync(patch)

  return {
    systemConfig,
    loading,
    error,
    isConfigReady,
    fetchConfig,
    updateConfig,
    isUpdatingConfig: computed(() => updateConfigMutation.isPending.value),
  }
}
