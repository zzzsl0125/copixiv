import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import EditNovelModal from '../../src/components/features/EditNovelModal.vue'
import type { Novel } from '../../src/types'

/**
 * 元数据编辑弹窗（docs/NOVEL_EDIT_DESIGN.md §4 / §5 / §6.2）。
 *
 * 拍板钉在这里：
 * - **「哪个字段能改」由服务端下发的 `editable_fields` 决定**（2026-09-22 改版），
 *   前端不再按 `source` 自己推一份表：pixiv 书 `title` / `author_id` / `series_id`
 *   / `caption` 置灰，本地书全可编辑；**取不到策略时一律只读**。置灰、本地校验、
 *   payload 共用同一个 `canEdit`——不可编辑的字段既不进 payload，也不参与本地校验；
 * - `caption` **仅本地书可编辑**（2026-09-22；此前是全局置灰——那条拍板的理由
 *   全是拿 pixiv 书说的，本地书没有来源会覆盖它，见 `field_policy.py`）；
 * - **作者与系列编辑的都是 id 而不是名字**（2026-09-19）：名字由服务端解析
 *   （`GET /api/novels/{author,series}/{id}` 预览、PATCH 落库），前端不再有"名字"
 *   输入框，也不会提交 `author_name` / `series_name`；
 * - **左下角「同步」**（仅 pixiv 书）：`POST /api/novels/{id}/sync` → 响应刷新初值
 *   + `emit('updated')`（父组件原地换卡片、弹窗不关）；失败走 `emit('error')`。
 *
 * 再加四条行为契约：只提交**变化**字段（PATCH 三态）、本地校验拦住保存、
 * 保存成功 emit 更新后的对象并由父组件关闭、**标题与作者 ID 同一行**（布局与
 * 下面的「系列 + 章节号」同构）。
 */

