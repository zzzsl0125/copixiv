import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import type { ScheduledTask, TaskMethod } from '../../types'

const mocks = vi.hoisted(() => ({
  getTaskMethods: vi.fn<() => Promise<TaskMethod[]>>(),
  toastError: vi.fn(),
}))

vi.mock('../../api', () => ({
  taskApi: {
    getTaskMethods: mocks.getTaskMethods,
  },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

vi.mock('../../composables', () => ({
  useToast: () => ({
    error: mocks.toastError,
    success: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
}))

import TaskEditModal from '../features/TaskEditModal.vue'

describe('TaskEditModal method grouping and source badges', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('groups methods into 通用任务 and Pixiv 专用 with scope badges and removed-task fallback', async () => {
    mocks.getTaskMethods.mockResolvedValue([
      {
        name: 'source_fetch',
        description: '从指定或自动识别的数据源抓取书本/系列/章节',
        source: null,
        arguments: [
          {
            name: 'refs',
            type: 'array',
            required: false,
            nullable: false,
            default: [],
            description: null,
          },
        ],
      },
      {
        name: 'failed_retry',
        description: '重试「下载失败」清单中的小说（留空可定时自动重试）',
        source: null,
        arguments: [],
      },
      {
        name: 'novel_follow',
        description: '抓取指定pixiv账号中关注的作者的新作',
        source: 'pixiv',
        arguments: [],
      },
    ])

    const legacyTask: ScheduledTask = {
      id: 8,
      name: '爬取小说',
      task: 'novel_fetch',
      cron: '0 0 * * *',
      params: { id: null, redownload: true },
      is_enabled: false,
      sort_index: 0,
    }

    const wrapper = mount(TaskEditModal, {
      props: {
        isOpen: true,
        task: legacyTask,
        loading: false,
      },
      global: {
        stubs: { teleport: true },
      },
    })

    await flushPromises()
    await nextTick()

    const optgroups = wrapper.findAll('optgroup')
    expect(optgroups.map((g) => g.attributes('label'))).toEqual(['通用任务', 'Pixiv 专用'])
    expect(optgroups[0].findAll('option').map((o) => o.text())).toEqual(['source_fetch', 'failed_retry'])
    expect(optgroups[1].findAll('option').map((o) => o.text())).toEqual(['novel_follow'])

    // Legacy removed task keeps a disabled fallback option + 已移除 badge
    const fallbackOption = wrapper.find('option[value="novel_fetch"]')
    expect(fallbackOption.exists()).toBe(true)
    expect(fallbackOption.text()).toContain('novel_fetch（已移除）')
    expect((fallbackOption.element as HTMLOptionElement).disabled).toBe(true)
    expect(wrapper.text()).toContain('已移除')
    expect(wrapper.text()).toContain('该方法已从任务目录移除')

    // Switch to a common method -> 通用 badge
    await wrapper.get('select').setValue('source_fetch')
    await nextTick()
    expect(wrapper.text()).toContain('通用')
    expect(wrapper.text()).toContain('从指定或自动识别的数据源抓取书本/系列/章节')

    // Switch to a Pixiv-specific method -> Pixiv 专用 badge
    await wrapper.get('select').setValue('novel_follow')
    await nextTick()
    expect(wrapper.text()).toContain('Pixiv 专用')
    expect(wrapper.text()).toContain('抓取指定pixiv账号中关注的作者的新作')

    wrapper.unmount()
  })
})
