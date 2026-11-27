import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import type { UploadResponse } from '../../types'

const mocks = vi.hoisted(() => ({
  uploadFiles: vi.fn<
    (
      files: File[],
      onUploadProgress?: (percent: number) => void,
      authorId?: number | null,
    ) => Promise<UploadResponse>
  >(),
  lookupAuthor: vi.fn(),
  toastSuccess: vi.fn(),
  toastInfo: vi.fn(),
  toastWarning: vi.fn(),
}))

vi.mock('../../api', () => ({
  libraryApi: {
    uploadFiles: mocks.uploadFiles,
  },
  novelApi: {
    lookupAuthor: mocks.lookupAuthor,
  },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

vi.mock('../../composables', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../composables')>()
  return {
    ...actual,
    useToast: () => ({
      success: mocks.toastSuccess,
      info: mocks.toastInfo,
      warning: mocks.toastWarning,
      error: vi.fn(),
    }),
  }
})

import UploadModal from '../features/UploadModal.vue'

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

describe('UploadModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.lookupAuthor.mockResolvedValue({ id: 42, name: 'TestAuthor' })
  })

  it('keeps presetIdNumber reactive across collapsible unmount and omits disabled series placeholder', async () => {
    const wrapper = mount(UploadModal, {
      props: { isOpen: true },
      global: { stubs: { teleport: true } },
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="upload-preset-series-disabled"]').exists()).toBe(false)

    // Pick a file so confirm button only depends on presetInvalid
    const fileInput = wrapper.find('[data-testid="upload-file-input"]')
    const file = new File(['hello'], 'book.txt', { type: 'text/plain' })
    Object.defineProperty(fileInput.element, 'files', { value: [file], configurable: true })
    await fileInput.trigger('change')
    await nextTick()

    const confirmBtn = () =>
      wrapper.findAll('button').find((b) => b.text().trim() === '上传')!
    expect(confirmBtn().attributes('disabled')).toBeUndefined()

    // Open preset section, enter invalid ID, collapse (unmount-on-hide), then reopen/reset
    await wrapper.find('[data-testid="upload-preset-toggle"]').trigger('click')
    await nextTick()
    const authorInput = wrapper.find('[data-testid="upload-preset-author-input"]')
    await authorInput.setValue('abc')
    await nextTick()
    expect(confirmBtn().attributes('disabled')).toBeDefined()
    // No duplicate error paragraph from UploadModal itself
    expect(wrapper.find('[data-testid="upload-preset-author-error"]').exists()).toBe(false)

    // Collapse collapsible, then close & reopen modal -> reset clears presetAuthorId while unmounted
    await wrapper.find('[data-testid="upload-preset-toggle"]').trigger('click')
    await nextTick()
    await wrapper.setProps({ isOpen: false })
    await wrapper.setProps({ isOpen: true })
    await nextTick()

    Object.defineProperty(fileInput.element, 'files', { value: [file], configurable: true })
    await fileInput.trigger('change')
    await nextTick()
    expect(confirmBtn().attributes('disabled')).toBeUndefined()

    wrapper.unmount()
  })

  it('shows clean batch progress label and emits uploaded when duplicate TXT updates preset author', async () => {
    let resolveUpload!: (res: UploadResponse) => void
    mocks.uploadFiles.mockImplementation((_files, onProgress) => {
      onProgress?.(45)
      return new Promise((resolve) => {
        resolveUpload = resolve
      })
    })

    const wrapper = mount(UploadModal, {
      props: { isOpen: true },
      global: { stubs: { teleport: true } },
    })
    await flushPromises()

    // Set preset author
    await wrapper.find('[data-testid="upload-preset-toggle"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="upload-preset-author-input"]').setValue('42')
    await nextTick()

    // Pick 2 files
    const fileInput = wrapper.find('[data-testid="upload-file-input"]')
    const f1 = new File(['a'], 'a.txt', { type: 'text/plain' })
    const f2 = new File(['b'], 'b.txt', { type: 'text/plain' })
    Object.defineProperty(fileInput.element, 'files', { value: [f1, f2], configurable: true })
    await fileInput.trigger('change')
    await nextTick()

    await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
    await nextTick()

    const progress = wrapper.find('[data-testid="upload-progress"]')
    expect(progress.exists()).toBe(true)
    expect(progress.text()).toContain('2 个文件 · 45%')
    expect(progress.text()).not.toContain('0/2')

    resolveUpload({
      results: [
        { filename: 'a.txt', status: 'duplicate', novel_id: -1, title: 'A', author_name: 'TestAuthor' },
        { filename: 'b.txt', status: 'duplicate', novel_id: -2, title: 'B', author_name: 'TestAuthor' },
      ],
      created: 0,
      duplicate: 2,
      failed: 0,
    })
    await flushPromises()

    expect(wrapper.emitted('uploaded')).toEqual([[{ created: 0, duplicate: 2, failed: 0 }]])
    wrapper.unmount()
  })

  it('accepts negative preset author ID and passes it to uploadFiles', async () => {
    mocks.uploadFiles.mockResolvedValue({
      results: [{ filename: 'ext.txt', status: 'created', novel_id: -5, title: 'Ext', author_name: 'ExtAuthor' }],
      created: 1,
      duplicate: 0,
      failed: 0,
    })

    const wrapper = mount(UploadModal, {
      props: { isOpen: true },
      global: { stubs: { teleport: true } },
    })
    await flushPromises()

    await wrapper.find('[data-testid="upload-preset-toggle"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="upload-preset-author-input"]').setValue('-12')
    await nextTick()

    const fileInput = wrapper.find('[data-testid="upload-file-input"]')
    const file = new File(['ext'], 'ext.txt', { type: 'text/plain' })
    Object.defineProperty(fileInput.element, 'files', { value: [file], configurable: true })
    await fileInput.trigger('change')
    await nextTick()

    const confirmBtn = wrapper.findAll('button').find((b) => b.text().trim() === '上传')!
    expect(confirmBtn.attributes('disabled')).toBeUndefined()

    await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
    await flushPromises()

    expect(mocks.uploadFiles).toHaveBeenCalledWith([file], expect.any(Function), -12)
    wrapper.unmount()
  })
})