const mocks = vi.hoisted(() => ({
  updateNovel: vi.fn(),
  syncNovel: vi.fn(),
  lookupAuthor: vi.fn(),
  lookupSeries: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  novelApi: {
    updateNovel: mocks.updateNovel,
    syncNovel: mocks.syncNovel,
    lookupAuthor: mocks.lookupAuthor,
    lookupSeries: mocks.lookupSeries,
  },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

// 后端 NovelBase 下发的字段策略——只有这两种形态（与 api-contract.test.ts 同一份契约）。
const LOCAL_EDITABLE = [
  'title', 'author_id', 'series_id', 'series_index', 'tags', 'create_time', 'caption',
]
const PIXIV_EDITABLE = ['series_index', 'tags', 'create_time']

const LOCAL_NOVEL: Novel = {
  id: -999000001,
  title: '本地书名',
  author_id: null,                  // 本地书：上传时 author_id / series_id 都是 NULL
  author_name: '未知作者',
  series_id: null,
  series_name: undefined,
  series_index: undefined,
  like: 0,
  view: 0,
  text: 1234,
  caption: '本地简介',
  create_time: '2026-09-01T00:00:00+08:00',
  has_epub: 3,
  tags: ['原有标签'],
  is_favourite: 0,
  is_special_follow: 0,
  source: 'local',
  editable_fields: LOCAL_EDITABLE,
}

const PIXIV_NOVEL: Novel = {
  ...LOCAL_NOVEL,
  id: 987654,
  title: 'pixiv 标题',
  author_id: 555,
  author_name: 'pixiv 作者',
  series_id: 606,
  series_name: 'pixiv 系列',
  series_index: 3,
  source: 'pixiv',
  editable_fields: PIXIV_EDITABLE,
}

/** 本地书但已挂到 pixiv 作者 / 系列名下（作者那一栏的初值非空）。 */
const LOCAL_WITH_AUTHOR: Novel = {
  ...LOCAL_NOVEL,
  author_id: 555,
  author_name: 'pixiv 作者',
}

function mountModal(novel: Novel = LOCAL_NOVEL) {
  return mount(EditNovelModal, {
    props: { isOpen: true, novel },
  })
}

type Wrapper = ReturnType<typeof mountModal>

function input(wrapper: Wrapper, testid: string): HTMLInputElement {
  return wrapper.get(`[data-testid="${testid}"]`).element as HTMLInputElement
}

function disabled(wrapper: Wrapper, testid: string) {
  return wrapper.get(`[data-testid="${testid}"]`).attributes('disabled')
}

function submitButton(wrapper: Wrapper) {
  return wrapper.get('[data-testid="base-modal"] button[type="submit"]')
}

async function submit(wrapper: Wrapper) {
  await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
  await flushPromises()
}

/**
 * 等过作者查询的防抖窗口（LOOKUP_DEBOUNCE_MS=350，真实计时器）。
 *
 * 不用 fake timers：@vue/test-utils 的 flushPromises 自己就靠 setTimeout(0)
 * 落地，混用会互相卡住。
 */
async function settleLookup() {
  await new Promise((resolve) => setTimeout(resolve, 420))
  await flushPromises()
}

function authorHint(wrapper: Wrapper) {
  return wrapper.get('[data-testid="edit-author-hint"]').text()
}

function seriesHint(wrapper: Wrapper) {
  return wrapper.get('[data-testid="edit-series-hint"]').text()
}

describe('EditNovelModal 字段策略驱动置灰（editable_fields）', () => {
  beforeEach(() => {
    mocks.updateNovel.mockReset()
    mocks.syncNovel.mockReset()
    mocks.lookupAuthor.mockReset()
    mocks.lookupSeries.mockReset()
    mocks.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: '新作者', source: 'pixiv', novel_count: 2,
    })
  })

  it('pixiv 书：title / author_id / series_id / caption 四个字段置灰，其余照旧可编辑', () => {
    const wrapper = mountModal(PIXIV_NOVEL)

    expect(disabled(wrapper, 'edit-title')).toBeDefined()
    expect(disabled(wrapper, 'edit-author-id')).toBeDefined()
    expect(disabled(wrapper, 'edit-series-id')).toBeDefined()
    expect(disabled(wrapper, 'edit-caption')).toBeDefined()
    // 策略里保留的字段一个都不能少
    expect(disabled(wrapper, 'edit-series-index')).toBeUndefined()
    expect(disabled(wrapper, 'edit-date')).toBeUndefined()
    expect(disabled(wrapper, 'edit-tag-input')).toBeUndefined()
    // 置灰字段仍然显示当前值，只是改不了
    expect(input(wrapper, 'edit-title').value).toBe('pixiv 标题')
    expect(input(wrapper, 'edit-author-id').value).toBe('555')
    expect(input(wrapper, 'edit-series-id').value).toBe('606')
    expect(input(wrapper, 'edit-caption').value).toBe('本地简介')
  })

  it('本地书：四个字段全部可编辑（策略里本来就有）', () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    expect(disabled(wrapper, 'edit-title')).toBeUndefined()
    expect(disabled(wrapper, 'edit-author-id')).toBeUndefined()
    expect(disabled(wrapper, 'edit-series-id')).toBeUndefined()
    expect(disabled(wrapper, 'edit-caption')).toBeUndefined()
  })

  it('判定依据是字段策略本身，不是 source（本地书带 pixiv 形态的策略 → 照样置灰）', async () => {
    // 这条用例的意义：置灰**只读服务端那个数组**。若哪天有人在组件里重新按
    // source 推一份表，这里立刻红——而那正是这次要消灭的重复真相。
    const wrapper = mountModal({ ...LOCAL_NOVEL, editable_fields: PIXIV_EDITABLE })
    await flushPromises()

    expect(disabled(wrapper, 'edit-title')).toBeDefined()
    expect(disabled(wrapper, 'edit-author-id')).toBeDefined()
    expect(disabled(wrapper, 'edit-series-id')).toBeDefined()
    expect(disabled(wrapper, 'edit-caption')).toBeDefined()
    // 字段只读 → 一个解析请求都不发
    await settleLookup()
    expect(mocks.lookupAuthor).not.toHaveBeenCalled()
    expect(mocks.lookupSeries).not.toHaveBeenCalled()
  })

  it('editable_fields 缺失（老响应）→ 一律只读，宁可否决', () => {
    const legacy = { ...LOCAL_NOVEL, editable_fields: undefined } as unknown as Novel
    const wrapper = mountModal(legacy)

    expect(disabled(wrapper, 'edit-title')).toBeDefined()
    expect(disabled(wrapper, 'edit-author-id')).toBeDefined()
    expect(disabled(wrapper, 'edit-series-id')).toBeDefined()
    expect(disabled(wrapper, 'edit-caption')).toBeDefined()
  })

  it('不可编辑的字段不进 payload：同步刷新出的 title / author_id / series_id / caption 不会被回提交', async () => {
    // 真实的模型分叉路径：同步把响应的 title / author_id / series_id / caption 装进
    // 表单，而这四个字段在 pixiv 书上不可编辑（服务端收到就 400）。这里让响应只改
    // 这些只读字段 + 一个可编辑的 tags：保存必须只提交 tags。
    const refreshed: Novel = {
      ...PIXIV_NOVEL,
      title: '来源新标题',
      author_id: 999,
      series_id: 111,
      caption: '来源新简介',
      tags: ['来源新标签'],
    }
    mocks.syncNovel.mockResolvedValue(refreshed)
    mocks.updateNovel.mockResolvedValue(refreshed)
    const wrapper = mountModal(PIXIV_NOVEL)

    await wrapper.get('[data-testid="edit-sync"]').trigger('click')
    await flushPromises()

    // 表单确实被响应刷新（模型真的变了，不是因为字段置灰而"没变"）
    expect(input(wrapper, 'edit-title').value).toBe('来源新标题')
    expect(input(wrapper, 'edit-author-id').value).toBe('999')
    expect(input(wrapper, 'edit-series-id').value).toBe('111')
    expect(input(wrapper, 'edit-caption').value).toBe('来源新简介')

    await submit(wrapper)

    expect(mocks.updateNovel).toHaveBeenCalledTimes(1)
    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ tags: ['来源新标签'] })
  })

  it('不可编辑的字段不参与本地校验（pixiv 书 title 为空也不拦保存）', async () => {
    // 语料里有标题为空的 pixiv 行：标题置灰不可改，若校验仍拿它拦保存，
    // 这本书的标签/章节号就永远改不了了。
    const emptyTitle = { ...PIXIV_NOVEL, title: '' }
    mocks.updateNovel.mockResolvedValue({ ...emptyTitle, series_index: 5 })
    const wrapper = mountModal(emptyTitle)

    expect(wrapper.find('[data-testid="edit-validation-error"]').exists()).toBe(false)

    await wrapper.get('[data-testid="edit-series-index"]').setValue('5')
    await submit(wrapper)

    expect(mocks.updateNovel).toHaveBeenCalledWith(987654, { series_index: 5 })
  })

  it('策略中途收窄（source 仍是 local）→ 不可编辑字段既不拦校验也不进 payload', async () => {
    // 本地书 + 收窄的策略：source 说"可改"，字段策略说"不可改"，后者说了算。
    // 若有人把判定改回按 source 推表：空标题会卡住保存、模型里已有的标题会被
    // 回提交（服务端 400）——两种漂移都在这条用例里红。
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, tags: ['原有标签', '新标签'] })
    const wrapper = mountModal({ ...LOCAL_NOVEL, title: '' })

    // 收窄前：标题可编辑，空标题就该拦（校验本身没有失灵）
    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toBe('标题不能为空')

    // 收窄后：标题置灰 → 它既不拦校验……
    await wrapper.setProps({ novel: { ...LOCAL_NOVEL, title: '', editable_fields: PIXIV_EDITABLE } })
    expect(disabled(wrapper, 'edit-title')).toBeDefined()
    expect(wrapper.find('[data-testid="edit-validation-error"]').exists()).toBe(false)

    // ……也不进 payload
    await wrapper.get('[data-testid="edit-tag-input"]').setValue('新标签')
    await wrapper.get('[data-testid="edit-tag-input"]').trigger('keydown.enter')
    await submit(wrapper)

    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ tags: ['原有标签', '新标签'] })
  })

  it('策略收窄后，模型里已改过的只读字段不回提交（payload 按当前策略过滤）', async () => {
    // 置灰只保证"用户点不动"；模型里可能已经有值（策略中途收窄、同步刷新）。
    // 提交是最后一道闸：按当前 canEdit 过滤，而不是按 source。
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, tags: ['原有标签', '新标签'] })
    const wrapper = mountModal(LOCAL_NOVEL)

    await wrapper.get('[data-testid="edit-title"]').setValue('改过的标题')
    await wrapper.setProps({ novel: { ...LOCAL_NOVEL, editable_fields: PIXIV_EDITABLE } })

    await wrapper.get('[data-testid="edit-tag-input"]').setValue('新标签')
    await wrapper.get('[data-testid="edit-tag-input"]').trigger('keydown.enter')
    await submit(wrapper)

    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ tags: ['原有标签', '新标签'] })
  })

  it('静态说明全部删除（只剩"填 ID → 解析结果"的动态反馈）', () => {
    const pixiv = mountModal(PIXIV_NOVEL)
    const local = mountModal(LOCAL_NOVEL)

    for (const wrapper of [pixiv, local]) {
      const text = wrapper.text()
      expect(text).not.toContain('pixiv 来源会覆盖')
      expect(text).not.toContain('暂不支持编辑')
      expect(text).not.toContain('标签在服务端统一小写')
      expect(text).not.toContain('留空则清除日期')
      // 那两个静态说明的宿主节点也一并消失（不留空壳）
      expect(wrapper.find('[data-testid="edit-title-hint"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="edit-caption-hint"]').exists()).toBe(false)
    }
  })

  it('解析 hint 只在字段可编辑时渲染：pixiv 书两行 hint 都不出现（不占行高）', () => {
    const wrapper = mountModal(PIXIV_NOVEL)

    expect(wrapper.find('[data-testid="edit-author-hint"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="edit-series-hint"]').exists()).toBe(false)
  })

  it('简介：本地书可编辑（2026-09-22 起），pixiv 书仍置灰', () => {
    expect(disabled(mountModal(LOCAL_NOVEL), 'edit-caption')).toBeUndefined()
    expect(disabled(mountModal(PIXIV_NOVEL), 'edit-caption')).toBeDefined()
    // 这一栏与别处一样不摆静态说明
    expect(
      mountModal(LOCAL_NOVEL).find('[data-testid="edit-caption-hint"]').exists(),
    ).toBe(false)
  })

  it('顶部显示来源徽章与 id（徽章看来源，置灰看字段策略，两件事）', () => {
    expect(mountModal(LOCAL_NOVEL).get('[data-testid="edit-source-badge"]').text()).toBe('本地')
    expect(mountModal(PIXIV_NOVEL).get('[data-testid="edit-source-badge"]').text()).toBe('pixiv')
  })
})

