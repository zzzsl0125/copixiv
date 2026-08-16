import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import Tasks from '../../src/views/Tasks.vue'

const taskApiMock = vi.hoisted(() => ({
  getTaskMethods: vi.fn(),
  getScheduledTasks: vi.fn(),
  createScheduledTask: vi.fn(),
  updateScheduledTask: vi.fn(),
  deleteScheduledTask: vi.fn(),
  runScheduledTask: vi.fn(),
  reorderScheduledTasks: vi.fn(),
  getTaskHistory: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  taskApi: taskApiMock,
}))

const SectionHeaderStub = {
  name: 'SectionHeader',
  emits: ['add', 'refresh'],
  template: '<div><button class="add-task-btn" @click="$emit(\'add\')">add</button></div>',
}

function mountTasks() {
  return mount(Tasks, {
    global: {
      stubs: {
        PageHeader: true,
        SectionHeader: SectionHeaderStub,
        ScheduledTaskList: true,
        TaskHistoryList: true,
      },
    },
  })
}

describe('Tasks (modal save-close regression)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    taskApiMock.getTaskMethods.mockResolvedValue([])
    taskApiMock.getScheduledTasks.mockResolvedValue([])
    taskApiMock.getTaskHistory.mockResolvedValue({ items: [], total: 0 })
    taskApiMock.createScheduledTask.mockResolvedValue({})
    taskApiMock.updateScheduledTask.mockResolvedValue({})
  })

  it('closes the edit modal after a successful save', async () => {
    const wrapper = mountTasks()
    await flushPromises()

    await wrapper.find('.add-task-btn').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(true)

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(taskApiMock.createScheduledTask).toHaveBeenCalledTimes(1)
    // 保存成功后模态框必须关闭（此前被 savingTask 守卫拦住，永远不关）。
    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(false)
  })

  it('keeps the edit modal open when the save fails', async () => {
    taskApiMock.createScheduledTask.mockRejectedValue(new Error('boom'))
    const wrapper = mountTasks()
    await flushPromises()

    await wrapper.find('.add-task-btn').trigger('click')
    await flushPromises()
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(taskApiMock.createScheduledTask).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(true)
  })
})
