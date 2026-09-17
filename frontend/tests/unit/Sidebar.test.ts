import { describe, it, expect } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import Sidebar from '../../src/components/Sidebar.vue'
import { SECTIONS } from '../../src/config'
import type { SectionId } from '../../src/config'
import type { NovelFilters } from '../../src/types'

/**
 * Sidebar「其他」区回归测试：
 * - 失败记录 / 批量操作 是常驻入口，不得随 showFilters（仅小说页为 true）消失；
 * - 批量操作在非小说页点击时跳回 '/' 并只进不退（不清空已选）；
 * - 排序/筛选区仍受 showFilters 门控，与「其他」区解耦。
 *
 * 「阅览」区（小说列表/我的收藏/特别关注/我的上传）：
 * - 每一项 = 一次特殊搜索，keyword 取自 config.ts 的 SECTIONS 表（不再写字面量）；
 * - 侧边栏**只有导航入口**：上传按钮不在这里（在列表顶部 UploadBar，
 *   见 tests/unit/Novels-upload-bar.test.ts），本文件钉住"行内没有上传钮"。
 *
 * 路由表本身由 tests/e2e/router.test.ts 钉住；这里只需路径存在，
 * 用 stub 组件避免单测里懒加载真实视图（Novels.vue → NovelCard 等）。
 *
 * ⚠️ 断言演进（契约 LIBRARY_DESIGN.md §5）：批量操作按钮此前用
 * `wrapper.find('button')` 取「DOM 里第一个 button」，新增「阅览」区按钮后
 * 首个 button 不再等于批量按钮 —— 改为按 data-testid 精确定位，
 * 断言语义不变（仍是「点批量按钮 → toggle-batch-mode」）。
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
  activeSection?: SectionId | null
} = {}) {
  const { path = '/', showFilters, isBatchMode = false, activeSection = null } = options
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
        activeSection,
        configLoadedAndApplied: false,
        filters: FILTERS,
        isBatchMode,
        randomDisabled: false,
      },
      global: { plugins: [router] },
    }),
  }
}

const batchButton = (wrapper: ReturnType<typeof mount>) =>
  wrapper.get('[data-testid="nav-batch-mode"]')

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

    await batchButton(wrapper).trigger('click')
    expect(wrapper.emitted('toggle-batch-mode')).toHaveLength(1)
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('非小说页点击（批量模式未开）：跳回 / 并进入批量模式', async () => {
    const { wrapper, router } = await mountSidebar({ path: '/tasks', showFilters: false })

    await batchButton(wrapper).trigger('click')
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

    await batchButton(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.emitted('toggle-batch-mode')).toBeUndefined()
    expect(router.currentRoute.value.path).toBe('/')
  })
})

describe('Sidebar「阅览」区（我的上传）', () => {
  it('阅览区分组共 4 项：小说列表 / 我的收藏 / 特别关注 / 我的上传', async () => {
    const { wrapper } = await mountSidebar({ path: '/', showFilters: true })

    const items = [
      wrapper.get('[data-testid="nav-novels"]'),
      wrapper.get('[data-testid="nav-favourites"]'),
      wrapper.get('[data-testid="nav-special-follow"]'),
      wrapper.get('[data-testid="nav-uploads"]'),
    ]
    expect(items.map((i) => i.text().trim())).toEqual([
      '小说列表', '我的收藏', '特别关注', '我的上传',
    ])
  })

  it('点击「我的上传」emit search，keyword/section 与 SECTIONS.uploads 一致', async () => {
    const { wrapper } = await mountSidebar({ path: '/', showFilters: true })

    await wrapper.get('[data-testid="nav-uploads"]').trigger('click')

    expect(wrapper.emitted('search')).toEqual([
      [SECTIONS.uploads.keyword, 'uploads'],
    ])
    expect(SECTIONS.uploads.keyword).toBe('source:local;')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('「我的收藏」/「特别关注」的 keyword 也来自 SECTIONS 表', async () => {
    const { wrapper } = await mountSidebar({ path: '/', showFilters: true })

    await wrapper.get('[data-testid="nav-favourites"]').trigger('click')
    await wrapper.get('[data-testid="nav-special-follow"]').trigger('click')

    expect(wrapper.emitted('search')).toEqual([
      [SECTIONS.favourites.keyword, 'favourites'],
      [SECTIONS.special_follow.keyword, 'special_follow'],
    ])
  })

  it('非小说页点击「我的上传」：先跳回 / 再 emit search', async () => {
    const { wrapper, router } = await mountSidebar({ path: '/tasks', showFilters: false })

    await wrapper.get('[data-testid="nav-uploads"]').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
    expect(wrapper.emitted('search')).toEqual([[SECTIONS.uploads.keyword, 'uploads']])
  })

  it('activeSection=uploads 时该项高亮（active 样式挂在按钮上）', async () => {
    const { wrapper } = await mountSidebar({ path: '/', showFilters: true, activeSection: 'uploads' })

    expect(wrapper.get('[data-testid="nav-uploads"]').classes()).toContain('bg-gray-100')
    expect(wrapper.get('[data-testid="nav-favourites"]').classes()).not.toContain('bg-gray-100')
  })

  it('「小说列表」走 reset-to-defaults，不 emit search', async () => {
    const { wrapper } = await mountSidebar({ path: '/', showFilters: true })

    await wrapper.get('[data-testid="nav-novels"]').trigger('click')

    expect(wrapper.emitted('reset-to-defaults')).toHaveLength(1)
    expect(wrapper.emitted('search')).toBeUndefined()
  })
})

describe('Sidebar 上传入口', () => {
  it('侧边栏不再挂上传按钮：只有导航项，动作入口在列表顶部 UploadBar', async () => {
    const { wrapper } = await mountSidebar({ path: '/', showFilters: true })

    // 上传按钮曾经挂在「我的上传」行右侧；现在只留导航入口（避免行内按钮不美观，
    // 也让上传与批量模式的关系归列表页处理）。
    expect(wrapper.find('[data-testid="nav-upload-trigger"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="nav-uploads"]').exists()).toBe(true)
    expect(wrapper.emitted('upload')).toBeUndefined()
  })
})

describe('Sidebar 「我的上传」与上传动作解耦', () => {
  it('点击我的上传只 emit search，不 emit upload', async () => {
    const { wrapper } = await mountSidebar({ path: '/', showFilters: true })

    await wrapper.get('[data-testid="nav-uploads"]').trigger('click')

    expect(wrapper.emitted('search')).toEqual([[SECTIONS.uploads.keyword, 'uploads']])
    expect(wrapper.emitted('upload')).toBeUndefined()
  })
})
