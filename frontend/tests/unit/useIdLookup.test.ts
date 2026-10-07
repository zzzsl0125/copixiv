import { describe, it, expect, vi } from 'vitest'
import { defineComponent, h, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import {
  ID_LOOKUP_DEBOUNCE_MS,
  formatIdLookupHint,
  parseIdInput,
  useIdLookup,
} from '../../src/composables/useIdLookup'

/**
 * 「填 pixiv ID → 预览解析出的名字」这条交互的共享实现
 * （docs/NOVEL_EDIT_DESIGN.md §5.5/§5.6）。
 *
 * 作者与系列两个字段共用它，所以这里钉住的行为**同时**是两者的契约：
 * 输入解析、防抖、过期响应丢弃、"与当前值相同就不查"、失败不拦保存。
 */

describe('parseIdInput', () => {
  it('空 = null（解除关联）；纯正整数 = 数字', () => {
    expect(parseIdInput('')).toBeNull()
    expect(parseIdInput('   ')).toBeNull()
    expect(parseIdInput('777')).toBe(777)
    expect(parseIdInput('  777  ')).toBe(777)      // 首尾空白无妨
  })

  it('0 / 负数 / 非数字 / 小数 / 全角 = undefined（非法，拦保存）', () => {
    for (const bad of ['0', '-1', 'abc', '12.3', '１２３', '1e3', '777x', '+7']) {
      expect(parseIdInput(bad), bad).toBeUndefined()
    }
  })
})

describe('formatIdLookupHint', () => {
  const base = {
    entity: '系列',
    idNumber: 888 as number | null | undefined,
    currentId: null as number | null,
    currentName: '未知',
    loading: false,
    failed: false,
    found: null as { name: string | null | undefined; novelCount: number } | null,
    emptyNote: '留空 = 不改系列',
    unlinkNote: '留空 = 解除系列关联，并清掉显示名「某系列」与章节号',
  }

  it('非法输入 → 红，说明只能填数字', () => {
    const hint = formatIdLookupHint({ ...base, idNumber: undefined })
    expect(hint.tone).toBe('error')
    expect(hint.text).toContain('只能填数字 ID')
  })

  it('留空且当前有值 → 原样用调用方给的"解除关联"那句话', () => {
    // 两个字段的收尾刻意不同（作者留名、系列连名字一起清），所以这句话不能
    // 由这里统一拼：共享的是行为，不是承诺。
    const hint = formatIdLookupHint({ ...base, idNumber: null, currentId: 606 })
    expect(hint.tone).toBe('muted')
    expect(hint.text).toBe(base.unlinkNote)
  })

  it('留空且当前没值 → 用调用方给的那句话（作者与系列语义不同）', () => {
    expect(formatIdLookupHint({ ...base, idNumber: null }).text).toBe('留空 = 不改系列')
  })

  it('与当前值相同 → 绿，直接用卡片手里的名字（此时不该有请求）', () => {
    const hint = formatIdLookupHint({ ...base, idNumber: 606, currentId: 606, currentName: '某系列' })
    expect(hint.tone).toBe('ok')
    expect(hint.text).toBe('当前系列：某系列')
  })

  it('查询中 → 灰', () => {
    expect(formatIdLookupHint({ ...base, loading: true }).text).toBe('查询中…')
  })

  it('请求失败 → 黄，且明说仍可保存', () => {
    const hint = formatIdLookupHint({ ...base, failed: true })
    expect(hint.tone).toBe('warn')
    expect(hint.text).toContain('仍可保存')
  })

  it('预览命中 → 绿 + 名字 + 库内本数', () => {
    const hint = formatIdLookupHint({
      ...base, found: { name: '新系列', novelCount: 4 },
    })
    expect(hint.tone).toBe('ok')
    expect(hint.text).toBe('✓ 新系列 · 库内已有 4 本')
  })

  it('库内 0 本时不显示那句多余的计数', () => {
    const hint = formatIdLookupHint({ ...base, found: { name: '新系列', novelCount: 0 } })
    expect(hint.text).toBe('✓ 新系列')
  })

  it('预览查不到名字 → 黄（**不是**错误：id 才是身份）', () => {
    const hint = formatIdLookupHint({ ...base, found: { name: null, novelCount: 0 } })
    expect(hint.tone).toBe('warn')
    expect(hint.text).toContain('未查到该系列')
    expect(hint.text).toContain('仍可保存')
  })
})

/** 把组合式函数挂进一个真实组件里跑（`onUnmounted` 需要 setup 上下文）。 */
function mountLookup(options: {
  currentId?: number | null
  enabled?: () => boolean
  fetch?: (id: number) => Promise<unknown>
}) {
  const input = ref('')
  const fetchSpy = vi.fn(options.fetch ?? (async (id: number) => ({ id })))
  let api: ReturnType<typeof useIdLookup> | undefined

  const Host = defineComponent({
    setup() {
      api = useIdLookup({
        input,
        currentId: () => options.currentId ?? null,
        fetch: fetchSpy as never,
        enabled: options.enabled,
      })
      return () => h('div')
    },
  })
  const wrapper = mount(Host)
  return { wrapper, input, fetchSpy, api: () => api! }
}

/** 等过防抖窗口（真实计时器；与 flushPromises 的 setTimeout 不冲突）。 */
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, ID_LOOKUP_DEBOUNCE_MS + 80))
  await nextTick()
}