describe('EditNovelModal 作者是 ID（2026-09-19 拍板）', () => {
  beforeEach(() => {
    mocks.updateNovel.mockReset()
    mocks.syncNovel.mockReset()
    mocks.lookupAuthor.mockReset()
    mocks.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: '新作者', source: 'pixiv', novel_count: 2,
    })
  })

  it('没有"作者名"输入框了：只剩作者 ID', () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    expect(wrapper.find('[data-testid="edit-author-id"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="edit-author"]').exists()).toBe(false)
    // 本地书的 ID 框是空的（上传时 author_id=NULL），占位符给形态提示
    expect(input(wrapper, 'edit-author-id').value).toBe('')
  })

  it('打开时预填当前作者 ID，且**不发查询**（卡片手里就有名字）', async () => {
    const wrapper = mountModal(LOCAL_WITH_AUTHOR)
    await flushPromises()

    expect(input(wrapper, 'edit-author-id').value).toBe('555')
    await settleLookup()
    expect(mocks.lookupAuthor).not.toHaveBeenCalled()
    expect(authorHint(wrapper)).toContain('当前作者：pixiv 作者')
  })

  it('填新 ID → 预览显示解析出的名字与库内本数', async () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    await wrapper.get('[data-testid="edit-author-id"]').setValue('777')
    expect(authorHint(wrapper)).toContain('查询中')
    await settleLookup()

    expect(mocks.lookupAuthor).toHaveBeenCalledWith(777)
    expect(authorHint(wrapper)).toContain('新作者')
    expect(authorHint(wrapper)).toContain('库内已有 2 本')
  })

  it('预览查不到名字：如实说明"仍可保存"，不拦保存', async () => {
    mocks.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: null, source: 'unknown', novel_count: 0,
    })
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, author_id: 777 })
    const wrapper = mountModal(LOCAL_NOVEL)

    await wrapper.get('[data-testid="edit-author-id"]').setValue('777')
    await settleLookup()

    expect(authorHint(wrapper)).toContain('未查到该作者')
    expect(wrapper.get('[data-testid="edit-author-id"]').attributes('aria-invalid')).toBeUndefined()
    await submit(wrapper)
    expect(mocks.updateNovel).toHaveBeenCalledWith(LOCAL_NOVEL.id, { author_id: 777 })
  })

  it('预览请求本身失败（网络抖动）→ 提示但不拦保存', async () => {
    mocks.lookupAuthor.mockRejectedValue(new Error('boom'))
    const wrapper = mountModal(LOCAL_NOVEL)

    await wrapper.get('[data-testid="edit-author-id"]').setValue('777')
    await settleLookup()

    expect(authorHint(wrapper)).toContain('查询失败')
    await submit(wrapper)
    expect(mocks.updateNovel).toHaveBeenCalledTimes(1)
  })

  it('非数字 / 0 → 本地校验拦住保存', async () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    await wrapper.get('[data-testid="edit-author-id"]').setValue('abc')
    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toBe('作者 ID 必须是正整数')
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="edit-author-id"]').setValue('0')
    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toBe('作者 ID 必须是正整数')
    await submit(wrapper)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('清空作者 ID → 显式提交 null（解除关联），hint 说明名字会保留', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_WITH_AUTHOR, author_id: null })
    const wrapper = mountModal(LOCAL_WITH_AUTHOR)

    await wrapper.get('[data-testid="edit-author-id"]').setValue('')
    expect(authorHint(wrapper)).toContain('解除作者关联')
    expect(authorHint(wrapper)).toContain('pixiv 作者')

    await submit(wrapper)
    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ author_id: null })
  })

  it('改回原 ID（输入后又撤回）→ 不发查询、不进 payload', async () => {
    const wrapper = mountModal(LOCAL_WITH_AUTHOR)

    await wrapper.get('[data-testid="edit-author-id"]').setValue('999')
    await wrapper.get('[data-testid="edit-author-id"]').setValue('555')
    await settleLookup()

    expect(mocks.lookupAuthor).not.toHaveBeenCalled()
    expect(authorHint(wrapper)).toContain('当前作者')
    await submit(wrapper)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('作者 ID 与标题**同一行**（与「系列 + 章节号」同构）', () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    const titleCell = wrapper.get('[data-testid="edit-title"]').element.parentElement
    const authorCell = wrapper.get('[data-testid="edit-author-id"]').element.parentElement
    expect(authorCell?.parentElement).toBe(titleCell?.parentElement)
    expect(titleCell?.parentElement?.className).toContain('flex')

    // 对照组：下面的系列 + 章节号也是同一套布局
    const seriesCell = wrapper.get('[data-testid="edit-series-id"]').element.parentElement
    const indexCell = wrapper.get('[data-testid="edit-series-index"]').element.parentElement
    expect(indexCell?.parentElement).toBe(seriesCell?.parentElement)
  })
})

