import { defineComponent, h } from 'vue'
import { mount, type VueWrapper } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { useProvideNovelShellState } from '../composables/useNovelShellState'

export interface MountedComposable<T> {
  result: T
  queryClient: QueryClient
  router: Router
  wrapper: VueWrapper
}

export async function flushPromises(rounds = 3): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await Promise.resolve()
  }
}

export async function mountWithQuery<T>(
  setup: () => T,
  options?: {
    initialRoute?: string
    provideShellState?: boolean
  },
): Promise<MountedComposable<T>> {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
      },
    },
  })

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'novels', component: { template: '<div />' } },
      { path: '/tasks', name: 'tasks', component: { template: '<div />' } },
    ],
  })

  if (options?.initialRoute) {
    await router.push(options.initialRoute)
  } else {
    await router.push('/')
  }
  await router.isReady()

  let result!: T
  const Harness = defineComponent({
    setup() {
      if (options?.provideShellState ?? true) {
        useProvideNovelShellState()
      }
      result = setup()
      return () => h('div')
    },
  })

  const wrapper = mount(Harness, {
    global: {
      plugins: [[VueQueryPlugin, { queryClient }], router],
    },
  })

  return { result, queryClient, router, wrapper }
}
