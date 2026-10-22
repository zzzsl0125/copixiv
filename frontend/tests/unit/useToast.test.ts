import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * useToast 现在只是 vue-sonner 的薄封装：调用点 API 与各类型默认时长必须
 * 与旧手写实现逐项一致（success 4s / error 6s / info 4s / warning 5s）。
 */
const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
}))

vi.mock('vue-sonner', () => ({ toast: toastMock }))

import { useToast } from '../../src/composables/useToast'

const { show, success, error, info, warning } = useToast()

describe('useToast', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('success → toast.success(msg, { duration: 4000 })', () => {
    success('已保存')
    expect(toastMock.success).toHaveBeenCalledTimes(1)
    expect(toastMock.success).toHaveBeenCalledWith('已保存', { duration: 4000 })
  })

  it('error → toast.error(msg, { duration: 6000 })', () => {
    error('保存失败')
    expect(toastMock.error).toHaveBeenCalledTimes(1)
    expect(toastMock.error).toHaveBeenCalledWith('保存失败', { duration: 6000 })
  })

  it('warning → toast.warning(msg, { duration: 5000 })', () => {
    warning('部分失败')
    expect(toastMock.warning).toHaveBeenCalledTimes(1)
    expect(toastMock.warning).toHaveBeenCalledWith('部分失败', { duration: 5000 })
  })

  it('info 与 show 默认走 toast.info(msg, { duration: 4000 })', () => {
    info('提示')
    show('默认提示')
    expect(toastMock.info).toHaveBeenNthCalledWith(1, '提示', { duration: 4000 })
    expect(toastMock.info).toHaveBeenNthCalledWith(2, '默认提示', { duration: 4000 })
    expect(toastMock.info).toHaveBeenCalledTimes(2)
  })

  it('show(msg, type, duration) 原样转发显式时长与类型', () => {
    show('完成', 'success', 1000)
    expect(toastMock.success).toHaveBeenCalledWith('完成', { duration: 1000 })
    expect(toastMock.info).not.toHaveBeenCalled()
  })
})
