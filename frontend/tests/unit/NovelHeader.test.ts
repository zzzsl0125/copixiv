import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import NovelHeader from '../../src/components/features/NovelHeader.vue'
import SearchHistoryPopup from '../../src/components/features/SearchHistoryPopup.vue'

const searchHistoryApiMock = vi.hoisted(() => ({
  getSearchHistory: vi.fn(),
  deleteSearchHistoryItem: vi.fn(),
  clearSearchHistory: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  searchHistoryApi: searchHistoryApiMock,
}))

describe('NovelHeader search history', () => {
  it('closes the history dropdown when a search is submitted with Enter', async () => {
    searchHistoryApiMock.getSearchHistory.mockResolvedValue([
      { id: 1, type: 'keyword', value: 'R-18', display_value: null, timestamp: 't1' },
    ])

    const wrapper = mount(NovelHeader, {
      props: {
        filters: { keyword: '', order_by: 'random', order_direction: 'DESC' },
      },
      global: {
        stubs: { AppLogo: true },
      },
    })

    const input = wrapper.find('input[aria-label="搜索小说"]')
    await input.setValue('R-18')
    await input.trigger('focus')
    await flushPromises()

    expect(wrapper.findComponent(SearchHistoryPopup).props('show')).toBe(true)

    await input.trigger('keyup.enter')
    await flushPromises()

    expect(wrapper.findComponent(SearchHistoryPopup).props('show')).toBe(false)
  })
})
