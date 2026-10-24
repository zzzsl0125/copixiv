import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import NovelCard from '../../src/components/features/NovelCard.vue'
import type { Novel, TagPreference } from '../../src/types'

vi.mock('../../src/api', () => ({
  novelApi: {
    toggleFavourite: vi.fn(),
    toggleSpecialFollow: vi.fn(),
    downloadUrl: vi.fn(() => '/api/novels/42/download?format=txt'),
  },
}))

const novel: Novel = {
  id: 42,
  title: '测试小说',
  author_id: 7,
  author_name: '测试作者',
  like: 123,
  view: 456,
  text: 789,
  create_time: '2024-01-02',
  has_epub: 1,
  tags: ['tag-a', 'tag-b'],
  is_favourite: 0,
  is_special_follow: 0,
  source: 'pixiv',
  // 后端 NovelBase 下发的字段策略（本用例不关心，给 pixiv 形态即可）。
  editable_fields: ['series_index', 'tags', 'create_time'],
}

function mountCard(props: Record<string, unknown> = {}, attachTo?: HTMLElement) {
  return mount(NovelCard, {
    props: {
      novel,
      isActive: false,
      tagPreferences: [],
      batchMode: false,
      batchSelected: false,
      ...props,
    },
    attachTo,
  })
}

describe('NovelCard 移动端抽屉（下载/收藏/追更）', () => {
  it('点击卡片触发 toggle-active，且阻止冒泡（父容器点击收起不得取消本次打开）', async () => {
    const containerClicks = vi.fn()
    const parent = document.createElement('div')
    parent.addEventListener('click', containerClicks)

    const wrapper = mountCard({ isActive: false }, parent)
    await wrapper.get('.group').trigger('click')

    // 抽屉要能打开：toggle-active 必须送达父组件
    expect(wrapper.emitted('toggle-active')).toEqual([[42]])
    // 关键回归：事件不得继续冒泡到容器层的 @click="activeCardId = null"，
    // 否则 active 会被立刻复位，移动端抽屉永远打不开。
    expect(containerClicks).not.toHaveBeenCalled()
  })

  it('激活时抽屉滑出（translate-y-0!），未激活时保持隐藏（translate-y-full）', () => {
    const hidden = mountCard({ isActive: false })
    const overlay = hidden.get('.translate-y-full')
    expect(overlay.classes()).toContain('translate-y-full')
    expect(overlay.classes()).not.toContain('translate-y-0!')

    const shown = mountCard({ isActive: true })
    const shownOverlay = shown.get('.translate-y-full')
    expect(shownOverlay.classes()).toContain('translate-y-0!')
    expect(shownOverlay.text()).toContain('下载')
    expect(shownOverlay.text()).toContain('收藏')
    expect(shownOverlay.text()).toContain('追更')
  })

  it('批量模式下点击卡片走批量勾选，同样阻止冒泡', async () => {
    const containerClicks = vi.fn()
    const parent = document.createElement('div')
    parent.addEventListener('click', containerClicks)

    const wrapper = mountCard({ batchMode: true, batchSelected: false }, parent)
    await wrapper.get('.group').trigger('click')

    expect(wrapper.emitted('toggle-batch-select')).toEqual([[42]])
    expect(containerClicks).not.toHaveBeenCalled()
  })

  it('批量模式下不渲染抽屉覆盖层', () => {
    const wrapper = mountCard({ batchMode: true, isActive: true })
    expect(wrapper.find('.translate-y-full').exists()).toBe(false)
  })
})

