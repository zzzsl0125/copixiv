import { ref } from 'vue'

export interface Toast {
  id: number
  message: string
  type: 'success' | 'error' | 'info' | 'warning'
}

const toasts = ref<Toast[]>([])
let nextId = 0

export function useToast() {
  const show = (message: string, type: Toast['type'] = 'info', duration = 4000) => {
    const id = nextId++
    toasts.value.push({ id, message, type })
    setTimeout(() => {
      toasts.value = toasts.value.filter(t => t.id !== id)
    }, duration)
  }

  const success = (message: string) => show(message, 'success')
  const error = (message: string) => show(message, 'error', 6000)
  const info = (message: string) => show(message, 'info')
  const warning = (message: string) => show(message, 'warning', 5000)

  return { toasts, show, success, error, info, warning }
}
