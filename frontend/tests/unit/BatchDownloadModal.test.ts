import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import BatchDownloadModal from '../../src/components/features/BatchDownloadModal.vue'

const apiMock = vi.hoisted(() => ({
  countNovels: vi.fn(),
  batchDownloadPreview: vi.fn(),
  batchDownload: vi.fn(),
  submitBatchExport: vi.fn(),
  getConfig: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  novelApi: {
    countNovels: apiMock.countNovels,
    batchDownloadPreview: apiMock.batchDownloadPreview,
    batchDownload: apiMock.batchDownload,
    submitBatchExport: apiMock.submitBatchExport,
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

describe('BatchDownloadModal (选多少下多少 — no download-count decision)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMock.batchDownloadPreview.mockResolvedValue({ path: '作者/标题_1.txt' })
    apiMock.batchDownload.mockResolvedValue(ZIP_RESPONSE)
    apiMock.getConfig.mockResolvedValue({
      batch_download_naming: '{id}-{title}',
    })
  })

  it('has no download-count input — the scope decides the quantity', async () => {
    const wrapper = mountModal()
    await openAndCount(wrapper, 1000)

    expect(wrapper.find('input[type="number"]').exists()).toBe(false)
    expect(
      wrapper.findAll('button').some((b) => b.text().includes('下载全部')),
    ).toBe(false)
  })

  it('sends the FULL matched count as limit regardless of size', async () => {
    const wrapper = mountModal()
    await openAndCount(wrapper, 1000)

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiMock.batchDownload).toHaveBeenCalledTimes(1)
    expect(apiMock.batchDownload).toHaveBeenCalledWith(
      expect.objectContaining({
        limit: 1000,
        order_by: 'id',
        order_direction: 'DESC',
        format_mode: 'txt',
        keyword: 'R-18',
      }),
    )
    expect(downloadBlobMock).toHaveBeenCalledWith(ZIP_BLOB, 'test.zip')
  })

  it('ids-mode selection sends exactly the selected count as limit', async () => {
    const wrapper = mountModal()
    await wrapper.setProps({
      isOpen: true,
      novelIds: [11, 22, 33, 44, 55],
    })
    await flushPromises()

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiMock.batchDownload).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 5, novel_ids: [11, 22, 33, 44, 55] }),
    )
  })

  it('routes selections over 1000 to the background export task', async () => {
    apiMock.submitBatchExport.mockResolvedValue({ task_id: 77, matched: 1500 })
    const wrapper = mountModal()
    await openAndCount(wrapper, 1500)

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiMock.batchDownload).not.toHaveBeenCalled()
    expect(apiMock.submitBatchExport).toHaveBeenCalledWith(
      expect.objectContaining({ novel_ids: [] }),
    )
    expect(wrapper.emitted('task-submitted')?.[0]).toEqual([
      { task_id: 77, matched: 1500 },
    ])
  })
})