describe('NovelCard 操作条第 4 个按钮：编辑（元数据编辑入口）', () => {
  it('点击把**完整** Novel 交给父组件（初值不再请求一次）', async () => {
    const wrapper = mountCard()

    const button = wrapper.get('[data-testid="novel-edit"]')
    expect(button.text()).toBe('编辑')

    await button.trigger('click')
    expect(wrapper.emitted('edit')).toEqual([[novel]])
  })

  it('点击不冒泡、也不切换抽屉（否则会边开弹窗边收起）', async () => {
    const containerClicks = vi.fn()
    const parent = document.createElement('div')
    parent.addEventListener('click', containerClicks)
    const wrapper = mountCard({ isActive: true }, parent)

    await wrapper.get('[data-testid="novel-edit"]').trigger('click')

    expect(containerClicks).not.toHaveBeenCalled()
    expect(wrapper.emitted('toggle-active')).toBeFalsy()
  })

  it('批量模式不渲染编辑按钮（整条抽屉本来就不渲染）', () => {
    const wrapper = mountCard({ batchMode: true, isActive: true })
    expect(wrapper.find('[data-testid="novel-edit"]').exists()).toBe(false)
  })
})

describe('NovelCard 小标签区（本地 / EPUB 同一区域）', () => {
  it('pixiv 书（source 缺省或 pixiv）不显示「本地」标签', () => {
    expect(mountCard().find('[data-testid="badge-local"]').exists()).toBe(false)
    expect(mountCard({ novel: { ...novel, source: 'pixiv' } }).find('[data-testid="badge-local"]').exists()).toBe(false)
  })

  it('source=local 显示「本地」标签，且不影响既有布局', () => {
    const wrapper = mountCard({ novel: { ...novel, id: -999999999, source: 'local' } })

    const badge = wrapper.get('[data-testid="badge-local"]')
    expect(badge.text()).toBe('本地')
    // 标题 / 作者 / 标签 / 下载按钮照旧（§8-4：先显示徽章，其余不动）
    expect(wrapper.text()).toContain('测试小说')
    expect(wrapper.text()).toContain('测试作者')
    expect(wrapper.text()).toContain('tag-a')
  })

  it('两个标签排在同一区域内（后续扩展只需加一条 badges 数据）', () => {
    // has_epub=2 → EPUB 就绪；source=local → 本地
    const wrapper = mountCard({ novel: { ...novel, source: 'local', has_epub: 2 } })

    const area = wrapper.get('[data-testid="novel-badges"]')
    expect(area.find('[data-testid="badge-local"]').exists()).toBe(true)
    expect(area.find('[data-testid="badge-epub"]').exists()).toBe(true)
    // 顺序稳定：本地在前、EPUB 在后
    expect(area.text().replace(/\s+/g, '')).toBe('本地EPUB')
  })

  it('只有 EPUB 就绪时不出现标签区以外的重复标签（单源渲染）', () => {
    const wrapper = mountCard({ novel: { ...novel, has_epub: 2 } })

    expect(wrapper.findAll('[data-testid="badge-epub"]')).toHaveLength(1)
    expect(wrapper.get('[data-testid="novel-badges"]').text().trim()).toBe('EPUB')
  })
})

// ---------------------------------------------------------------------------
// 渲染成本：每卡固定开销（标签偏好 Map 预索引 / 具体过渡属性 / 按需 backdrop-filter）
// ---------------------------------------------------------------------------

/** 13 标签的卡（实测平均每卡 15.6 个标签，取接近上限的样本）。 */
const manyTags = Array.from({ length: 13 }, (_, i) => `tag-${i}`)
const manyTagNovel: Novel = { ...novel, id: 43, tags: manyTags }

/** 19 条偏好（与线上规模一致），标签名故意都不在卡的标签集里，
 *  这样「一次都没命中」的线性查找才需要扫完整个数组（旧实现的最坏路径）。 */
const nineteenPrefs: TagPreference[] = Array.from({ length: 19 }, (_, i) => ({
  id: i + 1,
  tag: `pref-${i}`,
  preference: i % 2 === 0 ? 'favourite' : 'blocked',
  sort_index: i,
}))

/** 统计「按数字下标读取元素」的次数。
 *
 * 旧实现 `tagPreferences.find(...)` 每个标签都会从头扫一遍数组 →
 * 13 标签 × 19 条 = 最多 247 次下标读取；预索引后每张卡只遍历一次数组（19 次）。
 * 用读取次数而不是耗时断言，CI 上不会 flaky。
 */
