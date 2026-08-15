import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskHistoryList from '../../src/components/features/TaskHistoryList.vue'

describe('TaskHistoryList (lowercase status contract)', () => {
  it('shows the success icon for backend status "success"', () => {
    const wrapper = mount(TaskHistoryList, {
      props: {
        history: [
          {
            id: 1,
            name: 'test',
            arguments: null,
            status: 'success',
            start_time: '2026-08-16T01:00:00',
            end_time: null,
            duration: 1.5,
            result: null,
          },
        ],
        loading: false,
        hasMore: false,
      },
      global: {
        stubs: {
          LoadMore: true,
          LogViewer: true,
          StatusBadge: true,
        },
      },
    })

    expect(wrapper.find('.text-green-500').exists()).toBe(true)
  })
})
