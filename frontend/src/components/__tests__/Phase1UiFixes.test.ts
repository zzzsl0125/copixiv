import { mount } from '@vue/test-utils'
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
  novelApi: {
    toggleFavourite: vi.fn(),
    toggleSpecialFollow: vi.fn(),
    downloadUrl: vi.fn((id: number) => `/api/novels/${id}/download`),
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

import EmptyState from '../ui/EmptyState.vue'
import BaseModal from '../ui/BaseModal.vue'
import TaskEditModal from '../features/TaskEditModal.vue'
import NovelCard from '../features/NovelCard.vue'

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

describe('Phase 1 UI fixes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('EmptyState supports custom title and description', () => {
    const def = mount(EmptyState)
    expect(def.text()).toContain('未找到相关小说')

    const custom = mount(EmptyState, {
      props: {
        title: '暂无失败记录',
        description: '当前没有下载失败的小说记录',
      },
    })
    expect(custom.text()).toContain('暂无失败记录')
    expect(custom.text()).toContain('当前没有下载失败的小说记录')
    expect(custom.text()).not.toContain('未找到相关小说')
  })

  it('BaseModal shows clean loading text and disables cancel button while loading', () => {
    const wrapper = mount(BaseModal, {
      props: {
        isOpen: true,
        title: '编辑',
        confirmText: '确认保存',
        loading: true,
      },
    })
    const submitBtn = wrapper.get('button[type="submit"]')
    const cancelBtn = wrapper.get('button[type="button"]')
    expect(submitBtn.text()).toBe('确认保存中…')
    expect((submitBtn.element as HTMLButtonElement).disabled).toBe(true)
    expect((cancelBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('TaskEditModal keeps step cron in custom mode without NaN and preserves params across async method load', async () => {
    let resolveMethods!: (methods: TaskMethod[]) => void
    mocks.getTaskMethods.mockReturnValue(
      new Promise<TaskMethod[]>((resolve) => {
        resolveMethods = resolve
      }),
    )

    const task: ScheduledTask = {
      id: 1,
      name: '抓单篇',
      task: 'novel_fetch',
      cron: '*/5 * * * *',
      params: { id: 987654 },
      is_enabled: true,
      sort_index: 0,
    }

    const wrapper = mount(TaskEditModal, {
      props: {
        isOpen: false,
        task,
        loading: false,
      },
    })

    await nextTick()
    expect(mocks.getTaskMethods).not.toHaveBeenCalled()

    await wrapper.setProps({ isOpen: true })
    await nextTick()
    expect(mocks.getTaskMethods).toHaveBeenCalledTimes(1)
    // */5 * * * * must fall into custom mode and keep the raw cron text without NaN
    const inputs = wrapper.findAll('input')
    expect(inputs.some((i) => (i.element as HTMLInputElement).value === '*/5 * * * *')).toBe(true)
    expect(wrapper.html()).not.toContain('NaN')

    // Now resolve getTaskMethods asynchronously -> saved param id=987654 must NOT be wiped
    resolveMethods([
      {
        name: 'novel_fetch',
        description: '抓取单本小说',
        arguments: [
          {
            name: 'id',
            type: 'integer',
            required: true,
            nullable: false,
            default: null,
            description: '小说 ID',
          },
        ],
      },
    ])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('小说 ID')

    await wrapper.get('form').trigger('submit')
    const emitted = wrapper.emitted('save')
    expect(emitted).toBeTruthy()
    expect(emitted![0][0]).toMatchObject({
      task: 'novel_fetch',
      cron: '*/5 * * * *',
      params: { id: 987654 },
    })
  })

  it('NovelCard uses shared tagPreferenceMap and avoids fake 2016-01-01 fallback date', () => {
    const prefMap = new Map<string, 'favourite' | 'blocked'>([
      ['favTag', 'favourite'],
      ['badTag', 'blocked'],
    ])
    const wrapper = mount(NovelCard, {
      props: {
        novel: {
          id: -1,
          title: 'Local Novel',
          author_id: null,
          author_name: '未知作者',
          series_id: null,
          series_name: null,
          series_index: null,
          like: 10,
          view: 20,
          text: 300,
          has_epub: 0,
          create_time: null,
          source: 'local',
          can_sync: false,
          external_url: null,
          is_favourite: false,
          is_special_follow: false,
          is_series: false,
          chapter_count: 1,
          tags: ['favTag', 'badTag'],
          editable_fields: ['title', 'author_id', 'series_id', 'series_index', 'tags', 'create_time'],
        },
        isActive: false,
        tagPreferenceMap: prefMap,
      },
    })
    expect(wrapper.text()).not.toContain('2016-01-01')
    expect(wrapper.text()).toContain('—')
    const spans = wrapper.findAll('span')
    const favBadge = spans.find((s) => s.text() === 'favTag')
    const badBadge = spans.find((s) => s.text() === 'badTag')
    expect(favBadge?.classes()).toContain('bg-blue-100')
    expect(badBadge?.classes()).toContain('line-through')
  })
})
