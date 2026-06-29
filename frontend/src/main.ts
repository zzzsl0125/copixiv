import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import router from './router'

// 防止页面隐藏时 history.replaceState 导致浏览器从最小化状态恢复
const _originalReplaceState = window.history.replaceState.bind(window.history)
window.history.replaceState = function (...args: Parameters<typeof window.history.replaceState>) {
  if (document.visibilityState === 'hidden') return
  return _originalReplaceState(...args)
}

const app = createApp(App)
app.use(router)
app.mount('#app')
