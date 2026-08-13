import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SearchHistoryPopup from '../../src/components/features/SearchHistoryPopup.vue'

describe('SearchHistoryPopup', () => {
  const history = [
    { id: 1, type: 'keyword', value: 'R-18', display_value: undefined, timestamp: 't1' },
    { id: 2, type: 'author_id', value: '12345', display_value: '作者A', timestamp: 't2' },
  ]

  it('renders items with display_value fallback and type badge', () => {
    const wrapper = mount(SearchHistoryPopup, {
      props: { history, show: true },
    })
    const text = wrapper.text()
    expect(text).toContain('R-18')
    expect(text).toContain('作者A')
    expect(text).toContain('author_id')  // non-keyword types get a badge
  })

  it('hides itself when show is false or history is empty', () => {
    expect(mount(SearchHistoryPopup, { props: { history, show: false } }).html()).not.toContain('搜索历史')
    expect(mount(SearchHistoryPopup, { props: { history: [], show: true } }).html()).not.toContain('搜索历史')
  })

  it('emits select-item with the full item on click', async () => {
    const wrapper = mount(SearchHistoryPopup, { props: { history, show: true } })
    await wrapper.findAll('li')[0].trigger('click')
    expect(wrapper.emitted('select-item')![0]).toEqual([history[0]])
  })

  it('emits delete-item with the id', async () => {
    const wrapper = mount(SearchHistoryPopup, { props: { history, show: true } })
    await wrapper.findAll('li')[1].find('button').trigger('click')
    expect(wrapper.emitted('delete-item')![0]).toEqual([2])
  })

  it('emits clear-all', async () => {
    const wrapper = mount(SearchHistoryPopup, { props: { history, show: true } })
    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('clear-all')).toHaveLength(1)
  })
})
