import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import UploadModal from '../../src/components/features/UploadModal.vue'
import type { UploadResponse } from '../../src/types'

/**
 * UploadModal（我的上传）契约测试：
 * - 多选 .txt，待上传列表（文件名 + 大小）可移除；
 * - 提交 POST /api/library/uploads 用的 multipart 字段名是 `files`，重复多次；
 * - 逐条结果三态（已入库 / 已在库中（带已有那本的标题 + id）/ 失败原因）+ 汇总行；
 * - 部分成功不阻塞其他文件；整包失败（无文件 400 / 网络）走 emit('error')；
 * - 可折叠的「预设作者」：展开后与编辑弹窗的作者行同构，合法正整数随请求带上，
 *   非法拦上传，系列只留一个置灰占位。
 *
 * `useIdLookup` / `formatIdLookupHint` 用**真**实现（与 BatchEditModal.test.ts 同法）：
 * 防抖解析与 hint 文案正是预设作者这里要观察的行为，mock 掉等于把被测行为换成空壳。
 */

const apiMock = vi.hoisted(() => ({
  uploadFiles: vi.fn(),
  lookupAuthor: vi.fn(),
}))

const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  libraryApi: { uploadFiles: apiMock.uploadFiles },
  novelApi: { lookupAuthor: apiMock.lookupAuthor },
  getApiErrorMessage: (err: unknown, fallback: string) =>
    err instanceof Error ? err.message : fallback,
}))

vi.mock('../../src/composables', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/composables')>()
  return {
    ...actual,
    useToast: () => ({
      toasts: { value: [] },
      show: vi.fn(),
      success: toastMock.success,
      error: toastMock.error,
      info: toastMock.info,
      warning: toastMock.warning,
    }),
  }
})

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

/** 弹窗页脚的确认按钮（toggle 是 type="button"，不会混进来）。 */
const submitButton = (wrapper: ReturnType<typeof mount>) => wrapper.get('button[type="submit"]')

/**
 * 等过作者解析的防抖窗口（ID_LOOKUP_DEBOUNCE_MS=350，真实计时器；与
 * BatchEditModal.test.ts 同法——flushPromises 自己靠 setTimeout(0) 落地，
 * 混用 fake timers 会互相卡住）。
 */
