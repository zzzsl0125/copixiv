import { createRouter, createWebHistory } from 'vue-router'

// Exported so tests can pin the real route table (path/name/component wiring)
// instead of re-declaring a parallel copy that can silently drift.
export const routes = [
  {
    path: '/',
    name: 'novels',
    component: () => import('../views/Novels.vue'),
  },
  {
    path: '/tasks',
    name: 'tasks',
    component: () => import('../views/Tasks.vue'),
  },
  {
    path: '/tag-management',
    name: 'tag-management',
    component: () => import('../views/TagManagement.vue'),
  },
  {
    path: '/tokens',
    name: 'tokens',
    component: () => import('../views/Tokens.vue'),
  },
  {
    path: '/failed-novels',
    name: 'failed-novels',
    component: () => import('../views/FailedNovels.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
