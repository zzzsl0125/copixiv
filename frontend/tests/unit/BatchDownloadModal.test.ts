import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import BatchDownloadModal from '../../src/components/features/BatchDownloadModal.vue'

const apiMock = vi.hoisted(() => ({
  countNovels: vi.fn(),
  batchDownloadPreview: vi.fn(),
  batchDownload: vi.fn(),
  getConfig: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  novelApi: {
    countNovels: apiMock.countNovels,
    batchDownloadPreview: apiMock.batchDownloadPreview,
    batchDownload: apiMock.batchDownload,
  },
  systemApi: {
    getConfig: apiMock.getConfig,
  },
}))

describe('BatchDownloadModal (single-request limit)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMock.countNovels.mockResolvedValue({ total: 1000 })
    apiMock.batchDownloadPreview.mockResolvedValue({ path: '作者/标题_1.txt' })
    apiMock.getConfig.mockResolvedValue({
      default_min_like: 0,
      default_min_text: 0,
      batch_download_naming: '{id}-{title}',
    })
  })

  it('caps "下载全部" at 500 even when the matched total is larger', async () => {
    const wrapper = mount(BatchDownloadModal, {
      props: {
        isOpen: false,
        keyword: 'R-18',
        order_by: 'id',
        order_direction: 'DESC',
        min_like: 0,
        min_text: 0,
      },
    })

    await wrapper.setProps({ isOpen: true })
    await flushPromises()

    const downloadAll = wrapper
      .findAll('button')
      .find((button) => button.text().includes('下载全部'))
    expect(downloadAll).toBeTruthy()
    await downloadAll!.trigger('click')

    const limitInput = wrapper.find('input[type="number"]')
      .element as HTMLInputElement
    expect(limitInput.value).toBe('500')
  })
})