function countingPreferences(prefs: TagPreference[]) {
  const reads = { index: 0 }
  const proxy = new Proxy(prefs, {
    get(target, prop, receiver) {
      if (typeof prop === 'string' && /^\d+$/.test(prop)) reads.index += 1
      return Reflect.get(target, prop, receiver)
    },
  })
  return { proxy, reads }
}

function tagSpan(wrapper: ReturnType<typeof mountCard>, tag: string) {
  const span = wrapper.findAll('span').find(el => el.text() === tag)
  if (!span) throw new Error(`标签 ${tag} 未渲染`)
  return span
}

describe('NovelCard 标签偏好：Map 预索引（不再逐标签线性 find）', () => {
  it('偏好数组的读取次数与标签数无关（旧实现随标签数线性放大）', () => {
    const oneTag = countingPreferences(nineteenPrefs)
    mountCard({ novel: { ...manyTagNovel, tags: ['tag-0'] }, tagPreferences: oneTag.proxy })

    const thirteenTags = countingPreferences(nineteenPrefs)
    mountCard({ novel: manyTagNovel, tagPreferences: thirteenTags.proxy })

    // 旧实现 13 标签要扫 13 遍（≈247 次），1 标签只扫 1 遍；预索引两者相同。
    expect(thirteenTags.reads.index).toBeGreaterThan(0) // 确实读了（建了索引）
    expect(thirteenTags.reads.index).toBe(oneTag.reads.index)
    // 每张卡最多把数组过两遍：一次建 Map，一次 Vue 对 props 数组的响应式遍历。
    expect(thirteenTags.reads.index).toBeLessThanOrEqual(2 * nineteenPrefs.length)
  })

  it('30 张卡共用同一份偏好：总读取量不随标签数放大', () => {
    const { proxy, reads } = countingPreferences(nineteenPrefs)
    for (let i = 0; i < 30; i++) {
      mountCard({ novel: { ...manyTagNovel, id: 1000 + i }, tagPreferences: proxy })
    }

    // 旧实现：30 × 13 × 19 ≈ 7410；预索引：30 × 2 × 19 = 1140（与标签数无关）。
    expect(reads.index).toBeLessThanOrEqual(30 * 2 * nineteenPrefs.length)
  })

  it('配色语义逐字不变：favourite / blocked / 无偏好', () => {
    const prefs: TagPreference[] = [
      { id: 1, tag: 'tag-0', preference: 'favourite', sort_index: 0 },
      { id: 2, tag: 'tag-1', preference: 'blocked', sort_index: 1 },
    ]
    const wrapper = mountCard({ novel: manyTagNovel, tagPreferences: prefs })

    expect(tagSpan(wrapper, 'tag-0').classes()).toContain('bg-blue-100')
    expect(tagSpan(wrapper, 'tag-0').classes()).toContain('text-blue-800')
    expect(tagSpan(wrapper, 'tag-0').classes()).toContain('font-bold')

    expect(tagSpan(wrapper, 'tag-1').classes()).toContain('line-through')
    expect(tagSpan(wrapper, 'tag-1').classes()).toContain('text-red-400')
    expect(tagSpan(wrapper, 'tag-1').classes()).toContain('bg-gray-50')

    expect(tagSpan(wrapper, 'tag-2').classes()).toContain('bg-gray-100')
    expect(tagSpan(wrapper, 'tag-2').classes()).toContain('text-gray-800')
  })

  it('偏好变化后颜色跟着变（索引必须响应 tagPreferences）', async () => {
    const wrapper = mountCard({ novel: manyTagNovel, tagPreferences: [] })
    expect(tagSpan(wrapper, 'tag-0').classes()).toContain('bg-gray-100')

    await wrapper.setProps({
      tagPreferences: [{ id: 1, tag: 'tag-0', preference: 'favourite', sort_index: 0 }],
    })
    expect(tagSpan(wrapper, 'tag-0').classes()).toContain('bg-blue-100')

    await wrapper.setProps({
      tagPreferences: [{ id: 2, tag: 'tag-0', preference: 'blocked', sort_index: 0 }],
    })
    expect(tagSpan(wrapper, 'tag-0').classes()).toContain('line-through')
    expect(tagSpan(wrapper, 'tag-0').classes()).not.toContain('bg-blue-100')

    await wrapper.setProps({ tagPreferences: [] })
    expect(tagSpan(wrapper, 'tag-0').classes()).toContain('bg-gray-100')
  })

  it('同一标签重复出现时保持「先到先得」（与原来的 find 语义一致）', () => {
    const wrapper = mountCard({
      novel: manyTagNovel,
      tagPreferences: [
        { id: 1, tag: 'tag-0', preference: 'favourite', sort_index: 0 },
        { id: 2, tag: 'tag-0', preference: 'blocked', sort_index: 1 },
      ],
    })

    expect(tagSpan(wrapper, 'tag-0').classes()).toContain('bg-blue-100')
  })
})

