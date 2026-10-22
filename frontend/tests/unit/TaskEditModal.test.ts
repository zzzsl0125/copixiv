import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import TaskEditModal from '../../src/components/features/TaskEditModal.vue'
import type { ScheduledTask } from '../../src/types'

const taskApiMock = vi.hoisted(() => ({
  getTaskMethods: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  taskApi: taskApiMock,
  getApiErrorMessage: (err: unknown, fallback: string) =>
    err instanceof Error ? err.message : fallback,
}))

// The shapes Pydantic's model_json_schema() produces for the built-in tasks.
const METHODS = [
  {
    name: 'novel_fetch',
    description: '抓取小说',
    arguments: {
      type: 'object',
      title: 'NovelFetchArgs',
      properties: {
        id: { type: 'integer', title: 'Id' },
        redownload: { type: 'boolean', title: 'Redownload', default: true },
      },
      required: ['id'],
    },
  },
  {
    name: 'batch_export',
    description: '导出选中小说',
    arguments: {
      type: 'object',
      title: 'BatchExportArgs',
      properties: {
        novel_ids: { type: 'array', items: { type: 'integer' }, title: 'Novel Ids' },
        format_mode: { type: 'string', default: 'txt', title: 'Format Mode' },
        zip_name: {
          anyOf: [{ type: 'string' }, { type: 'null' }],
          default: null,
          title: 'Zip Name',
        },
        naming_template: {
          anyOf: [{ type: 'string' }, { type: 'null' }],
          default: null,
          title: 'Naming Template',
        },
      },
      required: ['novel_ids'],
    },
  },
  {
    name: 'failed_retry',
    description: '重试失败下载',
    arguments: {
      type: 'object',
      title: 'FailedRetryArgs',
      properties: {
        novel_ids: { type: 'array', items: { type: 'integer' }, title: 'Novel Ids' },
      },
      required: ['novel_ids'],
    },
  },
  {
    name: 'rebuild_fts',
    description: '重建全文索引',
    arguments: null,
  },
]

async function mountOpen(task: ScheduledTask | null) {
  const wrapper = mount(TaskEditModal, { props: { isOpen: false, task } })
  await flushPromises()
  await wrapper.setProps({ isOpen: true })
  await flushPromises()
  return wrapper
}

describe('TaskEditModal (Pydantic JSON Schema editor)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    taskApiMock.getTaskMethods.mockResolvedValue(METHODS)
  })

  it('renders controls from schema types and hides the block for no-arg tasks', async () => {
    const wrapper = await mountOpen(null)
    await wrapper.find('select').setValue('novel_fetch')
    await flushPromises()

    expect(wrapper.text()).toContain('(integer)')
    expect(wrapper.text()).toContain('(boolean)')
    expect(wrapper.find('input[type="number"]').exists()).toBe(true)
    // Redownload checkbox + the "启用此任务" checkbox（AppCheckbox 现在是 reka 的
    // ARIA checkbox，渲染 <button role="checkbox">，不再有原生 input[type=checkbox]）。
    expect(wrapper.findAll('[role="checkbox"]')).toHaveLength(2)

    // Array without a schema default starts as editable JSON text.
    await wrapper.find('select').setValue('failed_retry')
    await flushPromises()
    expect(
      (wrapper.findAll('input[type="text"]')[1].element as HTMLInputElement).value,
    ).toBe('[]')

    await wrapper.find('select').setValue('rebuild_fts')
    await flushPromises()
    expect(wrapper.text()).not.toContain('参数设置')
  })

  it('normalizes arrays to JSON text and saves coerced params', async () => {
    const task: ScheduledTask = {
      id: 1,
      name: '导出',
      task: 'batch_export',
      cron: '0 0 * * *',
      params: {
        novel_ids: [1, 2],
        format_mode: 'txt',
        zip_name: null,
        naming_template: null,
      },
      is_enabled: true,
      sort_index: 0,
    }
    const wrapper = await mountOpen(task)

    const textInputs = wrapper.findAll('input[type="text"]')
    expect(textInputs.map(i => (i.element as HTMLInputElement).value)).toEqual([
      '导出', '[1,2]', 'txt', '', '',
    ])

    await textInputs[1].setValue('[1, 2, 3]')
    await textInputs[2].setValue('epub')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    const payload = wrapper.emitted('save')![0][0] as {
      params: Record<string, unknown>
    }
    expect(payload.params).toEqual({
      novel_ids: [1, 2, 3],
      format_mode: 'epub',
      zip_name: null,
      naming_template: null,
    })
  })

  it('keeps an invalid array text from being saved silently', async () => {
    const task: ScheduledTask = {
      id: 2,
      name: '导出',
      task: 'batch_export',
      cron: '0 0 * * *',
      params: { novel_ids: [1] },
      is_enabled: true,
      sort_index: 0,
    }
    const wrapper = await mountOpen(task)

    await wrapper.findAll('input[type="text"]')[1].setValue('[not json')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.emitted('save')).toBeUndefined()
  })
})