describe('EditNovelModal 系列也是 ID（2026-09-19，与作者同构）', () => {
  beforeEach(() => {
    mocks.updateNovel.mockReset()
    mocks.syncNovel.mockReset()
    mocks.lookupAuthor.mockReset()
    mocks.lookupSeries.mockReset()
    mocks.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: '新作者', source: 'pixiv', novel_count: 0,
    })
    mocks.lookupSeries.mockResolvedValue({
      series_id: 888, series_name: '新系列', source: 'local', novel_count: 4,
    })
  })

  it('没有"系列名"输入框了：只剩系列 ID', () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    expect(wrapper.find('[data-testid="edit-series-id"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="edit-series"]').exists()).toBe(false)
    expect(input(wrapper, 'edit-series-id').value).toBe('')
  })

  it('填新系列 ID → 预览显示解析出的系列名与库内本数', async () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    await wrapper.get('[data-testid="edit-series-id"]').setValue('888')
    expect(seriesHint(wrapper)).toContain('查询中')
    await settleLookup()

    expect(mocks.lookupSeries).toHaveBeenCalledWith(888)
    expect(seriesHint(wrapper)).toContain('新系列')
    expect(seriesHint(wrapper)).toContain('库内已有 4 本')
  })

  it('本地书已挂系列：预填并在打开时零请求（卡片手里就有名字）', async () => {
    const wrapper = mountModal({ ...LOCAL_NOVEL, series_id: 606, series_name: '某系列' })
    await flushPromises()

    expect(input(wrapper, 'edit-series-id').value).toBe('606')
    await settleLookup()
    expect(mocks.lookupSeries).not.toHaveBeenCalled()
    expect(seriesHint(wrapper)).toContain('当前系列：某系列')
  })

  it('pixiv 书：系列 ID 置灰，且**一个查询都不发**（字段不可改就别问）', async () => {
    const wrapper = mountModal(PIXIV_NOVEL)
    await flushPromises()

    expect(disabled(wrapper, 'edit-series-id')).toBeDefined()
    expect(input(wrapper, 'edit-series-id').value).toBe('606')  // 仍显示当前值，只是改不了
    await settleLookup()
    expect(mocks.lookupSeries).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="edit-series-hint"]').exists()).toBe(false)
  })

  it('系列 ID 非正整数 → 本地校验拦住保存', async () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    await wrapper.get('[data-testid="edit-series-id"]').setValue('123abc')

    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toBe('系列 ID 必须是正整数')
    await submit(wrapper)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('只改系列 → 只提交 series_id（名字不由前端提交）', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, series_id: 888 })
    const wrapper = mountModal(LOCAL_NOVEL)

    await wrapper.get('[data-testid="edit-series-id"]').setValue('888')
    await submit(wrapper)

    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ series_id: 888 })
  })

  it('预览查不到系列名：如实说明"仍可保存"，不拦保存', async () => {
    mocks.lookupSeries.mockResolvedValue({
      series_id: 888, series_name: null, source: 'unknown', novel_count: 0,
    })
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, series_id: 888 })
    const wrapper = mountModal(LOCAL_NOVEL)

    await wrapper.get('[data-testid="edit-series-id"]').setValue('888')
    await settleLookup()

    expect(seriesHint(wrapper)).toContain('未查到该系列')
    await submit(wrapper)
    expect(mocks.updateNovel).toHaveBeenCalledWith(LOCAL_NOVEL.id, { series_id: 888 })
  })
})

