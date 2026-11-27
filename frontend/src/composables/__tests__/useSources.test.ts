import { defineComponent, h, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SourceCredential, SourceItem, SourceTestResult } from '../../types'
import { queryKeys } from '../../api'
import { flushPromises, mountWithQuery } from '../../test-utils/queryWrapper'

const mocks = vi.hoisted(() => ({
  list: vi.fn<() => Promise<SourceItem[]>>(),
  updateConfig: vi.fn(),
  testConnection: vi.fn<() => Promise<SourceTestResult>>(),
  listCredentials: vi.fn<() => Promise<SourceCredential[]>>(),
  createCredential: vi.fn(),
  updateCredential: vi.fn(),
  deleteCredential: vi.fn(),
  reorderCredentials: vi.fn(),
}))

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    sourceApi: {
      ...actual.sourceApi,
      list: mocks.list,
      updateConfig: mocks.updateConfig,
      testConnection: mocks.testConnection,
      listCredentials: mocks.listCredentials,
      createCredential: mocks.createCredential,
      updateCredential: mocks.updateCredential,
      deleteCredential: mocks.deleteCredential,
      reorderCredentials: mocks.reorderCredentials,
    },
  }
})

import {
  isMaskedSecret,
  sourceBadgeFor,
  useSourceCredentials,
  useSources,
} from '../useSources'

function makeSource(source: string, overrides: Partial<SourceItem> = {}): SourceItem {
  const defaultDisplay: Record<string, string> = {
    pixiv: 'Pixiv',
    esj: 'ESJZone',
    local: '本地上传',
  }
  return {
    source,
    enabled: true,
    config: {},
    credential_count: 0,
    valid_credential_count: 0,
    manifest: {
      source,
      display_name: defaultDisplay[source] ?? source.toUpperCase(),
      badge_label: source === 'pixiv' ? '' : source.toUpperCase(),
      badge_class: '',
      allow_proxy_config: true,
      enabled_by_default: true,
      epub_policy: 'container_only',
      capabilities: [],
      editable_fields: [],
      settings_schema: [],
      credential_kinds: [],
      shelf: null,
    },
    ...overrides,
  }
}

function makeCred(id: number, overrides: Partial<SourceCredential> = {}): SourceCredential {
  return {
    id,
    source: 'pixiv',
    name: `Account ${id}`,
    kind: 'refresh_token',
    secret: { refresh_token: `****000${id}` },
    valid: true,
    sort_index: id - 1,
    meta: { premium: false, is_follow: false },
    ...overrides,
  }
}

describe('sourceBadgeFor & isMaskedSecret', () => {
  it('resolves badge metadata from manifests with cold-load fallbacks', () => {
    const items: SourceItem[] = [
      makeSource('pixiv'),
      makeSource('esj', {
        manifest: {
          ...makeSource('esj').manifest,
          display_name: 'ESJZone',
          badge_label: 'ESJ',
          badge_class: 'bg-indigo-100 text-indigo-800',
        },
      }),
      makeSource('custom', {
        manifest: {
          ...makeSource('custom').manifest,
          display_name: '自定义源',
          badge_label: 'CST',
          badge_class: '',
        },
      }),
    ]

    expect(sourceBadgeFor('pixiv', items)).toBeNull()
    expect(sourceBadgeFor('esj', items)).toEqual({
      label: 'ESJ',
      class: 'bg-indigo-100 text-indigo-800',
      title: 'ESJZone（source=esj）',
    })
    expect(sourceBadgeFor('custom', items)).toEqual({
      label: 'CST',
      class: 'bg-gray-100 text-gray-700',
      title: '自定义源（source=custom）',
    })

    // Cold-load / unregistered fallbacks
    expect(sourceBadgeFor('pixiv')).toBeNull()
    expect(sourceBadgeFor('local')).toEqual({
      label: '本地',
      class: 'bg-amber-100 text-amber-800',
      title: '本地上传（source=local）',
    })
    expect(sourceBadgeFor('esj')).toEqual({
      label: 'ESJ',
      class: 'bg-indigo-100 text-indigo-800',
      title: 'ESJZone（source=esj）',
    })
    expect(sourceBadgeFor('novelpia')).toEqual({
      label: 'NOVELPIA',
      class: 'bg-gray-100 text-gray-700',
      title: 'source=novelpia',
    })
    expect(sourceBadgeFor('')).toBeNull()
  })

  it('recognizes backend-masked secrets', () => {
    expect(isMaskedSecret('****')).toBe(true)
    expect(isMaskedSecret('****abcd')).toBe(true)
    expect(isMaskedSecret('****12')).toBe(false)
    expect(isMaskedSecret('raw_secret_token')).toBe(false)
  })
})