async function settleLookup() {
  await new Promise((resolve) => setTimeout(resolve, 420))
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

  it('文件输入限定 .txt / .epub 且支持多选', () => {
    const input = mountModal().get('[data-testid="upload-file-input"]')
    expect(input.attributes('accept')).toBe('.txt,.epub')
    expect(input.attributes('multiple')).toBeDefined()
  })

  it('标题与说明都覆盖 EPUB（不再写着"仅收 .txt"）', () => {
    const wrapper = mountModal()
    expect(wrapper.text()).toContain('上传本地书籍')
    expect(wrapper.text()).toContain('.epub')
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

  it('EPUB 补挂重复件也 emit uploaded，并渲染「已补挂 EPUB」', async () => {
    apiMock.uploadFiles.mockResolvedValue({
      results: [
        { filename: 'dup.epub', status: 'duplicate', novel_id: -1, title: '已有那本', epub_attached: true },
      ],
      created: 0,
      duplicate: 1,
      failed: 0,
    })
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('dup.epub')])

    await submit(wrapper)

    // 没有 created，但补挂了原件 → 仍要刷新列表（否则 EPUB 徽章不亮）
    expect(wrapper.emitted('uploaded')).toEqual([[{ created: 0, duplicate: 1, failed: 0 }]])
    expect(wrapper.get('[data-testid="upload-result-list"]').text()).toContain('已补挂 EPUB')
    expect(toastMock.info).toHaveBeenCalledWith('1 个文件已在库中，其中 1 个补挂了 EPUB')
  })

  it('纯重复（没补挂）不 emit uploaded —— 旧行为逐字不变', async () => {
    apiMock.uploadFiles.mockResolvedValue({
      results: [{ filename: 'dup.txt', status: 'duplicate', novel_id: -2, title: '已有那本' }],
      created: 0,
      duplicate: 1,
      failed: 0,
    })
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('dup.txt')])

    await submit(wrapper)

    expect(wrapper.emitted('uploaded')).toBeUndefined()
    expect(toastMock.info).toHaveBeenCalledWith('1 个文件已在库中（同一份正文）')
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

/**
 * 预设作者（整批共用）：POST /api/library/uploads 的可选字段 `author_id`。
 * 不展开 / 留空 = **一个键都不加**——不预设的请求体与加这个字段之前一字不差。
 */
describe('UploadModal 预设作者', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMock.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: '新作者', source: 'pixiv', novel_count: 2,
    })
  })

  /** 展开折叠区（默认收起）。 */
  const openPreset = (wrapper: ReturnType<typeof mount>) =>
    wrapper.get('[data-testid="upload-preset-toggle"]').trigger('click')

  const authorInput = (wrapper: ReturnType<typeof mount>) =>
    wrapper.get('[data-testid="upload-preset-author-input"]')

  /** 输入框里的原文（`DOMWrapper.element` 的类型是 Element，取值要收窄）。 */
  const authorValue = (wrapper: ReturnType<typeof mount>) =>
    (authorInput(wrapper).element as HTMLInputElement).value

  const authorHint = (wrapper: ReturnType<typeof mount>) =>
    wrapper.get('[data-testid="upload-preset-author-hint"]').text()

  /** 这次上传交给 API 的第三个实参（预设作者）。 */
  const sentAuthorId = (call = 0) => apiMock.uploadFiles.mock.calls[call][2]

  /** 一本新书的响应（多数用例只需要"上传成功"这一件事）。 */
  const ONE_CREATED: UploadResponse = {
    results: [{ filename: '书.txt', status: 'created', novel_id: -1, title: '书' }],
    created: 1,
    duplicate: 0,
    failed: 0,
  }

  it('默认折叠：只有一个小按钮「+ 预设作者」，点开才出现作者 ID 输入框', async () => {
    const wrapper = mountModal()
    expect(wrapper.get('[data-testid="upload-preset-toggle"]').text()).toBe('+ 预设作者')
    expect(wrapper.find('[data-testid="upload-preset-author-input"]').exists()).toBe(false)

    await openPreset(wrapper)

    // 与编辑 / 批量编辑弹窗同构：数字键盘 + 关掉自动填充 + 正整数占位
    const input = authorInput(wrapper)
    expect(input.attributes('inputmode')).toBe('numeric')
    expect(input.attributes('autocomplete')).toBe('off')
    expect(input.attributes('placeholder')).toBe('如 1234567')
    // 留空那一档的承诺由 hint 给（不摆静态说明）
    expect(authorHint(wrapper)).toBe('留空 = 按文件名解析作者')
  })

  it('不预设：交给 API 的 authorId 是 null（键都不加，与今天一致）', async () => {
    apiMock.uploadFiles.mockResolvedValue(ONE_CREATED)
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('书.txt')])

    await submit(wrapper)

    expect(sentAuthorId()).toBeNull()
  })

  it('填合法正整数 → 上传带上它；上传后输入值保留（同一批可分多次传）', async () => {
    apiMock.uploadFiles.mockResolvedValue(ONE_CREATED)
    const wrapper = mountModal()
    await openPreset(wrapper)
    await authorInput(wrapper).setValue('777')
    await pickFiles(wrapper, [makeFile('书.txt')])

    await submit(wrapper)

    expect(sentAuthorId()).toBe(777)
    // 文件与结果都清了，预设还在
    expect(wrapper.find('[data-testid="upload-file-list"]').exists()).toBe(false)
    expect(authorValue(wrapper)).toBe('777')
  })

  it('「继续上传其他文件」保留预设：不折叠、不清空，收起时也看得见', async () => {
    apiMock.uploadFiles.mockResolvedValue(ONE_CREATED)
    const wrapper = mountModal()
    await openPreset(wrapper)
    await authorInput(wrapper).setValue('777')
    await pickFiles(wrapper, [makeFile('书.txt')])
    await submit(wrapper)

    await wrapper.get('[data-testid="upload-more"]').trigger('click')

    expect(authorValue(wrapper)).toBe('777')
    // 收起也要看得见这次会带的作者——一个被收起的预设不该悄悄生效
    await wrapper.get('[data-testid="upload-preset-toggle"]').trigger('click')
    expect(wrapper.get('[data-testid="upload-preset-toggle"]').text()).toBe('预设作者：777')
  })

  it('重新打开弹窗：预设与折叠态回到默认（新的一批）', async () => {
    const wrapper = mountModal()
    await openPreset(wrapper)
    await authorInput(wrapper).setValue('777')

    await wrapper.setProps({ isOpen: false })
    await wrapper.setProps({ isOpen: true })

    expect(wrapper.find('[data-testid="upload-preset-author-input"]').exists()).toBe(false)
    await openPreset(wrapper)
    expect(authorValue(wrapper)).toBe('')
  })

  it('非法值（abc / 0 / 负数）→ 上传按钮禁用 + 红字，一个请求都不发；改回正整数恢复', async () => {
    apiMock.uploadFiles.mockResolvedValue(ONE_CREATED)
    const wrapper = mountModal()
    await openPreset(wrapper)
    await pickFiles(wrapper, [makeFile('书.txt')])
    expect(submitButton(wrapper).attributes('disabled')).toBeUndefined()

    await authorInput(wrapper).setValue('abc')
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()
    expect(authorHint(wrapper)).toBe('只能填数字 ID（pixiv 作者 ID）')
    expect(wrapper.get('[data-testid="upload-preset-author-error"]').text()).toBe('作者 ID 必须是正整数')

    // 按钮禁用之外组件里还有第二道闸：表单 submit 事件绕不过校验
    await submit(wrapper)
    expect(apiMock.uploadFiles).not.toHaveBeenCalled()

    await authorInput(wrapper).setValue('0')
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()

    await authorInput(wrapper).setValue('-5')
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()

    await authorInput(wrapper).setValue('777')
    expect(submitButton(wrapper).attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="upload-preset-author-error"]').exists()).toBe(false)
  })

  it('填了合法 ID 会（防抖后）预查出名字：✓ 名字 · 库内已有 N 本', async () => {
    const wrapper = mountModal()
    await openPreset(wrapper)
    await authorInput(wrapper).setValue('777')

    await settleLookup()

    expect(apiMock.lookupAuthor).toHaveBeenCalledWith(777)
    expect(authorHint(wrapper)).toBe('✓ 新作者 · 库内已有 2 本')
  })

  it('查不到该作者 → 黄档（能传，名字之后补齐），不拦上传', async () => {
    apiMock.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: null, source: 'unknown', novel_count: 0,
    })
    const wrapper = mountModal()
    await openPreset(wrapper)
    await pickFiles(wrapper, [makeFile('书.txt')])
    await authorInput(wrapper).setValue('777')

    await settleLookup()

    expect(authorHint(wrapper)).toContain('未查到该作者')
    expect(submitButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('系列只留一个置灰占位行（不接输入逻辑）', async () => {
    const wrapper = mountModal()
    await openPreset(wrapper)

    const series = wrapper.get('[data-testid="upload-preset-series-disabled"]')
    expect(series.get('input').attributes('disabled')).toBeDefined()
    expect(series.text()).toContain('系列：后续版本支持')
  })

  it('duplicate 条带 author_name → 追加「· 已挂作者：X」（重复件也被应用了）', async () => {
    apiMock.uploadFiles.mockResolvedValue({
      results: [{
        filename: 'dup.txt', status: 'duplicate', novel_id: -999999998,
        title: '已有那本', author_name: '某作者',
      }],
      created: 0,
      duplicate: 1,
      failed: 0,
    })
    const wrapper = mountModal()
    await openPreset(wrapper)
    await authorInput(wrapper).setValue('777')
    await pickFiles(wrapper, [makeFile('dup.txt')])

    await submit(wrapper)

    const results = wrapper.get('[data-testid="upload-result-list"]')
    expect(results.text()).toContain('已在库中：《已有那本》')
    expect(results.text()).toContain('-999999998')
    expect(results.text()).toContain('已挂作者：某作者')
  })

  it('duplicate 没带 author_name（没预设 / 老响应）→ 不出现「已挂作者」', async () => {
    apiMock.uploadFiles.mockResolvedValue({
      results: [{ filename: 'dup.txt', status: 'duplicate', novel_id: -999999998, title: '已有那本' }],
      created: 0,
      duplicate: 1,
      failed: 0,
    })
    const wrapper = mountModal()
    await pickFiles(wrapper, [makeFile('dup.txt')])

    await submit(wrapper)

    expect(wrapper.get('[data-testid="upload-result-list"]').text()).not.toContain('已挂作者')
  })
})
