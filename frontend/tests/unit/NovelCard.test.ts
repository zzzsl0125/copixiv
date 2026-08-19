import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import NovelCard from '../../src/components/features/NovelCard.vue'
import type { Novel } from '../../src/types'

vi.mock('../../src/api', () => ({
  novelApi: {
    toggleFavourite: vi.fn(),
    toggleSpecialFollow: vi.fn(),
    downloadNovel: vi.fn(),
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