describe('useSources & useSourceCredentials', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('returns safe reactive fallbacks when mounted without VueQueryPlugin', async () => {
    let sourcesResult!: ReturnType<typeof useSources>
    let credsResult!: ReturnType<typeof useSourceCredentials>

    const Standalone = defineComponent({
      setup() {
        sourcesResult = useSources()
        credsResult = useSourceCredentials('pixiv')
        return () => h('div')
      },
    })

    const wrapper = mount(Standalone)
    expect(sourcesResult.sources.value).toEqual([])
    expect(sourcesResult.enabledSources.value).toEqual([])
    expect(sourcesResult.shelves.value).toEqual([])
    expect(sourcesResult.loading.value).toBe(false)
    expect(sourcesResult.error.value).toBeNull()
    expect(sourcesResult.getSource('local')).toBeUndefined()
    expect(sourcesResult.getManifest('local')).toBeUndefined()
    expect(sourcesResult.getBadge('local')?.label).toBe('本地')
    expect(sourcesResult.getBadge('pixiv')).toBeNull()
    await expect(sourcesResult.reload()).resolves.toEqual([])
    await expect(
      sourcesResult.updateConfig('pixiv', { enabled: false }),
    ).rejects.toThrow()
    await expect(sourcesResult.testConnection('pixiv')).rejects.toThrow()

    expect(credsResult.credentials.value).toEqual([])
    expect(credsResult.loading.value).toBe(false)
    expect(credsResult.error.value).toBeNull()
    await expect(credsResult.reload()).resolves.toEqual([])
    await expect(
      credsResult.create({ name: 'a', valid: true }),
    ).rejects.toThrow()

    expect(mocks.list).not.toHaveBeenCalled()
    expect(mocks.listCredentials).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('loads sources, derives enabled shelves, updates config, and tests connection', async () => {
    const pixivItem = makeSource('pixiv', {
      enabled: true,
      manifest: {
        ...makeSource('pixiv').manifest,
        shelf: {
          section_id: 'source_pixiv',
          label: 'Pixiv',
          keyword: 'source:pixiv;',
          min_like: 0,
          min_text: 0,
          order_by: 'id',
          order_direction: 'DESC',
          icon: 'sparkles',
          intake_action: 'ref_fetch',
          intake_placeholder: '',
          intake_button_text: '抓取',
        },
      },
    })
    const esjDisabled = makeSource('esj', {
      enabled: false,
      manifest: {
        ...makeSource('esj').manifest,
        display_name: 'ESJZone',
        badge_label: 'ESJ',
        badge_class: 'bg-indigo-100 text-indigo-800',
        shelf: {
          section_id: 'esj',
          label: 'ESJ',
          keyword: 'source:esj;',
          min_like: 0,
          min_text: 0,
          order_by: 'id',
          order_direction: 'DESC',
          icon: 'globe',
          intake_action: 'ref_fetch',
          intake_placeholder: '',
          intake_button_text: '抓取 ESJ',
        },
      },
    })
    const esjEnabled = { ...esjDisabled, enabled: true }

    mocks.list.mockResolvedValue([pixivItem, esjDisabled])
    mocks.updateConfig.mockResolvedValue(esjEnabled)
    mocks.testConnection.mockResolvedValue({
      ok: true,
      message: '连接正常',
      latency_ms: 42,
      account_summary: 'user@example',
    })

    const { result, queryClient, wrapper } = await mountWithQuery(() => useSources())
    await flushPromises()

    expect(mocks.list).toHaveBeenCalledTimes(1)
    expect(result.sources.value).toHaveLength(2)
    expect(result.enabledSources.value.map((s) => s.source)).toEqual(['pixiv'])
    expect(result.shelves.value).toEqual([
      {
        source: 'pixiv',
        displayName: 'Pixiv',
        shelf: pixivItem.manifest.shelf,
      },
    ])
    expect(result.getManifest('esj')?.display_name).toBe('ESJZone')
    expect(result.getBadge('esj')?.label).toBe('ESJ')

    // Verify staleTime = 30_000 on sources query
    const queries = queryClient.getQueryCache().findAll({ queryKey: queryKeys.sources() })
    expect(queries).toHaveLength(1)
    expect(queries[0]!.observers[0]!.options.staleTime).toBe(30_000)

    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')
    mocks.list.mockResolvedValue([pixivItem, esjEnabled])

    await result.updateConfig('esj', { enabled: true })
    await flushPromises()

    expect(mocks.updateConfig).toHaveBeenCalledWith('esj', { enabled: true })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.sources() })
    expect(result.enabledSources.value.map((s) => s.source)).toEqual(['pixiv', 'esj'])
    expect(result.shelves.value.map((s) => s.source)).toEqual(['pixiv', 'esj'])

    const testRes = await result.testConnection('esj')
    expect(testRes.ok).toBe(true)
    expect(mocks.testConnection).toHaveBeenCalledWith('esj')

    await result.reload()
    expect(mocks.list).toHaveBeenCalledTimes(3)

    wrapper.unmount()
  })

  it('surfaces formatted error message when listing sources fails', async () => {
    mocks.list.mockRejectedValue(new Error('网络不可达'))

    const { result, wrapper } = await mountWithQuery(() => useSources())
    await flushPromises()

    expect(result.error.value).toBe('网络不可达')
    expect(result.sources.value).toEqual([])
    wrapper.unmount()
  })

  it('manages source credentials, reacts to source switches, and invalidates both keys', async () => {
    const activeSource = ref('pixiv')
    const pixivCreds = [makeCred(1), makeCred(2)]
    const esjCreds = [makeCred(10, { source: 'esj', kind: 'cookie', name: 'Cookie 1' })]

    mocks.listCredentials.mockImplementation((src?: string) => {
      return Promise.resolve(src === 'esj' ? esjCreds : pixivCreds)
    })
    mocks.createCredential.mockResolvedValue(makeCred(3))
    mocks.updateCredential.mockResolvedValue(makeCred(1, { valid: false }))
    mocks.deleteCredential.mockResolvedValue(undefined)
    mocks.reorderCredentials.mockResolvedValue(undefined)

    const { result, queryClient, wrapper } = await mountWithQuery(() =>
      useSourceCredentials(activeSource),
    )
    await flushPromises()

    expect(mocks.listCredentials).toHaveBeenCalledWith('pixiv', expect.any(AbortSignal))
    expect(result.credentials.value.map((c) => c.id)).toEqual([1, 2])

    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    await result.create({
      name: 'New Acc',
      kind: 'refresh_token',
      secret: { refresh_token: 'abc' },
      valid: true,
    })
    expect(mocks.createCredential).toHaveBeenCalledWith('pixiv', {
      name: 'New Acc',
      kind: 'refresh_token',
      secret: { refresh_token: 'abc' },
      valid: true,
    })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: queryKeys.sourceCredentials('pixiv'),
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.sources() })

    await result.update(1, { valid: false })
    expect(mocks.updateCredential).toHaveBeenCalledWith('pixiv', 1, { valid: false })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: queryKeys.sourceCredentials('pixiv'),
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.sources() })

    await result.remove(2)
    expect(mocks.deleteCredential).toHaveBeenCalledWith('pixiv', 2)
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: queryKeys.sourceCredentials('pixiv'),
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.sources() })

    // Reorder optimistic update & rollback on error
    mocks.reorderCredentials.mockRejectedValueOnce(new Error('排序失败'))
    await expect(result.reorder([pixivCreds[1]!, pixivCreds[0]!])).rejects.toThrow(
      '排序失败',
    )
    await flushPromises()
    expect(result.credentials.value.map((c) => c.id)).toEqual([1, 2])

    // Switching active source fetches new credentials
    activeSource.value = 'esj'
    await nextTick()
    await flushPromises()

    expect(mocks.listCredentials).toHaveBeenCalledWith('esj', expect.any(AbortSignal))
    expect(result.credentials.value.map((c) => c.id)).toEqual([10])

    wrapper.unmount()
  })
})
