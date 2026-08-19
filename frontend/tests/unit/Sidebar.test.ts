import { describe, it, expect } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import Sidebar from '../../src/components/Sidebar.vue'
import type { NovelFilters } from '../../src/types'

/**
 * Sidebar「其他」区回归测试：
 * - 失败记录 / 批量操作 是常驻入口，不得随 showFilters（仅小说页为 true）消失；
 * - 批量操作在非小说页点击时跳回 '/' 并只进不退（不清空已选）；
 * - 排序/筛选区仍受 showFilters 门控，与「其他」区解耦。
 *
 * 路由表本身由 tests/e2e/router.test.ts 钉住；这里只需路径存在，
 * 用 stub 组件避免单测里懒加载真实视图（Novels.vue → NovelCard 等）。
 */

const STUB = { template: '<div />' }

const stubRoutes = [
  { path: '/', component: STUB },
  { path: '/tasks', component: STUB },
  { path: '/tag-management', component: STUB },
  { path: '/tokens', component: STUB },
  { path: '/failed-novels', component: STUB },
]

const FILTERS: NovelFilters = {
  keyword: '',
  order_by: 'random',
  order_direction: 'DESC',
  min_like: undefined,
  min_text: undefined,
}

async function mountSidebar(options: {
  path?: string
  showFilters?: boolean
  isBatchMode?: boolean
} = {}) {
  const { path = '/', showFilters, isBatchMode = false } = options
  const router = createRouter({
    history: createMemoryHistory(),
    routes: stubRoutes,
  })
  await router.push(path) // 等初始导航落定，避免点击时 route.path 仍是 '/'
  return {
    router,
    wrapper: mount(Sidebar, {
      props: {
        isOpen: true,
        showFilters,
        activeSection: null,
        configLoadedAndApplied: false,
        filters: FILTERS,
        isBatchMode,
        randomDisabled: false,
      },
      global: { plugins: [router] },
    }),
  }
}

describe('Sidebar「其他」区常驻', () => {
  it('showFilters=false（非小说页）时「其他」区仍渲染', async () => {
    const { wrapper } = await mountSidebar({ path: '/tasks', showFilters: false })

    expect(wrapper.text()).toContain('失败记录')
    expect(wrapper.text()).toContain('批量操作')
    // 排序/筛选区仍受门控：非小说页不出现
    expect(wrapper.text()).not.toContain('排序')
  })

  it('showFilters=true（小说页）时「其他」区与排序区都渲染', async () => {
    const { wrapper } = await mountSidebar({ path: '/', showFilters: true })

    expect(wrapper.text()).toContain('失败记录')
    expect(wrapper.text()).toContain('批量操作')
  })

  it('点击「失败记录」跳转到 /failed-novels', async () => {
    const { wrapper, router } = await mountSidebar({ path: '/tasks', showFilters: false })

    await wrapper.find('a[href="/failed-novels"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/failed-novels')
  })
})

describe('Sidebar 批量操作', () => {
  it('小说页点击：维持原有开关切换语义', async () => {
    const { wrapper } = await mountSidebar({ path: '/', showFilters: true })

    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('toggle-batch-mode')).toHaveLength(1)
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('非小说页点击（批量模式未开）：跳回 / 并进入批量模式', async () => {
    const { wrapper, router } = await mountSidebar({ path: '/tasks', showFilters: false })

    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('toggle-batch-mode')).toHaveLength(1)
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('非小说页点击（批量模式已开）：只跳回 /，不退出批量模式（不清空已选）', async () => {
    const { wrapper, router } = await mountSidebar({
      path: '/tasks',
      showFilters: false,
      isBatchMode: true,
    })

    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('toggle-batch-mode')).toBeUndefined()
    expect(router.currentRoute.value.path).toBe('/')
  })
})
