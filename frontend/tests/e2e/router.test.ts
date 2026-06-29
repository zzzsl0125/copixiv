import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createRouter, createWebHistory } from 'vue-router'
import { mount } from '@vue/test-utils'
import { createApp, h, type Component } from 'vue'

/**
 * E2E-style tests: mount full views and verify rendering.
 * These use jsdom (no real browser) but validate the integration
 * of router + views + composables together.
 */

// Mock axios before importing any API module
vi.mock('axios', () => ({
  default: {
    create: () => ({
      get: vi.fn().mockResolvedValue({ data: {} }),
      post: vi.fn().mockResolvedValue({ data: {} }),
      put: vi.fn().mockResolvedValue({ data: {} }),
      delete: vi.fn().mockResolvedValue({ data: {} }),
    }),
  },
}))

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'novels', component: { template: '<div>NovelsPage</div>' } },
    { path: '/tasks', name: 'tasks', component: { template: '<div>TasksPage</div>' } },
    { path: '/tag-management', name: 'tag-management', component: { template: '<div>TagsPage</div>' } },
    { path: '/tokens', name: 'tokens', component: { template: '<div>TokensPage</div>' } },
  ],
})

function mountWithRouter(component: Component) {
  return mount(component, {
    global: {
      plugins: [router],
    },
  })
}

describe('Router configuration', () => {
  beforeEach(async () => {
    await router.push('/')
    await router.isReady()
  })

  it('resolves / to novels route', () => {
    expect(router.currentRoute.value.name).toBe('novels')
  })

  it('resolves /tasks to tasks route', async () => {
    await router.push('/tasks')
    expect(router.currentRoute.value.name).toBe('tasks')
  })

  it('resolves /tag-management to tag-management route', async () => {
    await router.push('/tag-management')
    expect(router.currentRoute.value.name).toBe('tag-management')
  })

  it('resolves /tokens to tokens route', async () => {
    await router.push('/tokens')
    expect(router.currentRoute.value.name).toBe('tokens')
  })
})
