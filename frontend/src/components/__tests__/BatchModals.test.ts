import { nextTick, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  batchOperation: vi.fn(),
  batchDownloadPreview: vi.fn(),
  batchDownload: vi.fn(),
}))

const toastMocks = vi.hoisted(() => ({
  warning: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
}))

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    novelApi: {
      ...actual.novelApi,
      batchOperation: apiMocks.batchOperation,
      batchDownloadPreview: apiMocks.batchDownloadPreview,
      batchDownload: apiMocks.batchDownload,
    },
  }
})

vi.mock('../../composables', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../composables')>()
  return {
    ...actual,
    useToast: () => toastMocks,
    useSystem: () => ({
      systemConfig: ref({ batch_download_naming: '{title}' }),
    }),
  }
})

vi.mock('../../lib/utils', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/utils')>()
  return {
    ...actual,
    downloadBlob: vi.fn(),
  }
})

import BatchTagModal from '../features/BatchTagModal.vue'
import BatchDownloadModal from '../features/BatchDownloadModal.vue'

describe('BatchTagModal & BatchDownloadModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('BatchTagModal splits unified separators and enforces BATCH_MAX_TAGS = 20', async () => {
    const wrapper = mount(BatchTagModal, {
      props: {
        isOpen: true,
        operation: 'add_tags',
        scope: { mode: 'ids', novel_ids: [1], excluded_ids: [] },
        scopeLabel: '已勾选的 1 篇',
      },
      global: {
        stubs: { teleport: true },
      },
    })

    const input = wrapper.get('[data-testid="batch-tag-input"]')
    await input.setValue('a, b，c/d#e、f')
    await nextTick()
    expect(wrapper.find('[data-testid="batch-tag-error"]').exists()).toBe(false)

    // 21 tags should show validation error and disable confirm
    const twentyOne = Array.from({ length: 21 }, (_, i) => `t${i + 1}`).join(' ')
    await input.setValue(twentyOne)
    await nextTick()

    expect(wrapper.get('[data-testid="batch-tag-error"]').text()).toContain('20')
    const confirmBtn = wrapper.findAll('button').find((b) => b.text().includes('确认添加'))
    expect(confirmBtn?.attributes('disabled')).toBeDefined()

    wrapper.unmount()
  })

  it('BatchDownloadModal sends a single preview request on open and warns on missingIds', async () => {
    apiMocks.batchDownloadPreview.mockResolvedValue({ path: 'preview.txt' })
    apiMocks.batchDownload.mockResolvedValue({
      blob: new Blob(['zip']),
      contentDisposition: 'attachment; filename="test.zip"',
      missingIds: ['101', '102'],
    })

    const wrapper = mount(BatchDownloadModal, {
      props: {
        isOpen: true,
        keyword: '百合',
        order_by: 'like',
        order_direction: 'DESC',
        min_like: 500,
        min_text: 3000,
        novelIds: [101, 102, 103],
      },
      global: {
        stubs: { teleport: true },
      },
    })

    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    await nextTick()

    // Must fire only once on open (not twice from watch + debounce)
    expect(apiMocks.batchDownloadPreview).toHaveBeenCalledTimes(1)
    // Misleading sort/filter rows are hidden in ids mode
    expect(wrapper.text()).not.toContain('排序：')
    expect(wrapper.text()).not.toContain('筛选：')

    await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
    await flushPromises()

    expect(toastMocks.warning).toHaveBeenCalledWith(expect.stringContaining('2 篇小说文件缺失'))
    wrapper.unmount()
  })
})
