import { nextTick, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  batchOperation: vi.fn(),
  batchExportPreview: vi.fn(),
  submitBatchExport: vi.fn(),
}))

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    novelApi: {
      ...actual.novelApi,
      batchOperation: apiMocks.batchOperation,
      batchExportPreview: apiMocks.batchExportPreview,
      submitBatchExport: apiMocks.submitBatchExport,
    },
  }
})

vi.mock('../../composables', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../composables')>()
  return {
    ...actual,
    useSystem: () => ({
      systemConfig: ref({ batch_download_naming: '{title}' }),
    }),
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
        scope: { novel_ids: [1] },
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

  it('BatchDownloadModal sends a single preview request on open and submits batch export task', async () => {
    apiMocks.batchExportPreview.mockResolvedValue({ path: 'preview.txt' })
    apiMocks.submitBatchExport.mockResolvedValue({ task_id: 42, matched: 3 })

    const wrapper = mount(BatchDownloadModal, {
      props: {
        isOpen: true,
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
    expect(apiMocks.batchExportPreview).toHaveBeenCalledTimes(1)
    expect(apiMocks.batchExportPreview).toHaveBeenCalledWith(
      {
        novel_ids: [101, 102, 103],
        format_mode: 'txt',
        naming_template: '{title}',
      },
      expect.any(AbortSignal),
    )
    expect(wrapper.text()).toContain('preview.txt')
    expect(wrapper.text()).not.toContain('排序：')
    expect(wrapper.text()).not.toContain('筛选：')

    await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
    await flushPromises()

    expect(apiMocks.submitBatchExport).toHaveBeenCalledWith({
      novel_ids: [101, 102, 103],
      format_mode: 'txt',
      zip_name: '批量导出',
      naming_template: '{title}',
    })
    expect(wrapper.emitted('task-submitted')).toEqual([[{ task_id: 42, matched: 3 }]])
    expect(wrapper.emitted('close')).toEqual([[]])
    wrapper.unmount()
  })

  it('BatchDownloadModal disables submit and skips preview when novelIds is empty', async () => {
    const wrapper = mount(BatchDownloadModal, {
      props: {
        isOpen: true,
        novelIds: [],
      },
      global: {
        stubs: { teleport: true },
      },
    })

    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    await nextTick()

    expect(apiMocks.batchExportPreview).not.toHaveBeenCalled()
    const submitBtn = wrapper.get('button[type="submit"]')
    expect((submitBtn.element as HTMLButtonElement).disabled).toBe(true)
    wrapper.unmount()
  })
})
