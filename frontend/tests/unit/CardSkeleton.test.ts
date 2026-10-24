import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import CardSkeleton from '../../src/components/ui/CardSkeleton.vue'

describe('CardSkeleton 列表加载骨架屏', () => {
  it('渲染出骨架块（标题 / 作者 / 标签 / 页脚都有占位）', () => {
    const wrapper = mount(CardSkeleton)

    const blocks = wrapper.findAll('[data-testid="skeleton-block"]')
    expect(blocks.length).toBeGreaterThanOrEqual(8)
    // 每个占位块都得有可见的底色，否则骨架屏在页面上等于没有。
    for (const block of blocks) {
      expect(block.classes()).toContain('bg-gray-200')
    }
    // 页脚（真实卡片里 px-5 py-4 bg-gray-50 那一行）也要占位，避免加载完高度跳变。
    expect(wrapper.get('[data-testid="skeleton-footer"]').classes()).toContain('bg-gray-50')
  })

  it('带 animate-pulse（Tailwind 自带）', () => {
    const wrapper = mount(CardSkeleton)

    expect(wrapper.get('[data-testid="card-skeleton"]').classes()).toContain('animate-pulse')
  })

  it('外框沿用 NovelCard 的几何样式（尺寸大致一致，加载完不跳）', () => {
    const classes = mount(CardSkeleton).get('[data-testid="card-skeleton"]').classes()

    expect(classes).toContain('rounded-xl')
    expect(classes).toContain('border')
    expect(classes).toContain('flex')
    expect(classes).toContain('flex-col')
    // 骨架卡不应自带 hover / 操作条等真实卡片才有的交互开销。
    expect(classes).not.toContain('group')
    expect(classes).not.toContain('cursor-pointer')
  })

  it('不含任何真实内容，也不接受 novel prop（纯占位）', () => {
    const wrapper = mount(CardSkeleton)

    // 全是占位条：没有任何文字
    expect(wrapper.text().trim()).toBe('')
    // 结构上也没有真实卡片的测试钩子（badges / edit）
    expect(wrapper.find('[data-testid="novel-badges"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="novel-edit"]').exists()).toBe(false)
    // 组件本身没有 novel 这个 prop —— 接线方只负责 v-for 重复挂载。
    const props = (CardSkeleton as unknown as { props?: Record<string, unknown> }).props ?? {}
    expect(Object.keys(props)).not.toContain('novel')
  })

  it('对读屏软件隐藏（骨架屏只是视觉占位）', () => {
    const wrapper = mount(CardSkeleton)
    expect(wrapper.get('[data-testid="card-skeleton"]').attributes('aria-hidden')).toBe('true')
  })
})
