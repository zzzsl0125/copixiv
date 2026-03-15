import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'novels',
    component: () => import('../views/Novels.vue')
  },
  {
    path: '/tasks',
    name: 'tasks',
    component: () => import('../views/Tasks.vue')
  },
  {
    path: '/tag-management',
    name: 'tag-management',
    component: () => import('../views/TagManagement.vue')
  },
  {
    path: '/tokens',
    name: 'tokens',
    component: () => import('../views/Tokens.vue')
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
