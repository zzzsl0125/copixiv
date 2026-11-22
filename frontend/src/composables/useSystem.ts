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

  const updateConfigMutation = useMutation({
    mutationFn: (patch: { exclude_blocked_tag_novels: boolean }) => systemApi.updateConfig(patch),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.systemConfig(), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.novelsAll() })
    },
  })

  const updateConfig = (patch: { exclude_blocked_tag_novels: boolean }) =>
    updateConfigMutation.mutateAsync(patch)

  return {
    systemConfig,
    updateConfig,
    isUpdatingConfig: computed(() => updateConfigMutation.isPending.value),
  }
}
