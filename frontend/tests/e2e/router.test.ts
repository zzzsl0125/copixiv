import { describe, it, expect } from 'vitest'
import { createRouter, createMemoryHistory, type RouteRecordRaw } from 'vue-router'
import router, { routes } from '../../src/router'

/**
 * E2E-style tests: pin the REAL route table from ``src/router``.
 *
 * A previous version of this file declared its own inline route table and
 * tested that — a regression in ``src/router/index.ts`` would have passed
 * all green.  Now every assertion runs against the exported routes /
 * router, so a renamed path, a removed route, or a stub replacing a real
 * view component fails here.
 */

const EXPECTED: Array<{ path: string; name: string; view: string }> = [
  { path: '/', name: 'novels', view: 'Novels.vue' },
  { path: '/tasks', name: 'tasks', view: 'Tasks.vue' },
  { path: '/tag-management', name: 'tag-management', view: 'TagManagement.vue' },
  { path: '/tokens', name: 'tokens', view: 'Tokens.vue' },
  { path: '/failed-novels', name: 'failed-novels', view: 'FailedNovels.vue' },
]

describe('Router configuration (real route table)', () => {
  it('registers exactly the five app routes', () => {
    const actual = router
      .getRoutes()
      .filter((r) => r.path !== '/:pathMatch(.*)*')
      .map((r) => ({ path: r.path, name: r.name }))

    expect(actual).toEqual(EXPECTED.map(({ path, name }) => ({ path, name })))
  })

  it('wires every route to a real lazy view component', () => {
    const byPath = new Map(routes.map((r) => [r.path, r]))

    for (const { path, view } of EXPECTED) {
      const record = byPath.get(path) as RouteRecordRaw
      expect(record.component, `route ${path} has no component`).toBeTruthy()
      // The component loader must reference the real view file — a stub
      // like { template: '<div/>' } is a plain object, not a loader.
      // Vite rewrites the lazy import to /src/views/<File>; both dev and
      // test transforms keep the "/views/<File>" part stable.
      expect(record.component).toBeTypeOf('function')
      expect(String(record.component)).toContain(`/views/${view}`)
    }
  })

  it('resolves each path to the expected route name', () => {
    for (const { path, name } of EXPECTED) {
      expect(router.resolve(path).name).toBe(name)
    }
  })

  it('navigates between routes using the real routes', async () => {
    const testRouter = createRouter({
      history: createMemoryHistory(),
      routes: routes as RouteRecordRaw[],
    })
    await testRouter.push('/')
    await testRouter.isReady()
    expect(testRouter.currentRoute.value.name).toBe('novels')

    for (const { path, name } of EXPECTED.slice(1)) {
      await testRouter.push(path)
      expect(testRouter.currentRoute.value.name).toBe(name)
    }
  })
})
