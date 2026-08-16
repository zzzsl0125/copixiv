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

const downloadBlobMock = vi.hoisted(() => vi.fn())

// Stub only the browser-side download helper; keep buildQueries /
// filenameFromContentDisposition real so the request payload assertion
// still passes through the real query construction.
vi.mock('../../src/lib/utils', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/lib/utils')>()
  return { ...actual, downloadBlob: downloadBlobMock }
})

const ZIP_BLOB = new Blob(['zip-bytes'])
const ZIP_RESPONSE = {
  data: ZIP_BLOB,
  headers: { 'content-disposition': "attachment; filename*=UTF-8''test.zip" },
}

function mountModal(keyword = 'R-18') {
  return mount(BatchDownloadModal, {
    props: {
      isOpen: false,
      keyword,
      order_by: 'id',
      order_direction: 'DESC',
      min_like: 0,
      min_text: 0,
    },
  })
}

async function openAndCount(wrapper: ReturnType<typeof mountModal>, total: number) {
  apiMock.countNovels.mockResolvedValue({ total })
  await wrapper.setProps({ isOpen: true })
  await flushPromises()
}

describe('BatchDownloadModal (single-request limit contract)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMock.batchDownloadPreview.mockResolvedValue({ path: '作者/标题_1.txt' })
    apiMock.batchDownload.mockResolvedValue(ZIP_RESPONSE)
    apiMock.getConfig.mockResolvedValue({
      default_min_like: 0,
      default_min_text: 0,
      batch_download_naming: '{id}-{title}',
    })
  })

  it('caps "下载全部" at 500 even when the matched total is larger', async () => {
    const wrapper = mountModal()
    await openAndCount(wrapper, 1000)

    const downloadAll = wrapper
      .findAll('button')
      .find((button) => button.text().includes('下载全部'))
    expect(downloadAll).toBeTruthy()
    await downloadAll!.trigger('click')

    const limitInput = wrapper.find('input[type="number"]')
      .element as HTMLInputElement
    expect(limitInput.value).toBe('500')
  })

  it('sends limit=500 in the actual download request when total > 500', async () => {
    const wrapper = mountModal()
    await openAndCount(wrapper, 1000)

    await wrapper
      .findAll('button')
      .find((button) => button.text().includes('下载全部'))!
      .trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiMock.batchDownload).toHaveBeenCalledTimes(1)
    expect(apiMock.batchDownload).toHaveBeenCalledWith(
      expect.objectContaining({
        limit: 500,
        order_by: 'id',
        order_direction: 'DESC',
        format_mode: 'txt',
        keyword: 'R-18',
      }),
    )
    expect(downloadBlobMock).toHaveBeenCalledWith(ZIP_BLOB, 'test.zip')
  })

  it('sends the matched count as limit when total <= 500 (no false cap)', async () => {
    const wrapper = mountModal()
    await openAndCount(wrapper, 30)

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiMock.batchDownload).toHaveBeenCalledTimes(1)
    expect(apiMock.batchDownload).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 30 }),
    )
  })
})
