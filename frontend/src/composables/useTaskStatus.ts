import { computed } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { queryKeys, taskApi } from '../api'

const POLL_MS = 5000

/**
 * Shared busy-task status powered by `@tanstack/vue-query`.
 *
 * Fetches once on mount and automatically polls every 5s only while at least
 * one task function is pending/running on the server (`funcs.length > 0`).
 */
export function useTaskStatus() {
  const queryClient = useQueryClient()

  const query = useQuery({
    queryKey: queryKeys.tasksBusy(),
    queryFn: ({ signal }) => taskApi.getBusyTasks(signal),
    refetchInterval: (q) => (q.state.data?.funcs?.length ? POLL_MS : false),
    refetchIntervalInBackground: false,
    staleTime: 0,
  })

  const busyFuncNames = computed(() => query.data.value?.funcs ?? [])
  const busyFuncs = computed(() => new Set(busyFuncNames.value))

  function isBusy(funcName?: string | null): boolean {
    return !!funcName && busyFuncs.value.has(funcName)
  }

  function refreshTaskStatus() {
    return query.refetch()
  }

  function notifyTaskEnqueued(funcName: string): void {
    if (funcName) {
      queryClient.setQueryData<{ funcs: string[] }>(queryKeys.tasksBusy(), (prev) => {
        const current = prev?.funcs ?? []
        if (current.includes(funcName)) return prev ?? { funcs: current }
        return { funcs: [...current, funcName] }
      })
    }
    void queryClient.invalidateQueries({ queryKey: queryKeys.tasksBusy() })
  }

  return {
    busyFuncs,
    busyFuncNames,
    dataUpdatedAt: query.dataUpdatedAt,
    isBusy,
    refreshTaskStatus,
    notifyTaskEnqueued,
  }
}
