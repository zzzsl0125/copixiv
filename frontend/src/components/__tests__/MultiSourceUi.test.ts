import { beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, nextTick, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { Novel } from '../../types'
import type { DynamicShelfEntry } from '../../composables/useSources'
import type { FailedNovelListResponse } from '../../types/failedNovels'
import type { SourceItem, SourceShelfDef } from '../../types/sources'

const pixivShelf: SourceShelfDef = {
  section_id: 'source_pixiv',
  label: 'Pixiv',
  keyword: 'source:pixiv;',
  min_like: 0,
  min_text: 0,
  order_by: 'id',
  order_direction: 'DESC',
  icon: 'sparkles',
  intake_action: 'ref_fetch',
  intake_placeholder: '粘贴 Pixiv 小说链接或作品 ID（如 12345678）',
  intake_button_text: '抓取',
}

const esjShelf: SourceShelfDef = {
  section_id: 'esj',
  label: 'ESJ',
  keyword: 'source:esj;',
  min_like: 0,
  min_text: 0,
  order_by: 'id',
  order_direction: 'DESC',
  icon: 'globe',
  intake_action: 'ref_fetch',
  intake_placeholder: '输入 ESJ 书本或章节 URL...',
  intake_button_text: '抓取 ESJ',
}

const localShelf: SourceShelfDef = {
  section_id: 'source_local',
  label: '我的上传',
  keyword: 'source:local;',
  min_like: 0,
  min_text: 0,
  order_by: 'id',
  order_direction: 'DESC',
  icon: 'upload',
  intake_action: 'file_upload',
  intake_placeholder: '',
  intake_button_text: '上传书籍',
}

const defaultSourceItems: SourceItem[] = [
  {
    source: 'pixiv',
    enabled: true,
    config: {},
    credential_count: 1,
    valid_credential_count: 1,
    manifest: {
      source: 'pixiv',
      display_name: 'Pixiv',
      badge_label: '',
      badge_class: '',
      capabilities: ['FETCH_BY_REF', 'SYNC_NOVEL'],
      editable_fields: ['series_index', 'tags', 'create_time'],
      epub_policy: 'strict_placeholders',
      settings_schema: [],
      credential_kinds: [],
      shelf: pixivShelf,
      allow_proxy_config: true,
      enabled_by_default: true,
    },
  },
  {
    source: 'esj',
    enabled: true,
    config: {},
    credential_count: 0,
    valid_credential_count: 0,
    manifest: {
      source: 'esj',
      display_name: 'ESJZone',
      badge_label: 'ESJ',
      badge_class: 'bg-indigo-100 text-indigo-800',
      capabilities: ['FETCH_BY_REF', 'SYNC_NOVEL'],
      editable_fields: ['series_index', 'tags', 'create_time'],
      epub_policy: 'best_effort_external',
      settings_schema: [],
      credential_kinds: [],
      shelf: esjShelf,
      allow_proxy_config: true,
      enabled_by_default: false,
    },
  },
  {
    source: 'local',
    enabled: true,
    config: {},
    credential_count: 0,
    valid_credential_count: 0,
    manifest: {
      source: 'local',
      display_name: '本地上传',
      badge_label: '本地',
      badge_class: 'bg-amber-100 text-amber-800',
      capabilities: ['FILE_UPLOAD'],
      editable_fields: ['title', 'author_id', 'series_id', 'series_index', 'tags', 'create_time'],
      epub_policy: 'container_only',
      settings_schema: [],
      credential_kinds: [],
      shelf: localShelf,
      allow_proxy_config: false,
      enabled_by_default: true,
    },
  },
]

const mocks = vi.hoisted(() => ({
  fetchRefs: vi.fn(),
  failedList: vi.fn<() => Promise<FailedNovelListResponse>>(),
  failedRetry: vi.fn(),
  failedResetCount: vi.fn(),
  failedRetryAll: vi.fn(),
  failedResetAll: vi.fn(),
  confirm: vi.fn(async () => true),
  toast: {
    error: vi.fn(),
    success: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}))

const mockSourcesRef = ref<SourceItem[]>(defaultSourceItems)
const mockShelves = computed<DynamicShelfEntry[]>(() =>
  mockSourcesRef.value
    .filter((item) => item.enabled && item.manifest.shelf != null)
    .map((item) => ({
      source: item.source,
      displayName: item.manifest.display_name,
      shelf: item.manifest.shelf!,
    })),
)

vi.mock('../../composables/useSources', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../composables/useSources')>()
  return {
    ...actual,
    useSources: () => ({
      sources: mockSourcesRef,
      enabledSources: computed(() => mockSourcesRef.value.filter((item) => item.enabled)),
      shelves: mockShelves,
      loading: computed(() => false),
      error: computed(() => null),
      reload: vi.fn(async () => mockSourcesRef.value),
      updateConfig: vi.fn(),
      testConnection: vi.fn(),
      getSource: (s: string) => mockSourcesRef.value.find((item) => item.source === s),
      getManifest: (s: string) => mockSourcesRef.value.find((item) => item.source === s)?.manifest,
      getBadge: (s: string) => actual.sourceBadgeFor(s, mockSourcesRef.value),
    }),
  }
})

vi.mock('../../composables', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../composables')>()
  return {
    ...actual,
    useToast: () => mocks.toast,
    useConfirm: () => ({ confirm: mocks.confirm }),
  }
})

