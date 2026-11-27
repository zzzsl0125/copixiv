import { computed, toValue, type MaybeRefOrGetter } from 'vue'
import { type QueryClient, useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { getApiErrorMessage, queryKeys, sourceApi } from '../api'
import type {
  SourceConfigUpdate,
  SourceCredential,
  SourceCredentialCreate,
  SourceCredentialUpdate,
  SourceItem,
  SourceManifest,
  SourceShelfDef,
  SourceTestResult,
} from '../types'

export interface SourceBadge {
  label: string
  class: string
  title: string
}

export interface DynamicShelfEntry {
  source: string
  displayName: string
  shelf: SourceShelfDef
}

/** Backend masks secret fields as ``****`` (<=4 chars) or ``****`` + last 4 chars. */
export const isMaskedSecret = (value: string): boolean =>
  value === '****' || /^\*{4}.{4}$/.test(value)

function tryUseQueryClient(): QueryClient | null {
  try {
    return useQueryClient()
  } catch {
    return null
  }
}

/**
 * Resolve display badge metadata for a novel/entity source from loaded source
 * manifests, falling back to known cold-load defaults before `/api/sources` settles.
 */
export function sourceBadgeFor(
  source: string,
  items?: readonly SourceItem[],
): SourceBadge | null {
  if (!source) return null
  const matched = items?.find((item) => item.source === source)
  if (matched) {
    if (!matched.manifest.badge_label) return null
    return {
      label: matched.manifest.badge_label,
      class: matched.manifest.badge_class || 'bg-gray-100 text-gray-700',
      title: `${matched.manifest.display_name}（source=${source}）`,
    }
  }
  if (source === 'pixiv') return null
  if (source === 'local') {
    return {
      label: '本地',
      class: 'bg-amber-100 text-amber-800',
      title: '本地上传（source=local）',
    }
  }
  if (source === 'esj') {
    return {
      label: 'ESJ',
      class: 'bg-indigo-100 text-indigo-800',
      title: 'ESJZone（source=esj）',
    }
  }
  return {
    label: source.toUpperCase(),
    class: 'bg-gray-100 text-gray-700',
    title: `source=${source}`,
  }
}

export function useSources() {
  const queryClient = tryUseQueryClient()

  if (!queryClient) {
    const emptySources = computed<SourceItem[]>(() => [])
    return {
      sources: emptySources,
      enabledSources: emptySources,
      shelves: computed<DynamicShelfEntry[]>(() => []),
      loading: computed(() => false),
      error: computed<string | null>(() => null),
      reload: async (): Promise<SourceItem[]> => [],
      updateConfig: async (
        _source: string,
        _body: SourceConfigUpdate,
      ): Promise<SourceItem> => {
        throw new Error('VueQueryPlugin is required to update source config')
      },
      testConnection: async (_source: string): Promise<SourceTestResult> => {
        throw new Error('VueQueryPlugin is required to test source connection')
      },
      getSource: (_source: string): SourceItem | undefined => undefined,
      getManifest: (_source: string): SourceManifest | undefined => undefined,
      getBadge: (source: string): SourceBadge | null => sourceBadgeFor(source),
    }
  }

  const query = useQuery({
    queryKey: queryKeys.sources(),
    queryFn: ({ signal }) => sourceApi.list(signal),
    staleTime: 30_000,
  })

  const sources = computed<SourceItem[]>(() => query.data.value ?? [])
  const enabledSources = computed<SourceItem[]>(() =>
    sources.value.filter((item) => item.enabled),
  )
  const shelves = computed<DynamicShelfEntry[]>(() =>
    enabledSources.value
      .filter(
        (
          item,
        ): item is SourceItem & {
          manifest: SourceManifest & { shelf: SourceShelfDef }
        } => item.manifest.shelf != null,
      )
      .map((item) => ({
        source: item.source,
        displayName: item.manifest.display_name,
        shelf: item.manifest.shelf,
      })),
  )

  const loading = computed(() => query.isPending.value)
  const error = computed<string | null>(() =>
    query.error.value ? getApiErrorMessage(query.error.value, '获取数据源列表失败') : null,
  )

  const getSource = (source: string): SourceItem | undefined =>
    sources.value.find((item) => item.source === source)

  const getManifest = (source: string): SourceManifest | undefined =>
    getSource(source)?.manifest

  const getBadge = (source: string): SourceBadge | null =>
    sourceBadgeFor(source, sources.value)

  const updateConfigMutation = useMutation({
    mutationFn: ({ source, body }: { source: string; body: SourceConfigUpdate }) =>
      sourceApi.updateConfig(source, body),
    onSuccess: (updated) => {
      queryClient.setQueryData<SourceItem[]>(queryKeys.sources(), (prev) =>
        prev
          ? prev.map((item) => (item.source === updated.source ? updated : item))
          : [updated],
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.sources() })
    },
  })

  const testMutation = useMutation({
    mutationFn: (source: string) => sourceApi.testConnection(source),
  })

  return {
    sources,
    enabledSources,
    shelves,
    loading,
    error,
    reload: async (): Promise<SourceItem[]> => (await query.refetch()).data ?? [],
    updateConfig: (source: string, body: SourceConfigUpdate): Promise<SourceItem> =>
      updateConfigMutation.mutateAsync({ source, body }),
    testConnection: (source: string): Promise<SourceTestResult> =>
      testMutation.mutateAsync(source),
    getSource,
    getManifest,
    getBadge,
  }
}