describe('NovelCard 卡片根：transition-all 收敛到具体属性', () => {
  /** 取根元素上的过渡属性清单（`transition-shadow` / `transition-[a,b]`）。 */
  function transitionProps(classes: string[]): string[] | null {
    const util = classes.find(c => c.startsWith('transition-') && c !== 'transition-colors')
    if (!util) return null
    if (util === 'transition-shadow') return ['box-shadow']
    if (util === 'transition-transform') return ['transform']
    const arbitrary = /^transition-\[(.+)\]$/.exec(util)
    return arbitrary ? arbitrary[1]!.split(',').map(s => s.trim()) : []
  }

  it('不再使用 transition-all，但仍然有 300ms 过渡', () => {
    const classes = mountCard().get('.group').classes()

    expect(classes).not.toContain('transition-all')
    expect(classes).toContain('duration-300')
    expect(transitionProps(classes)).not.toBeNull()
  })

  it('过渡属性覆盖 hover 阴影与 likeBorderClass 会动的边框/阴影', () => {
    // likeBorderClass 会在 likes 档位/批量选中时切换 ring-* / border-* / shadow-[...]，
    // 这些全部走 box-shadow 与 border-color，必须在过渡清单里，否则视觉会和原来不一致。
    const props = transitionProps(mountCard({
      novel: { ...novel, like: 5000 },
      batchMode: true,
      batchSelected: true,
    }).get('.group').classes())!

    expect(props).toContain('box-shadow')
    expect(props).toContain('border-color')
  })
})

describe('NovelCard 操作条：backdrop-filter 只在需要时存在', () => {
  const isBackdropUtility = (c: string) => /^backdrop-blur/.test(c)

  it('默认态（未 hover / 非 active / 非 batchMode）没有任何常驻 backdrop-filter', () => {
    const overlay = mountCard({ isActive: false }).get('.translate-y-full')

    // 默认态不能出现会立即生效的 backdrop-blur 工具类（否则每卡一个合成层）。
    expect(overlay.classes().filter(isBackdropUtility)).toEqual([])
    // 磨砂只在 hover 时挂上（group-hover 变体）。
    expect(overlay.classes()).toContain('group-hover:backdrop-blur-sm')
  })

  it('激活（移动端抽屉展开）时保留磨砂底', () => {
    const overlay = mountCard({ isActive: true }).get('.translate-y-full')

    expect(overlay.classes()).toContain('backdrop-blur-sm')
    expect(overlay.classes().filter(isBackdropUtility)).toHaveLength(1)
  })

  it('批量模式不渲染操作条，自然也没有 backdrop-filter', () => {
    const wrapper = mountCard({ batchMode: true, isActive: true })

    expect(wrapper.find('.translate-y-full').exists()).toBe(false)
    expect(wrapper.find('[class*="backdrop-blur"]').exists()).toBe(false)
  })
})