describe('EditNovelModal 提交语义（§5 PATCH 三态）', () => {
  beforeEach(() => {
    mocks.updateNovel.mockReset()
    mocks.syncNovel.mockReset()
    mocks.lookupAuthor.mockReset()
    mocks.lookupSeries.mockReset()
    mocks.lookupAuthor.mockResolvedValue({
      author_id: 777, author_name: '新作者', source: 'pixiv', novel_count: 0,
    })
    mocks.lookupSeries.mockResolvedValue({
      series_id: 888, series_name: '新系列', source: 'local', novel_count: 0,
    })
  })

  it('只提交发生变化的字段', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, author_id: 777 })
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-author-id"]').setValue('777')
    await submit(wrapper)

    expect(mocks.updateNovel).toHaveBeenCalledTimes(1)
    expect(mocks.updateNovel.mock.calls[0][0]).toBe(LOCAL_NOVEL.id)
    // 只有 author_id 一个键——未改的字段不出现在 PATCH 体里，名字更不由前端提交
    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ author_id: 777 })
  })

  it('无改动时保存按钮禁用，且不发请求', async () => {
    const wrapper = mountModal()

    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()

    await submit(wrapper)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('清空系列 ID 会显式提交 null，且 hint 明说名字与章节号一起清', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, series_id: null })
    const wrapper = mountModal({ ...LOCAL_NOVEL, series_id: 606, series_name: '某系列' })

    await wrapper.get('[data-testid="edit-series-id"]').setValue('')

    // 与作者刻意不同：作者留名（见上一组用例），系列连名字与章节号一起清
    expect(seriesHint(wrapper)).toContain('解除系列关联')
    expect(seriesHint(wrapper)).toContain('清掉显示名「某系列」')

    await submit(wrapper)
    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ series_id: null })
  })

  it('标题为空 → 本地校验拦住保存并给出提示', async () => {
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-title"]').setValue('   ')

    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toBe('标题不能为空')
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()
    await submit(wrapper)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('章节号非数字 → 校验拦住', async () => {
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-series-index"]').setValue('三')

    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toContain('章节号')
  })

  it('章节号 0 → 校验拦住，文案与服务端 400 的 detail 一字不差', async () => {
    // 章节号是"系列里的第几篇"，从 1 起数；0 已被服务端判非法（400）。
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-series-index"]').setValue('0')

    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toBe('章节号必须是正整数')
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()
    await submit(wrapper)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('章节号 1 起算：填 1 合法可提交', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, series_index: 1 })
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-series-index"]').setValue('1')
    await submit(wrapper)

    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ series_index: 1 })
  })

  it('标签：回车添加（含分隔符切分）、× 删除', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, tags: ['原有标签', 'r-18', '魔法'] })
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-tag-input"]').setValue('R-18/魔法')
    await wrapper.get('[data-testid="edit-tag-input"]').trigger('keydown.enter')

    expect(wrapper.get('[data-testid="edit-tags"]').text()).toContain('R-18')
    expect(wrapper.get('[data-testid="edit-tags"]').text()).toContain('魔法')

    await wrapper.get('[data-testid="edit-tag-remove-R-18"]').trigger('click')
    expect(wrapper.get('[data-testid="edit-tags"]').text()).not.toContain('R-18')

    await submit(wrapper)
    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ tags: ['原有标签', '魔法'] })
  })

  it('简介：改了才提交，且 strip 后再 diff（只有空白不算改）', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, caption: '新简介' })
    const wrapper = mountModal()

    // 只在首尾敲了几个空格 → 与初值等价，不进 payload（保存按钮也不点亮）
    await wrapper.get('[data-testid="edit-caption"]').setValue('  本地简介  ')
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="edit-caption"]').setValue('  新简介  ')
    await submit(wrapper)

    // 提交的是 strip 后的值（服务端 _text 做同一套归一，两边口径一致）
    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ caption: '新简介' })
  })

  it('简介清空 → 显式提交 null（PATCH 三态里的"清空"）', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, caption: null })
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-caption"]').setValue('')
    await submit(wrapper)

    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ caption: null })
  })

  it('简介超长 → 本地校验拦住，文案与服务端 400 的 detail 一致', async () => {
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-caption"]').setValue('x'.repeat(2001))

    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toBe('简介过长（最多 2000 字）')
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()
    // textarea 自带 maxlength=2000 是同一口径的第一道闸
    expect(input(wrapper, 'edit-caption').getAttribute('maxlength')).toBe('2000')
    await submit(wrapper)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('保存成功：emit 更新后的对象并请求关闭', async () => {
    const updated = { ...LOCAL_NOVEL, author_id: 777, author_name: '新作者' }
    mocks.updateNovel.mockResolvedValue(updated)
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-author-id"]').setValue('777')
    await submit(wrapper)

    expect(wrapper.emitted('updated')?.[0]).toEqual([updated])
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('保存失败：emit error，不关闭', async () => {
    mocks.updateNovel.mockRejectedValue(new Error('boom'))
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-author-id"]').setValue('777')
    await submit(wrapper)

    expect(wrapper.emitted('error')?.[0]).toEqual(['保存失败'])
    expect(wrapper.emitted('close')).toBeFalsy()
    expect(wrapper.emitted('updated')).toBeFalsy()
  })
})

describe('EditNovelModal 「同步」（POST /api/novels/{id}/sync）', () => {
  beforeEach(() => {
    mocks.updateNovel.mockReset()
    mocks.syncNovel.mockReset()
    mocks.lookupAuthor.mockReset()
    mocks.lookupSeries.mockReset()
  })

  it('仅 pixiv 书显示：本地书没有来源可同步（服务端也会 400）', () => {
    expect(mountModal(PIXIV_NOVEL).find('[data-testid="edit-sync"]').exists()).toBe(true)
    expect(mountModal(LOCAL_NOVEL).find('[data-testid="edit-sync"]').exists()).toBe(false)
  })

  it('DOM 末位 → 渲染在最左（左下角）：footer 子节点顺序为 保存 / 取消 / 同步', () => {
    // 位置由 BaseModal 的 slot 落点决定：footer 是 row-reverse + 本 slot 在 DOM 末位，
    // 末位 DOM 才是视觉最左。只断言 DOM 顺序，样式类怎么调都不影响这条契约。
    const wrapper = mountModal(PIXIV_NOVEL)
    const sync = wrapper.get('[data-testid="edit-sync"]').element
    const footer = sync.parentElement
    expect(footer).not.toBeNull()

    const children = Array.from(footer!.children)
    expect(children).toHaveLength(3)
    expect(children[0]).toBe(wrapper.get('[data-testid="base-modal"] button[type="submit"]').element)
    expect((children[1] as HTMLElement).textContent?.trim()).toBe('取消')
    expect(children[2]).toBe(sync)
  })

  it('本地书：footer 里只有保存与取消，末位没有次要动作', () => {
    const wrapper = mountModal(LOCAL_NOVEL)
    const footer = wrapper.get('[data-testid="base-modal"] button[type="submit"]').element.parentElement

    // 条件为假的 slot 内容只留注释节点，不进元素子节点列表
    expect(Array.from(footer!.children)).toHaveLength(2)
  })

  it('type="button"：点它不会顺手提交表单（footer 在 <form> 里）', async () => {
    mocks.syncNovel.mockResolvedValue(PIXIV_NOVEL)
    const wrapper = mountModal(PIXIV_NOVEL)

    expect(wrapper.get('[data-testid="edit-sync"]').attributes('type')).toBe('button')

    await wrapper.get('[data-testid="edit-sync"]').trigger('click')
    await flushPromises()

    expect(mocks.syncNovel).toHaveBeenCalledWith(987654)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('同步成功：表单被响应刷新，emit updated，弹窗不关', async () => {
    const refreshed: Novel = {
      ...PIXIV_NOVEL,
      title: '来源新标题',
      author_id: 999,
      author_name: '来源新作者',
      series_id: 111,
      series_name: '来源新系列',
      series_index: 7,
      tags: ['来源新标签'],
      create_time: '2026-01-02T00:00:00+09:00',
    }
    mocks.syncNovel.mockResolvedValue(refreshed)
    const wrapper = mountModal(PIXIV_NOVEL)

    await wrapper.get('[data-testid="edit-sync"]').trigger('click')
    await flushPromises()

    // 初值换成响应值（否则用户看到的是同步前的旧数据）
    expect(input(wrapper, 'edit-title').value).toBe('来源新标题')
    expect(input(wrapper, 'edit-author-id').value).toBe('999')
    expect(input(wrapper, 'edit-series-id').value).toBe('111')
    expect(input(wrapper, 'edit-series-index').value).toBe('7')
    expect(input(wrapper, 'edit-date').value).toBe('2026-01-02')
    expect(wrapper.get('[data-testid="edit-tags"]').text()).toContain('来源新标签')

    // 父组件据此原地替换卡片；弹窗留着让用户接着改
    expect(wrapper.emitted('updated')?.[0]).toEqual([refreshed])
    expect(wrapper.emitted('close')).toBeFalsy()
    expect(wrapper.emitted('error')).toBeFalsy()
    // 置灰仍按字段策略（本用例没换 props，策略还是挂载时那份）
    expect(disabled(wrapper, 'edit-title')).toBeDefined()
  })

  it('字段策略跟着 props 走：父组件换掉卡片对象后，置灰随之更新', async () => {
    // 同步响应自带 editable_fields，父组件会把它换成弹窗的初值（Novels.vue::
    // handleNovelUpdated）——所以"谁能改"必须每次从 props 读，不能挂载时定死。
    const wrapper = mountModal(PIXIV_NOVEL)
    expect(disabled(wrapper, 'edit-title')).toBeDefined()

    await wrapper.setProps({ novel: { ...PIXIV_NOVEL, editable_fields: LOCAL_EDITABLE } })

    expect(disabled(wrapper, 'edit-title')).toBeUndefined()
    expect(wrapper.find('[data-testid="edit-author-hint"]').exists()).toBe(true)
  })

  it('同步失败：emit error（与保存失败同一条路），弹窗不关、初值不动', async () => {
    mocks.syncNovel.mockRejectedValue(new Error('boom'))
    const wrapper = mountModal(PIXIV_NOVEL)

    await wrapper.get('[data-testid="edit-sync"]').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('error')?.[0]).toEqual(['同步失败'])
    expect(wrapper.emitted('updated')).toBeFalsy()
    expect(wrapper.emitted('close')).toBeFalsy()
    expect(input(wrapper, 'edit-title').value).toBe('pixiv 标题')
  })

  it('执行期间禁用按钮防连点，同时禁用保存；完成后恢复', async () => {
    let resolveSync: (novel: Novel) => void = () => {}
    mocks.syncNovel.mockImplementation(
      () => new Promise<Novel>((resolve) => { resolveSync = resolve }),
    )
    const wrapper = mountModal(PIXIV_NOVEL)

    // 先制造一个改动，让保存按钮本来是可用状态
    await wrapper.get('[data-testid="edit-series-index"]').setValue('5')
    expect(submitButton(wrapper).attributes('disabled')).toBeUndefined()

    await wrapper.get('[data-testid="edit-sync"]').trigger('click')

    const syncButton = wrapper.get('[data-testid="edit-sync"]')
    expect(syncButton.attributes('disabled')).toBeDefined()
    expect(syncButton.text()).toContain('同步中')
    // 两个请求写的是同一本书 → 同步期间保存也禁用
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()

    // 防连点靠 disabled（@vue/test-utils 对 disabled 元素的 trigger 本身就不派发）
    await syncButton.trigger('click')
    expect(mocks.syncNovel).toHaveBeenCalledTimes(1)

    resolveSync({ ...PIXIV_NOVEL, series_index: 7 })
    await flushPromises()

    expect(wrapper.get('[data-testid="edit-sync"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="edit-sync"]').text()).toBe('同步')
    expect(mocks.syncNovel).toHaveBeenCalledTimes(1)
  })
})