function applyCredentialOrder(
  previous: SourceCredential[] | undefined,
  itemsOrIds: SourceCredential[] | number[],
): SourceCredential[] | undefined {
  if (itemsOrIds.length === 0) return []
  if (typeof itemsOrIds[0] !== 'number') {
    return itemsOrIds as SourceCredential[]
  }
  if (!previous) return previous
  const byId = new Map(previous.map((item) => [item.id, item]))
  return (itemsOrIds as number[])
    .map((id) => byId.get(id))
    .filter((item): item is SourceCredential => item !== undefined)
}

export function useSourceCredentials(
  source: MaybeRefOrGetter<string>,
  options?: { enabled?: MaybeRefOrGetter<boolean> },
) {
  const queryClient = tryUseQueryClient()
  const sourceKey = computed(() => toValue(source))

  if (!queryClient) {
    return {
      credentials: computed<SourceCredential[]>(() => []),
      loading: computed(() => false),
      error: computed<string | null>(() => null),
      reload: async (): Promise<SourceCredential[]> => [],
      create: async (_body: SourceCredentialCreate): Promise<SourceCredential> => {
        throw new Error('VueQueryPlugin is required to create source credentials')
      },
      update: async (
        _credId: number,
        _body: SourceCredentialUpdate,
      ): Promise<SourceCredential> => {
        throw new Error('VueQueryPlugin is required to update source credentials')
      },
      remove: async (_credId: number): Promise<void> => {
        throw new Error('VueQueryPlugin is required to delete source credentials')
      },
      reorder: async (_itemsOrIds: SourceCredential[] | number[]): Promise<void> => {
        throw new Error('VueQueryPlugin is required to reorder source credentials')
      },
    }
  }

  const queryEnabled = computed(
    () =>
      Boolean(sourceKey.value) &&
      (options?.enabled === undefined || Boolean(toValue(options.enabled))),
  )

  const query = useQuery({
    queryKey: computed(() => queryKeys.sourceCredentials(sourceKey.value)),
    queryFn: ({ signal }) => sourceApi.listCredentials(sourceKey.value, signal),
    enabled: queryEnabled,
  })

  const credentials = computed<SourceCredential[]>(() => query.data.value ?? [])
  const loading = computed(() => query.isPending.value)
  const error = computed<string | null>(() =>
    query.error.value ? getApiErrorMessage(query.error.value, '获取凭据列表失败') : null,
  )

  const invalidate = (targetSource: string) => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.sourceCredentials(targetSource),
    })
    void queryClient.invalidateQueries({ queryKey: queryKeys.sources() })
  }

  const createMutation = useMutation({
    mutationFn: ({ target, body }: { target: string; body: SourceCredentialCreate }) =>
      sourceApi.createCredential(target, body),
    onSuccess: (_data, { target }) => invalidate(target),
  })

  const updateMutation = useMutation({
    mutationFn: ({
      target,
      credId,
      body,
    }: {
      target: string
      credId: number
      body: SourceCredentialUpdate
    }) => sourceApi.updateCredential(target, credId, body),
    onSuccess: (_data, { target }) => invalidate(target),
  })

  const removeMutation = useMutation({
    mutationFn: ({ target, credId }: { target: string; credId: number }) =>
      sourceApi.deleteCredential(target, credId),
    onSuccess: (_data, { target }) => invalidate(target),
  })

  const reorderMutation = useMutation({
    mutationFn: ({
      target,
      itemsOrIds,
    }: {
      target: string
      itemsOrIds: SourceCredential[] | number[]
    }) =>
      sourceApi.reorderCredentials(
        target,
        itemsOrIds.map((item) => (typeof item === 'number' ? item : item.id)),
      ),
    onMutate: async ({ target, itemsOrIds }) => {
      const key = queryKeys.sourceCredentials(target)
      await queryClient.cancelQueries({ queryKey: key })
      const previous = queryClient.getQueryData<SourceCredential[]>(key)
      const next = applyCredentialOrder(previous, itemsOrIds)
      if (next !== undefined) {
        queryClient.setQueryData(key, next)
      }
      return { target, previous }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.previous !== undefined) {
        queryClient.setQueryData(
          queryKeys.sourceCredentials(ctx.target),
          ctx.previous,
        )
      }
    },
    onSettled: (_data, _err, { target }) => invalidate(target),
  })

  return {
    credentials,
    loading,
    error,
    reload: async (): Promise<SourceCredential[]> => (await query.refetch()).data ?? [],
    create: (body: SourceCredentialCreate): Promise<SourceCredential> =>
      createMutation.mutateAsync({ target: sourceKey.value, body }),
    update: (credId: number, body: SourceCredentialUpdate): Promise<SourceCredential> =>
      updateMutation.mutateAsync({ target: sourceKey.value, credId, body }),
    remove: (credId: number): Promise<void> =>
      removeMutation.mutateAsync({ target: sourceKey.value, credId }),
    reorder: (itemsOrIds: SourceCredential[] | number[]): Promise<void> =>
      reorderMutation.mutateAsync({ target: sourceKey.value, itemsOrIds }),
  }
}
