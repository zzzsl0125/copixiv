import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import UploadModal from '../../src/components/features/UploadModal.vue'
import type { UploadResponse } from '../../src/types'

/**
 * UploadModal（我的上传）契约测试：
 * - 多选 .txt，待上传列表（文件名 + 大小）可移除；
 * - 提交 POST /api/library/uploads 用的 multipart 字段名是 `files`，重复多次；
 * - 逐条结果三态（已入库 / 已在库中（带已有那本的标题 + id）/ 失败原因）+ 汇总行；
 * - 部分成功不阻塞其他文件；整包失败（无文件 400 / 网络）走 emit('error')。
 */

const apiMock = vi.hoisted(() => ({
  uploadFiles: vi.fn(),
}))

const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  libraryApi: { uploadFiles: apiMock.uploadFiles },
  getApiErrorMessage: (err: unknown, fallback: string) =>
    err instanceof Error ? err.message : fallback,
}))

vi.mock('../../src/composables', () => ({
  useToast: () => ({
    toasts: { value: [] },
    show: vi.fn(),
    success: toastMock.success,
    error: toastMock.error,
    info: toastMock.info,
    warning: toastMock.warning,
  }),
}))

function makeFile(name: string, size = 2048, type = 'text/plain'): File {
  return new File([new Uint8Array(size)], name, { type })
}

function mountModal(props: Partial<{ isOpen: boolean }> = {}) {
  return mount(UploadModal, { props: { isOpen: true, ...props } })
}

/** 把文件塞进 <input type="file"> 并触发 change（jsdom 允许直接改写 files）。 */
async function pickFiles(wrapper: ReturnType<typeof mount>, files: File[]) {
  const input = wrapper.get('[data-testid="upload-file-input"]')
  Object.defineProperty(input.element, 'files', { value: files, writable: true, configurable: true })
  await input.trigger('change')
}

const submit = async (wrapper: ReturnType<typeof mount>) => {
  await wrapper.get('form').trigger('submit')
  await flushPromises()
}

const MIXED: UploadResponse = {
  results: [
    { filename: '我的书.txt', status: 'created', novel_id: -999999999, title: '我的书', author_name: '某某', text: 123456 },
    { filename: 'dup.txt', status: 'duplicate', novel_id: -999999998, title: '已有那本' },
    { filename: 'bad.txt', status: 'failed', error: '无法识别编码（尝试 UTF-8 / GB18030 / Big5 / UTF-16）' },
  ],
  created: 1,
  duplicate: 1,
  failed: 1,
}

describe('UploadModal 选择与移除', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('未选文件时上传按钮禁用（BaseModal confirm 禁用态）', () => {
    const wrapper = mountModal()
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
  })

  it('文件输入限定 .txt 且支持多选', () => {
    const input = mountModal().get('[data-testid="upload-file-input"]')
    expect(input.attributes('accept')).toBe('.txt')
    expect(input.attributes('multiple')).toBeDefined()
  })

  it('选中的文件出现在待上传列表（文件名 + 大小），可逐个移除', async () => {
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('我的书.txt', 2048), makeFile('另一本.txt', 1024 * 1024)])

    const list = wrapper.get('[data-testid="upload-file-list"]')
    expect(list.text()).toContain('我的书.txt')
    expect(list.text()).toContain('2.0 KB')
    expect(list.text()).toContain('另一本.txt')
    expect(list.text()).toContain('1.0 MB')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()

    await wrapper.findAll('[data-testid="upload-remove-file"]')[0].trigger('click')
    expect(wrapper.get('[data-testid="upload-file-list"]').text()).not.toContain('我的书.txt')
  })

  it('同一文件重复选择（同名同大小）不重复入列', async () => {
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('书.txt', 100)])
    await pickFiles(wrapper, [makeFile('书.txt', 100)])

    expect(wrapper.findAll('[data-testid="upload-file-list"] li')).toHaveLength(1)
  })

  it('关闭再打开时清空待上传列表', async () => {
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('书.txt')])
    expect(wrapper.find('[data-testid="upload-file-list"]').exists()).toBe(true)

    await wrapper.setProps({ isOpen: false })
    await wrapper.setProps({ isOpen: true })

    expect(wrapper.find('[data-testid="upload-file-list"]').exists()).toBe(false)
  })
})

