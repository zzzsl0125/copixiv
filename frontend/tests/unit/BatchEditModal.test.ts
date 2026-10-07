import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import BatchEditModal from '../../src/components/features/BatchEditModal.vue'
import type { BatchMetadataResult, BatchScope } from '../../src/types'

/**
 * 批量编辑「作者」/「系列」弹窗（批量栏「编辑」）。
 *
 * 这里钉的是与单本 `EditNovelModal` **刻意不同**的那条语义：批量里**留空 = 不改**
 * （单本里留空 = 解除关联）——一批书各自挂着不同的作者，空输入框不可能是它们的
 * 共同意图；解绑必须显式勾选，由前端翻成请求体里的显式 `null`。另外钉住预演
 * （`apply: false`）在打开时被调用、执行走 `apply: true`、预演失败不拦提交。
 *
 * `useIdLookup` / `formatIdLookupHint` 用**真**实现：防抖与 hint 文案正是这里
 * 要观察的行为，mock 掉等于把被测行为换成空壳。
 */

const apiMock = vi.hoisted(() => ({
  batchMetadata: vi.fn(),
  lookupAuthor: vi.fn(),
  lookupSeries: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  novelApi: {
    batchMetadata: apiMock.batchMetadata,
    lookupAuthor: apiMock.lookupAuthor,
    lookupSeries: apiMock.lookupSeries,
  },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

const SCOPE: BatchScope = { mode: 'ids', novel_ids: [1, 2, 3], excluded_ids: [] }

/** 预演响应：10 篇选择集 = 6 篇本地书（可改）+ 4 篇 pixiv 书（跳过）。 */
const PREVIEW: BatchMetadataResult = { matched: 10, applicable: 6, skipped: 4, affected: 0 }
/** 真执行响应：同形状，affected 才是真改了的那部分。 */
const APPLIED: BatchMetadataResult = { ...PREVIEW, affected: 6 }

function mountModal(props: Partial<{ isOpen: boolean; scope: BatchScope | null; scopeLabel: string }> = {}) {
  return mount(BatchEditModal, {
    props: { isOpen: true, scope: SCOPE, scopeLabel: '已勾选的 10 篇', ...props },
  })
}

type Wrapper = ReturnType<typeof mountModal>

function input(wrapper: Wrapper, testid: string): HTMLInputElement {
  return wrapper.get(`[data-testid="${testid}"]`).element as HTMLInputElement
}

function text(wrapper: Wrapper, testid: string): string {
  return wrapper.get(`[data-testid="${testid}"]`).text()
}

function confirmButton(wrapper: Wrapper) {
  return wrapper.get('[data-testid="base-modal"] button[type="submit"]')
}

async function submit(wrapper: Wrapper) {
  await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
  await flushPromises()
}

/**
 * 等过作者 / 系列查询的防抖窗口（ID_LOOKUP_DEBOUNCE_MS=350，真实计时器；
 * 与 EditNovelModal.test.ts 同法——flushPromises 自己靠 setTimeout(0) 落地，
 * 混用 fake timers 会互相卡住）。
 */
async function settleLookup() {
  await new Promise((resolve) => setTimeout(resolve, 420))
  await flushPromises()
}

describe('BatchEditModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 预演与执行同形状，只有 affected 不同（服务端契约）
    apiMock.batchMetadata.mockImplementation(
      async (params: { apply: boolean }) => (params.apply ? APPLIED : PREVIEW),
    )
    apiMock.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: '新作者', source: 'pixiv', novel_count: 2,
    })
    apiMock.lookupSeries.mockResolvedValue({
      series_id: 888, series_name: '新系列', source: 'local', novel_count: 5,
    })
  })

  it('打开即预演（apply:false），报出「本地书将被修改 / pixiv 书跳过」', async () => {
    const wrapper = mountModal()
    await flushPromises()

    expect(apiMock.batchMetadata).toHaveBeenCalledTimes(1)
    expect(apiMock.batchMetadata.mock.calls[0][0]).toEqual({ scope: SCOPE, apply: false })

    const preview = text(wrapper, 'batch-edit-preview')
    expect(preview).toContain('已勾选 10 篇')
    expect(preview).toContain('6 篇本地书将被修改')
    expect(preview).toContain('4 篇 pixiv 书跳过')
  })

  it('预演进行中显示「统计中…」，落地后换成数字', async () => {
    const wrapper = mountModal()

    // 未等预演落地：第一帧就是加载态（不是空白，也不是"0 篇"）
    expect(text(wrapper, 'batch-edit-preview')).toBe('统计中…')

    await flushPromises()
    expect(text(wrapper, 'batch-edit-preview')).toContain('已勾选 10 篇')
  })

  it('两个字段都没填也没勾选 → 确认按钮禁用（不发一次空请求）', async () => {
    const wrapper = mountModal()
    await flushPromises()

    expect(confirmButton(wrapper).attributes('disabled')).toBeDefined()
    // 两句"留空"的承诺由各自的 hint 给（不摆静态说明）
    expect(text(wrapper, 'batch-edit-author-hint')).toBe('留空 = 不修改作者')
    expect(text(wrapper, 'batch-edit-series-hint')).toBe('留空 = 不修改系列')

    await submit(wrapper)

    // 只有那次预演，没有被禁用的确认按钮漏出去的第二条请求
    expect(apiMock.batchMetadata).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('success')).toBeUndefined()
  })

  it('填了作者 ID → 可确认；提交体只带 author_id（系列留空 = 不改）+ apply:true', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('777')
    expect(confirmButton(wrapper).attributes('disabled')).toBeUndefined()

    await submit(wrapper)

    expect(apiMock.batchMetadata).toHaveBeenLastCalledWith({
      scope: SCOPE,
      author_id: 777,
      apply: true,
    })
    expect(wrapper.emitted('success')?.[0]).toEqual([APPLIED])
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('勾选「解除作者关联」→ 输入框置灰、hint 说清显示名保留、提交体是显式 null', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await wrapper.get('[data-testid="batch-edit-author-unlink"]').setValue(true)

    expect(input(wrapper, 'batch-edit-author-input').disabled).toBe(true)
    const hint = text(wrapper, 'batch-edit-author-hint')
    expect(hint).toContain('解除作者关联')
    expect(hint).toContain('显示名保留')
    // 勾选本身就是"要改"——不必再填 ID 也能确认
    expect(confirmButton(wrapper).attributes('disabled')).toBeUndefined()

    await submit(wrapper)

    expect(apiMock.batchMetadata).toHaveBeenLastCalledWith({
      scope: SCOPE,
      author_id: null,
      apply: true,
    })
  })

  it('勾选后输入框内容一律忽略（解绑是那个勾，不是框里的字）', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('999')
    await wrapper.get('[data-testid="batch-edit-author-unlink"]').setValue(true)
    await submit(wrapper)

    expect(apiMock.batchMetadata).toHaveBeenLastCalledWith({
      scope: SCOPE,
      author_id: null,
      apply: true,
    })
  })

  it('系列的解绑收尾与作者不同（连显示名与章节号一起清）', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await wrapper.get('[data-testid="batch-edit-series-unlink"]').setValue(true)

    expect(text(wrapper, 'batch-edit-series-hint')).toContain('清掉显示名与章节号')
    await submit(wrapper)

    expect(apiMock.batchMetadata).toHaveBeenLastCalledWith({
      scope: SCOPE,
      series_id: null,
      apply: true,
    })
  })

  it('两个字段都动 → 一个请求里带两个键（apply:true）', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('777')
    await wrapper.get('[data-testid="batch-edit-series-input"]').setValue('888')
    await submit(wrapper)

    expect(apiMock.batchMetadata).toHaveBeenLastCalledWith({
      scope: SCOPE,
      author_id: 777,
      series_id: 888,
      apply: true,
    })
  })

  it('填了合法 ID 会（防抖后）预查出名字', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('777')
    await settleLookup()

    expect(apiMock.lookupAuthor).toHaveBeenCalledWith(777)
    expect(text(wrapper, 'batch-edit-author-hint')).toContain('新作者')
  })

  it('非数字 / 0 / 负数一律拦提交（红字），改回正整数即可确认', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('abc')
    expect(confirmButton(wrapper).attributes('disabled')).toBeDefined()
    expect(text(wrapper, 'batch-edit-author-hint')).toContain('只能填数字 ID')
    expect(text(wrapper, 'batch-edit-validation-error')).toBe('作者 ID 必须是正整数')

    await submit(wrapper)
    expect(apiMock.batchMetadata).toHaveBeenCalledTimes(1)   // 仍然只有预演

    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('0')
    expect(confirmButton(wrapper).attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('-5')
    expect(confirmButton(wrapper).attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('777')
    expect(confirmButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('预演失败不拦操作：降级文案（不上抛 error）+ 仍可提交', async () => {
    apiMock.batchMetadata.mockRejectedValueOnce(new Error('500 boom'))
    const wrapper = mountModal()
    await flushPromises()

    expect(text(wrapper, 'batch-edit-preview')).toContain('统计暂不可用')
    // 预演只是一次读数，不是这次操作的结果——不该弹错误提示
    expect(wrapper.emitted('error')).toBeUndefined()

    apiMock.batchMetadata.mockResolvedValueOnce(APPLIED)
    await wrapper.get('[data-testid="batch-edit-series-input"]').setValue('888')
    await submit(wrapper)

    expect(apiMock.batchMetadata).toHaveBeenLastCalledWith({
      scope: SCOPE,
      series_id: 888,
      apply: true,
    })
    expect(wrapper.emitted('success')?.[0]).toEqual([APPLIED])
  })

  it('执行失败：上抛 error，不谎报成功', async () => {
    const wrapper = mountModal()
    await flushPromises()

    apiMock.batchMetadata.mockRejectedValueOnce(new Error('500 boom'))
    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('777')
    await submit(wrapper)

    expect(wrapper.emitted('success')).toBeUndefined()
    expect(wrapper.emitted('error')).toBeTruthy()
  })

  it('关掉再打开：表单与勾选清空、确认回到禁用，并重新预演一次', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await wrapper.get('[data-testid="batch-edit-author-input"]').setValue('777')
    await wrapper.get('[data-testid="batch-edit-author-unlink"]').setValue(true)

    await wrapper.setProps({ isOpen: false })
    await wrapper.setProps({ isOpen: true })
    await flushPromises()

    expect(input(wrapper, 'batch-edit-author-input').value).toBe('')
    expect(input(wrapper, 'batch-edit-author-unlink').checked).toBe(false)
    expect(confirmButton(wrapper).attributes('disabled')).toBeDefined()
    expect(apiMock.batchMetadata).toHaveBeenCalledTimes(2)
  })

  it('范围为空（scope=null）时确认禁用，且不发预演', async () => {
    const wrapper = mountModal({ scope: null })
    await flushPromises()

    expect(apiMock.batchMetadata).not.toHaveBeenCalled()
    expect(text(wrapper, 'batch-edit-preview')).toContain('统计暂不可用')
    expect(confirmButton(wrapper).attributes('disabled')).toBeDefined()
  })
})