describe('useIdLookup', () => {
  it('输入新 id → 防抖后查一次', async () => {
    const { input, fetchSpy, api } = mountLookup({ currentId: null })

    input.value = '888'
    await nextTick()
    expect(api().loading.value).toBe(true)
    expect(fetchSpy).not.toHaveBeenCalled()          // 还在防抖窗口里

    await settle()
    expect(fetchSpy).toHaveBeenCalledWith(888)
    expect(api().result.value).toEqual({ id: 888 })
    expect(api().loading.value).toBe(false)
  })

  it('与当前值相同 → 一个请求都不发', async () => {
    const { input, fetchSpy, api } = mountLookup({ currentId: 888 })

    input.value = '888'
    await settle()

    expect(fetchSpy).not.toHaveBeenCalled()
    expect(api().result.value).toBeNull()
  })

  it('连续输入只查最后一次，且慢回来的旧响应被丢弃', async () => {
    let releaseSlow: (value: unknown) => void = () => {}
    const slow = new Promise((resolve) => { releaseSlow = resolve })
    const { input, fetchSpy, api } = mountLookup({
      currentId: null,
      fetch: (id: number) => (id === 111 ? slow : Promise.resolve({ id })),
    })

    input.value = '111'
    await settle()                                   // 111 的请求发出去了（未回）
    expect(fetchSpy).toHaveBeenCalledWith(111)

    input.value = '222'
    await settle()
    expect(fetchSpy).toHaveBeenLastCalledWith(222)
    expect(api().result.value).toEqual({ id: 222 })

    releaseSlow({ id: 111 })                         // 迟到的旧响应
    await nextTick()
    await settle()
    expect(api().result.value).toEqual({ id: 222 })   // 没有被覆盖
  })

  it('请求失败 → failed=true 且清空结果（调用方据此提示但不禁用保存）', async () => {
    const { input, api } = mountLookup({
      currentId: null,
      fetch: () => Promise.reject(new Error('boom')),
    })

    input.value = '888'
    await settle()

    expect(api().failed.value).toBe(true)
    expect(api().result.value).toBeNull()
    expect(api().loading.value).toBe(false)
  })

  it('enabled=false（如 pixiv 书的系列字段）→ 一个请求都不发', async () => {
    const { input, fetchSpy, api } = mountLookup({ currentId: null, enabled: () => false })

    input.value = '888'
    await settle()

    expect(fetchSpy).not.toHaveBeenCalled()
    expect(api().loading.value).toBe(false)
  })
})