vi.mock('../../api/sources', () => ({
  sourceApi: {
    fetchRefs: mocks.fetchRefs,
  },
}))

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    failedNovelApi: {
      list: mocks.failedList,
      retry: mocks.failedRetry,
      resetCount: mocks.failedResetCount,
      retryAll: mocks.failedRetryAll,
      resetAll: mocks.failedResetAll,
    },
  }
})

import NovelCard from '../features/NovelCard.vue'
import SourceShelfBar from '../features/SourceShelfBar.vue'
import Sidebar from '../Sidebar.vue'
import FailedNovels from '../../views/FailedNovels.vue'

function makeNovel(overrides: Partial<Novel> = {}): Novel {
  return {
    id: -101,
    title: 'Multi Source Novel',
    author_id: -10,
    author_name: '作者A',
    series_id: -20,
    series_name: '系列B',
    series_index: 2,
    like: 120,
    view: 900,
    text: 5600,
    has_epub: 0,
    create_time: '2026-05-01T12:00:00Z',
    source: 'esj',
    can_sync: true,
    external_url: 'https://www.esjzone.cc/forum/100/1004.html',
    is_favourite: false,
    is_special_follow: false,
    tags: ['奇幻'],
    editable_fields: ['series_index', 'tags', 'create_time'],
    ...overrides,
  }
}

