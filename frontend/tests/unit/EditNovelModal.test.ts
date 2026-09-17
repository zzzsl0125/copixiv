import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import EditNovelModal from '../../src/components/features/EditNovelModal.vue'
import type { Novel } from '../../src/types'

/**
 * 元数据编辑弹窗（docs/NOVEL_EDIT_DESIGN.md §4 / §5 / §6.2）。
 *
 * 两条 2026-09-18 拍板直接钉在这里：
 * - `title` / `series_name` **仅本地书可编辑**（pixiv 书置灰 + 说明）；
 * - `caption` 全局不可编辑（置灰 + "暂不支持编辑"）。
 *
 * 再加三条行为契约：只提交**变化**字段（PATCH 三态）、本地校验拦住保存、
 * 保存成功 emit 更新后的对象并由父组件关闭。
 */

const mocks = vi.hoisted(() => ({ updateNovel: vi.fn() }))

vi.mock('../../src/api', () => ({
  novelApi: { updateNovel: mocks.updateNovel },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

const LOCAL_NOVEL: Novel = {
  id: -999000001,
  title: '本地书名',
  author_id: undefined as unknown as number,
  author_name: '未知作者',
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
}

const PIXIV_NOVEL: Novel = {
  ...LOCAL_NOVEL,
  id: 987654,
  title: 'pixiv 标题',
  author_id: 555,
  author_name: 'pixiv 作者',
  series_name: 'pixiv 系列',
  series_index: 3,
  source: 'pixiv',
}

function mountModal(novel: Novel = LOCAL_NOVEL) {
  return mount(EditNovelModal, {
    props: { isOpen: true, novel },
  })
}

async function submit(wrapper: ReturnType<typeof mountModal>) {
  await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
  await flushPromises()
}

describe('EditNovelModal 来源规则（§4 拍板）', () => {
  beforeEach(() => {
    mocks.updateNovel.mockReset()
  })

  it('pixiv 书：标题与系列置灰并给出"以来源为准"说明', () => {
    const wrapper = mountModal(PIXIV_NOVEL)

    expect(wrapper.get('[data-testid="edit-title"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="edit-series"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="edit-title-hint"]').text()).toContain('pixiv 来源会覆盖')
    expect(wrapper.get('[data-testid="edit-series-hint"]').text()).toContain('pixiv 来源会覆盖')
    // 其余四个字段照旧可编辑
    expect(wrapper.get('[data-testid="edit-author"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="edit-series-index"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="edit-date"]').attributes('disabled')).toBeUndefined()
    // 作者名是**本书这一列**，改了不动 author 表——这条要说明，避免误解成"给作者改名"
    expect(wrapper.get('[data-testid="edit-author-hint"]').text()).toContain('只改本书的显示名')
  })

  it('本地书：标题与系列可编辑，且不显示来源说明', () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    expect(wrapper.get('[data-testid="edit-title"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="edit-series"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="edit-title-hint"]').exists()).toBe(false)
  })

  it('简介全局置灰 + "暂不支持编辑"', () => {
    const wrapper = mountModal(LOCAL_NOVEL)

    expect(wrapper.get('[data-testid="edit-caption"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="edit-caption-hint"]').text()).toBe('暂不支持编辑')
  })

  it('顶部显示来源徽章与 id', () => {
    expect(mountModal(LOCAL_NOVEL).get('[data-testid="edit-source-badge"]').text()).toBe('本地')
    expect(mountModal(PIXIV_NOVEL).get('[data-testid="edit-source-badge"]').text()).toBe('pixiv')
  })
})

describe('EditNovelModal 提交语义（§5 PATCH 三态）', () => {
  beforeEach(() => {
    mocks.updateNovel.mockReset()
  })

  it('只提交发生变化的字段', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, author_name: '新作者' })
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-author"]').setValue('新作者')
    await submit(wrapper)

    expect(mocks.updateNovel).toHaveBeenCalledTimes(1)
    expect(mocks.updateNovel.mock.calls[0][0]).toBe(LOCAL_NOVEL.id)
    // 只有 author_name 一个键——未改的字段不出现在 PATCH 体里
    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ author_name: '新作者' })
  })

  it('无改动时保存按钮禁用，且不发请求', async () => {
    const wrapper = mountModal()

    const confirm = wrapper.get('[data-testid="base-modal"] button[type="submit"]')
    expect(confirm.attributes('disabled')).toBeDefined()

    await submit(wrapper)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('清空系列名会显式提交 null（PATCH 的清空语义）', async () => {
    mocks.updateNovel.mockResolvedValue({ ...LOCAL_NOVEL, series_name: undefined })
    const wrapper = mountModal({ ...LOCAL_NOVEL, series_name: '某系列' })

    await wrapper.get('[data-testid="edit-series"]').setValue('')
    await submit(wrapper)

    expect(mocks.updateNovel.mock.calls[0][1]).toEqual({ series_name: null })
  })

  it('标题为空 → 本地校验拦住保存并给出提示', async () => {
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-title"]').setValue('   ')

    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toBe('标题不能为空')
    expect(
      wrapper.get('[data-testid="base-modal"] button[type="submit"]').attributes('disabled'),
    ).toBeDefined()
    await submit(wrapper)
    expect(mocks.updateNovel).not.toHaveBeenCalled()
  })

  it('pixiv 书标题为空时仍可改其它字段（置灰字段不参与本地校验）', async () => {
    // 语料里有标题为空的 pixiv 行：标题置灰不可改，若校验仍拿它拦保存，
    // 这本书的标签/作者就永远改不了了。
    const emptyTitle = { ...PIXIV_NOVEL, title: '' }
    mocks.updateNovel.mockResolvedValue({ ...emptyTitle, author_name: '新作者' })
    const wrapper = mountModal(emptyTitle)

    expect(wrapper.find('[data-testid="edit-validation-error"]').exists()).toBe(false)

    await wrapper.get('[data-testid="edit-author"]').setValue('新作者')
    await submit(wrapper)

    expect(mocks.updateNovel).toHaveBeenCalledWith(987654, { author_name: '新作者' })
  })

  it('章节号非数字 → 校验拦住', async () => {
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-series-index"]').setValue('三')

    expect(wrapper.get('[data-testid="edit-validation-error"]').text()).toContain('章节号')
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

  it('保存成功：emit 更新后的对象并请求关闭', async () => {
    const updated = { ...LOCAL_NOVEL, author_name: '新作者' }
    mocks.updateNovel.mockResolvedValue(updated)
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-author"]').setValue('新作者')
    await submit(wrapper)

    expect(wrapper.emitted('updated')?.[0]).toEqual([updated])
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('保存失败：emit error，不关闭', async () => {
    mocks.updateNovel.mockRejectedValue(new Error('boom'))
    const wrapper = mountModal()

    await wrapper.get('[data-testid="edit-author"]').setValue('新作者')
    await submit(wrapper)

    expect(wrapper.emitted('error')?.[0]).toEqual(['保存失败'])
    expect(wrapper.emitted('close')).toBeFalsy()
    expect(wrapper.emitted('updated')).toBeFalsy()
  })
})
