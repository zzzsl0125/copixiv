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

describe('TaskHistoryList row identity across polls', () => {
  function makeItem(id: number, summary: string) {
    return {
      id,
      name: 'batch_export',
      arguments: { novel_ids: [1], format_mode: 'txt' },
      status: 'running',
      start_time: '2026-01-01T00:00:00',
      end_time: null,
      duration: null,
      result: { summary },
    }
  }

  it('reuses the same DOM elements when history is refreshed in place', async () => {
    const wrapper = mount(TaskHistoryList, {
      props: {
        history: [makeItem(10, '打包中 1/10 篇'), makeItem(9, '其他')],
        loading: false,
        hasMore: false,
      },
      global: { stubs: { LoadMore: true, LogViewer: true } },
    })

    const before = wrapper.findAll('li').map((li) => li.element)

    // Simulate a silent poll: brand-new objects, same ids.
    await wrapper.setProps({
      history: [makeItem(10, '打包中 5/10 篇'), makeItem(9, '其他')],
    })

    const after = wrapper.findAll('li').map((li) => li.element)
    expect(after.length).toBe(before.length)
    after.forEach((el, i) => expect(el).toBe(before[i]))
  })
})
