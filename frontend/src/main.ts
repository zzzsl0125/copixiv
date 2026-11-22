import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createApp } from 'vue'
import './style.css'
import 'vue-sonner/style.css'
import App from './App.vue'
import { ApiError } from './api/errors'
import router from './router'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
          return false
        }
        return failureCount < 1
      },
      refetchOnWindowFocus: false,
    },
  },
})

const app = createApp(App)
app.use(VueQueryPlugin, { queryClient })
app.use(router)
app.mount('#app')
