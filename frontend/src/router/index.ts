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
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
