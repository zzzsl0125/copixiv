import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskHistoryList from '../../src/components/features/TaskHistoryList.vue'

const STATUS_ICON_CLASS: Record<string, string> = {
  success: '.text-green-500',
  failed: '.text-red-500',
  running: '.text-blue-500',
  pending: '.text-yellow-500',
  interrupted: '.text-gray-400',
}

function mountWithStatus(status: string) {
  return mount(TaskHistoryList, {
    props: {
      history: [
        {
          id: 1,
          name: 'test',
          arguments: null,
          status,
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
}

describe('TaskHistoryList (lowercase status contract)', () => {
  it('shows the success icon for backend status "success"', () => {
    const wrapper = mountWithStatus('success')
    expect(wrapper.find('.text-green-500').exists()).toBe(true)
  })

  it.each(Object.entries(STATUS_ICON_CLASS))(
    'shows the %s icon for status "%s"',
    (status, selector) => {
      const wrapper = mountWithStatus(status)
      expect(wrapper.find(selector).exists()).toBe(true)

      // No other status icon may render alongside it.
      for (const [other, otherSelector] of Object.entries(STATUS_ICON_CLASS)) {
        if (other === status) continue
        expect(wrapper.find(otherSelector).exists()).toBe(false)
      }
    },
  )

  it('tolerates uppercase statuses from a misbehaving backend', () => {
    const wrapper = mountWithStatus('SUCCESS')
    expect(wrapper.find('.text-green-500').exists()).toBe(true)
  })
})