describe('Multi-source UI components', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockSourcesRef.value = defaultSourceItems
    mocks.confirm.mockResolvedValue(true)
  })

  it('NovelCard renders ESJ badge, external_url anchor, null external_url fallback, and unknown source fallback badge', () => {
    const esjWrapper = mount(NovelCard, {
      props: {
        novel: makeNovel({
          source: 'esj',
          external_url: 'https://www.esjzone.cc/forum/100/1004.html',
        }),
        isActive: false,
      },
    })

    const esjBadge = esjWrapper.get('[data-testid="badge-esj"]')
    expect(esjBadge.text()).toBe('ESJ')
    expect(esjBadge.classes()).toContain('bg-indigo-100')
    expect(esjBadge.classes()).toContain('text-indigo-800')

    const titleAnchor = esjWrapper.get('a')
    expect(titleAnchor.attributes('href')).toBe('https://www.esjzone.cc/forum/100/1004.html')
    expect(titleAnchor.text()).toBe('Multi Source Novel')

    const noLinkWrapper = mount(NovelCard, {
      props: {
        novel: makeNovel({
          source: 'syosetsu',
          external_url: null,
          title: 'Unknown Source Offline Book',
        }),
        isActive: false,
      },
    })

    expect(noLinkWrapper.find('a').exists()).toBe(false)
    expect(noLinkWrapper.text()).toContain('Unknown Source Offline Book')
    const fallbackBadge = noLinkWrapper.get('[data-testid="badge-syosetsu"]')
    expect(fallbackBadge.text()).toBe('SYOSETSU')
    expect(fallbackBadge.classes()).toContain('bg-gray-100')
    expect(fallbackBadge.classes()).toContain('text-gray-700')
  })

  it('SourceShelfBar supports file_upload emit and ref_fetch submission with multiline/comma refs & force flag', async () => {
    const uploadWrapper = mount(SourceShelfBar, {
      props: {
        source: 'local',
        displayName: '本地上传',
        shelf: localShelf,
      },
    })

    await uploadWrapper.get('[data-testid="shelf-bar-upload"]').trigger('click')
    expect(uploadWrapper.emitted('upload')).toHaveLength(1)

    mocks.fetchRefs.mockResolvedValueOnce({
      task_id: 88,
      source: 'esj',
      ref_count: 3,
    })

    const refWrapper = mount(SourceShelfBar, {
      props: {
        source: 'esj',
        displayName: 'ESJZone',
        shelf: esjShelf,
      },
    })

    const input = refWrapper.get('[data-testid="shelf-bar-ref-input"]')
    expect(input.attributes('placeholder')).toBe('输入 ESJ 书本或章节 URL...')
    expect(refWrapper.get('[data-testid="shelf-bar-ref-submit"]').text()).toContain('抓取 ESJ')

    await input.setValue('https://www.esjzone.cc/detail/100.html\n200, 300')
    await refWrapper.get('[data-testid="shelf-bar-force"]').setValue(true)
    await refWrapper.get('form').trigger('submit')
    await flushPromises()

    expect(mocks.fetchRefs).toHaveBeenCalledWith({
      source: 'esj',
      refs: ['https://www.esjzone.cc/detail/100.html', '200', '300'],
      force: true,
    })
    expect(mocks.toast.success).toHaveBeenCalledWith(expect.stringContaining('#88'))
    expect((input.element as HTMLTextAreaElement).value).toBe('')
    expect(refWrapper.emitted('fetched')).toEqual([[{ task_id: 88, source: 'esj', ref_count: 3 }]])

    mocks.fetchRefs.mockRejectedValueOnce(new Error('无法识别的引用'))
    await input.setValue('bad-ref')
    await refWrapper.get('form').trigger('submit')
    await flushPromises()

    expect(mocks.toast.error).toHaveBeenCalledWith('无法识别的引用')
    expect((input.element as HTMLTextAreaElement).value).toBe('bad-ref')
  })

  it('Sidebar renders two-group navigation (阅览 + dynamic 来源书架) and /sources link without novels collision', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/tasks', component: { template: '<div />' } },
        { path: '/tag-management', component: { template: '<div />' } },
        { path: '/sources', component: { template: '<div />' } },
        { path: '/failed-novels', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(Sidebar, {
      props: { isOpen: true },
      global: { plugins: [router] },
    })

    expect(wrapper.text()).toContain('阅览')
    expect(wrapper.text()).toContain('随机发现')
    expect(wrapper.text()).toContain('来源书架')
    expect(wrapper.text()).toContain('数据源管理')
    expect(wrapper.text()).not.toContain('账号管理')

    expect(wrapper.get('[data-testid="nav-shelf-pixiv"]').text()).toContain('Pixiv')
    expect(wrapper.get('[data-testid="nav-shelf-esj"]').text()).toContain('ESJ')
    expect(wrapper.get('[data-testid="nav-shelf-local"]').text()).toContain('我的上传')
    expect(wrapper.get('[data-testid="nav-sources"]').attributes('href')).toBe('/sources')

    // On '/', 随机发现 is active while Pixiv shelf is NOT active
    expect(wrapper.get('[data-testid="nav-novels"]').classes()).toContain('bg-gray-100')
    expect(wrapper.get('[data-testid="nav-shelf-pixiv"]').classes()).not.toContain('bg-gray-100')

    // Clicking Pixiv shelf navigates to /?keyword=source:pixiv; and highlights Pixiv shelf only
    await wrapper.get('[data-testid="nav-shelf-pixiv"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(router.currentRoute.value.query).toEqual({ keyword: 'source:pixiv;' })
    expect(wrapper.get('[data-testid="nav-shelf-pixiv"]').classes()).toContain('bg-gray-100')
    expect(wrapper.get('[data-testid="nav-novels"]').classes()).not.toContain('bg-gray-100')

    // Clicking ESJ shelf navigates to /?keyword=source:esj;
    await wrapper.get('[data-testid="nav-shelf-esj"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(router.currentRoute.value.query).toEqual({ keyword: 'source:esj;' })
    expect(wrapper.get('[data-testid="nav-shelf-esj"]').classes()).toContain('bg-gray-100')
  })

  it('FailedNovels renders multi-source rows keyed by id with source badge, source_key, and conditional external_url', async () => {
    mocks.failedList.mockResolvedValue({
      total: 2,
      offset: 0,
      limit: 100,
      items: [
        {
          id: 11,
          source: 'esj',
          source_key: '100/1004',
          novel_id: -50,
          title: 'ESJ 章节密码错误',
          error_message: '密码错误',
          failed_times: 2,
          last_failed_at: '2026-05-01T10:00:00Z',
          external_url: 'https://www.esjzone.cc/forum/100/1004.html',
        },
        {
          id: 12,
          source: 'pixiv',
          source_key: '501',
          novel_id: 501,
          title: 'Pixiv 无链接失败记录',
          error_message: '作品已删除',
          failed_times: 3,
          last_failed_at: null,
          external_url: null,
        },
      ],
    })
    mocks.failedRetry.mockResolvedValue({ task_id: 99, matched: 1 })
    mocks.failedResetCount.mockResolvedValue(undefined)

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })

    const wrapper = mount(FailedNovels, {
      global: {
        plugins: [[VueQueryPlugin, { queryClient }]],
        stubs: {
          PageHeader: { template: '<div />' },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const esjRow = wrapper.get('[data-testid="failed-row-11"]')
    expect(esjRow.get('[data-testid="failed-source-badge-11"]').text()).toBe('ESJ')
    const esjLink = esjRow.get('a')
    expect(esjLink.attributes('href')).toBe('https://www.esjzone.cc/forum/100/1004.html')
    expect(esjLink.text()).toContain('#100/1004')

    const pixivRow = wrapper.get('[data-testid="failed-row-12"]')
    expect(pixivRow.get('[data-testid="failed-source-badge-12"]').text()).toBe('Pixiv')
    expect(pixivRow.find('a').exists()).toBe(false)
    expect(pixivRow.text()).toContain('#501')

    // Retry and reset pass row.id (11), not novel_id (-50)
    await esjRow.get('[data-testid="failed-retry-11"]').trigger('click')
    await flushPromises()
    expect(mocks.failedRetry).toHaveBeenCalledWith([11])

    await esjRow.get('[data-testid="failed-reset-11"]').trigger('click')
    await flushPromises()
    expect(mocks.failedResetCount).toHaveBeenCalledWith(11)
    expect(esjRow.text()).toContain('0')

    queryClient.clear()
  })
})
