import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import router from './router'

window.history.replaceState = function () {
  console.log('🚨 history.replaceState 被调用，已拦截')
  return
}

const app = createApp(App)
app.use(router)
app.mount('#app')
