import { reactive, readonly } from 'vue'

export interface ConfirmOptions {
  title?: string
  description: string
  confirmText?: string
  cancelText?: string
  variant?: 'danger' | 'default'
}

interface ConfirmState {
  open: boolean
  title: string
  description: string
  confirmText: string
  cancelText: string
  variant: 'danger' | 'default'
}

const state = reactive<ConfirmState>({
  open: false,
  title: '确认操作',
  description: '',
  confirmText: '确认',
  cancelText: '取消',
  variant: 'danger',
})

let resolver: ((confirmed: boolean) => void) | null = null

function settle(result: boolean) {
  state.open = false
  if (resolver) {
    const resolve = resolver
    resolver = null
    resolve(result)
  }
}

function confirm(options: ConfirmOptions | string): Promise<boolean> {
  if (resolver) {
    settle(false)
  }
  const normalized: ConfirmOptions =
    typeof options === 'string' ? { description: options } : options

  state.title = normalized.title ?? '确认操作'
  state.description = normalized.description
  state.confirmText = normalized.confirmText ?? '确认'
  state.cancelText = normalized.cancelText ?? '取消'
  state.variant = normalized.variant ?? 'danger'
  state.open = true

  return new Promise<boolean>((resolve) => {
    resolver = resolve
  })
}

export function useConfirm() {
  return {
    state: readonly(state),
    confirm,
    resolveConfirm: (accepted: boolean) => settle(accepted),
  }
}
