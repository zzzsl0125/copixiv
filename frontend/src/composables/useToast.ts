import { toast } from 'vue-sonner'

type ToastType = 'success' | 'error' | 'info' | 'warning'

/** 各类型的默认停留时长（毫秒），沿用旧手写 toast 的语义。 */
const DEFAULT_DURATIONS: Record<ToastType, number> = {
  success: 4000,
  error: 6000,
  info: 4000,
  warning: 5000,
}

/**
 * 全局提示的薄封装：状态与渲染都交给 `vue-sonner`（`<Toaster>` 在 App.vue 挂载
 * 一次），这里只保留既有的调用点 API 与各类型时长。
 */
export function useToast() {
  const show = (message: string, type: ToastType = 'info', duration = 4000) => {
    toast[type](message, { duration })
  }

  const success = (message: string) => show(message, 'success', DEFAULT_DURATIONS.success)
  const error = (message: string) => show(message, 'error', DEFAULT_DURATIONS.error)
  const info = (message: string) => show(message, 'info', DEFAULT_DURATIONS.info)
  const warning = (message: string) => show(message, 'warning', DEFAULT_DURATIONS.warning)

  return { show, success, error, info, warning }
}