describe('UploadModal 上传', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('把全部待上传文件与进度回调交给 API，逐条结果与汇总行都渲染', async () => {
    apiMock.uploadFiles.mockResolvedValue(MIXED)
    const wrapper = mountModal()
    const a = makeFile('我的书.txt')
    const b = makeFile('dup.txt')
    const c = makeFile('bad.txt')
    await pickFiles(wrapper, [a, b, c])

    await submit(wrapper)

    // multipart 字段名（files）由 api/library.ts 组装，见 LibraryApi.test.ts；
    // 这里钉组件侧：文件按顺序整体提交 + 进度回调已接上。
    const [submitted, onProgress] = apiMock.uploadFiles.mock.calls[0]
    expect((submitted as File[]).map((f) => f.name)).toEqual(['我的书.txt', 'dup.txt', 'bad.txt'])
    expect(typeof onProgress).toBe('function')

    const results = wrapper.get('[data-testid="upload-result-list"]')
    expect(results.text()).toContain('我的书.txt')
    expect(results.text()).toContain('已入库')
    expect(results.text()).toContain('我的书')
    expect(results.text()).toContain('123456 字')
    // 重复项必须指出已有那本的标题与 id
    expect(results.text()).toContain('已在库中')
    expect(results.text()).toContain('已有那本')
    expect(results.text()).toContain('-999999998')
    // 失败项给出后端原因
    expect(results.text()).toContain('无法识别编码（尝试 UTF-8 / GB18030 / Big5 / UTF-16）')

    expect(wrapper.get('[data-testid="upload-summary"]').text()).toContain('成功 1 / 重复 1 / 失败 1')
  })

  it('成功后 emit uploaded（入库数）并提示；失败/重复不阻塞其他文件', async () => {
    apiMock.uploadFiles.mockResolvedValue(MIXED)
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('我的书.txt'), makeFile('dup.txt'), makeFile('bad.txt')])

    await submit(wrapper)

    expect(wrapper.emitted('uploaded')).toEqual([
      [{ created: 1, duplicate: 1, failed: 1 }],
    ])
    expect(toastMock.success).toHaveBeenCalledWith('已入库 1 本')
    expect(toastMock.info).toHaveBeenCalledWith('1 个文件已在库中（同一份正文）')
    expect(toastMock.warning).toHaveBeenCalledWith('1 个文件上传失败，详见结果列表')
    // 全部文件都已回结果 → 待上传列表清空，不会重复提交
    expect(wrapper.find('[data-testid="upload-file-list"]').exists()).toBe(false)
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
  })

  it('全部失败（无 created）不 emit uploaded，只在结果里逐条报告', async () => {
    apiMock.uploadFiles.mockResolvedValue({
      results: [{ filename: 'bad.txt', status: 'failed', error: '空文件' }],
      created: 0,
      duplicate: 0,
      failed: 1,
    })
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('bad.txt')])

    await submit(wrapper)

    expect(wrapper.emitted('uploaded')).toBeUndefined()
    expect(wrapper.get('[data-testid="upload-summary"]').text()).toContain('成功 0 / 重复 0 / 失败 1')
  })

  it('上传中禁用按钮并显示进度（已完成/总数 + 百分比）', async () => {
    let resolveUpload: (value: UploadResponse) => void = () => {}
    let reportProgress: (percent: number) => void = () => {}
    apiMock.uploadFiles.mockImplementation(
      (_files: File[], onProgress?: (p: number) => void) =>
        new Promise<UploadResponse>((resolve) => {
          resolveUpload = resolve
          reportProgress = (p) => onProgress?.(p)
        }),
    )
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('a.txt'), makeFile('b.txt')])

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    reportProgress(50)
    await flushPromises()
    expect(wrapper.get('[data-testid="upload-progress"]').text()).toContain('0/2')
    expect(wrapper.get('[data-testid="upload-progress"]').text()).toContain('50%')

    resolveUpload({
      results: [
        { filename: 'a.txt', status: 'created', novel_id: -1, title: 'A' },
        { filename: 'b.txt', status: 'created', novel_id: -2, title: 'B' },
      ],
      created: 2,
      duplicate: 0,
      failed: 0,
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="upload-progress"]').exists()).toBe(false)
    expect(wrapper.emitted('uploaded')).toEqual([[{ created: 2, duplicate: 0, failed: 0 }]])
  })

  it('整包失败（如无文件 400 / 网络错误）走 emit(error)，不发 uploaded', async () => {
    apiMock.uploadFiles.mockRejectedValue(new Error('400：没有文件'))
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('书.txt')])

    await submit(wrapper)

    expect(wrapper.emitted('error')).toEqual([['400：没有文件']])
    expect(wrapper.emitted('uploaded')).toBeUndefined()
    // 失败后按钮恢复可用、文件仍在列表里，可直接重试
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="upload-file-list"]').text()).toContain('书.txt')
  })

  it('后端未回结果的文件留在待上传列表（不被静默丢弃）', async () => {
    apiMock.uploadFiles.mockResolvedValue({
      results: [{ filename: 'a.txt', status: 'created', novel_id: -1, title: 'A' }],
      created: 1,
      duplicate: 0,
      failed: 0,
    })
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('a.txt'), makeFile('b.txt')])

    await submit(wrapper)

    expect(wrapper.get('[data-testid="upload-file-list"]').text()).toContain('b.txt')
    expect(wrapper.get('[data-testid="upload-file-list"]').text()).not.toContain('a.txt')
  })
})
