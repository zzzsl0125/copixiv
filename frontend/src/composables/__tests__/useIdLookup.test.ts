import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import {
  formatIdLookupHint,
  ID_LOOKUP_DEBOUNCE_MS,
  parseIdInput,
  useIdLookup,
  type IdLookup,
} from '../useIdLookup'

interface LookupResult {
  name: string | null
  novelCount: number
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('parseIdInput', () => {
  it('parses whitespace as null, positive safe integers as numbers, and invalid inputs as undefined', () => {
    expect(parseIdInput('')).toBeNull()
    expect(parseIdInput('   \t\n ')).toBeNull()

    expect(parseIdInput('123')).toBe(123)
    expect(parseIdInput(' 42 ')).toBe(42)

    expect(parseIdInput('0')).toBeUndefined()
    expect(parseIdInput('-1')).toBeUndefined()
    expect(parseIdInput('1e3')).toBeUndefined()
    expect(parseIdInput('１２３')).toBeUndefined()
    expect(parseIdInput('abc')).toBeUndefined()
    expect(parseIdInput('99999999999999999')).toBeUndefined()
  })
})

describe('formatIdLookupHint', () => {
  const base = {
    entity: '作者',
    idNumber: null as number | null | undefined,
    currentId: null as number | null,
    currentName: '原名',
    loading: false,
    failed: false,
    found: null as LookupResult | null,
    emptyNote: '留空仅按名字展示',
    unlinkNote: '清空将解除关联',
  }

  it('covers all hint branches', () => {
    expect(formatIdLookupHint({ ...base, idNumber: undefined })).toEqual({
      tone: 'error',
      text: '只能填数字 ID（pixiv 作者 ID）',
    })

    expect(formatIdLookupHint({ ...base, currentId: 7, idNumber: null })).toEqual({
      tone: 'muted',
      text: '清空将解除关联',
    })

    expect(formatIdLookupHint({ ...base, currentId: null, idNumber: null })).toEqual({
      tone: 'muted',
      text: '留空仅按名字展示',
    })

    expect(formatIdLookupHint({ ...base, currentId: 7, idNumber: 7 })).toEqual({
      tone: 'ok',
      text: '当前作者：原名',
    })

    expect(formatIdLookupHint({ ...base, idNumber: 9, loading: true })).toEqual({
      tone: 'muted',
      text: '查询中…',
    })

    expect(formatIdLookupHint({ ...base, unlinking: true, currentId: null, idNumber: null })).toEqual({
      tone: 'muted',
      text: '清空将解除关联',
    })

    expect(formatIdLookupHint({ ...base, idNumber: 9, failed: true })).toEqual({
      tone: 'warn',
      text: '查询失败；仍可保存，保存时将再次尝试解析',
    })

    expect(formatIdLookupHint({ ...base, idNumber: 9, found: null })).toEqual({
      tone: 'muted',
      text: '将按此 ID 关联 pixiv 作者',
    })

    expect(
      formatIdLookupHint({ ...base, idNumber: 9, found: { name: '新作者', novelCount: 0 } }),
    ).toEqual({
      tone: 'ok',
      text: '✓ 新作者',
    })

    expect(
      formatIdLookupHint({ ...base, idNumber: 9, found: { name: '新作者', novelCount: 5 } }),
    ).toEqual({
      tone: 'ok',
      text: '✓ 新作者 · 库内已有 5 本',
    })

    expect(
      formatIdLookupHint({ ...base, idNumber: 9, found: { name: null, novelCount: 0 } }),
    ).toEqual({
      tone: 'warn',
      text: '未查到该作者（ID 不存在或 pixiv 暂不可达）；仍可保存，保存时将再次尝试解析',
    })
  })
})

describe('useIdLookup', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  function mountLookup(opts: {
    initial?: string
    currentId?: () => number | null
    enabled?: () => boolean
    fetch: (id: number, signal?: AbortSignal) => Promise<LookupResult>
  }) {
    const input = ref(opts.initial ?? '')
    let lookup!: IdLookup<LookupResult>
    const Comp = defineComponent({
      setup() {
        lookup = useIdLookup<LookupResult>({
          input,
          currentId: opts.currentId ?? (() => null),
          enabled: opts.enabled,
          fetch: opts.fetch,
        })
        return () => null
      },
    })
    const wrapper = mount(Comp)
    return { input, lookup, wrapper }
  }

  it('skips fetch when disabled or matching currentId', async () => {
    const fetchSpy = vi.fn(async (id: number) => ({ name: `User ${id}`, novelCount: 0 }))
    let isEnabled = false
    const { input, lookup, wrapper } = mountLookup({
      currentId: () => 10,
      enabled: () => isEnabled,
      fetch: fetchSpy,
    })

    input.value = '20'
    await nextTick()
    await vi.advanceTimersByTimeAsync(ID_LOOKUP_DEBOUNCE_MS)
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(lookup.loading.value).toBe(false)

    isEnabled = true
    input.value = '10'
    await nextTick()
    await vi.advanceTimersByTimeAsync(ID_LOOKUP_DEBOUNCE_MS)
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(lookup.loading.value).toBe(false)

    wrapper.unmount()
  })

  it('debounces rapid edits and discards stale responses out of order', async () => {
    const pending = new Map<number, ReturnType<typeof deferred<LookupResult>>>()
    const fetchSpy = vi.fn((id: number) => {
      const d = deferred<LookupResult>()
      pending.set(id, d)
      return d.promise
    })

    const { input, lookup, wrapper } = mountLookup({ fetch: fetchSpy })

    input.value = '1'
    await nextTick()
    await vi.advanceTimersByTimeAsync(100)
    input.value = '12'
    await nextTick()
    await vi.advanceTimersByTimeAsync(100)
    input.value = '123'
    await nextTick()
    expect(lookup.loading.value).toBe(true)
    expect(fetchSpy).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(ID_LOOKUP_DEBOUNCE_MS)
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    expect(fetchSpy).toHaveBeenCalledWith(123, expect.any(AbortSignal))

    // Change input before first in-flight request resolves
    input.value = '456'
    await nextTick()
    await vi.advanceTimersByTimeAsync(ID_LOOKUP_DEBOUNCE_MS)
    expect(fetchSpy).toHaveBeenCalledTimes(2)
    expect(fetchSpy).toHaveBeenLastCalledWith(456, expect.any(AbortSignal))

    // Newer request resolves first, then older stale request resolves
    pending.get(456)!.resolve({ name: 'Fresh Author', novelCount: 3 })
    await nextTick()
    await Promise.resolve()
    expect(lookup.result.value).toEqual({ name: 'Fresh Author', novelCount: 3 })
    expect(lookup.loading.value).toBe(false)

    pending.get(123)!.resolve({ name: 'Stale Author', novelCount: 1 })
    await nextTick()
    await Promise.resolve()
    expect(lookup.result.value).toEqual({ name: 'Fresh Author', novelCount: 3 })

    wrapper.unmount()
  })

  it('sets failed state on rejection and cancels pending debounce on unmount', async () => {
    const fetchSpy = vi.fn(async () => {
      throw new Error('network down')
    })

    const { input, lookup, wrapper } = mountLookup({ fetch: fetchSpy })

    input.value = '777'
    await nextTick()
    await vi.advanceTimersByTimeAsync(ID_LOOKUP_DEBOUNCE_MS)
    await nextTick()

    expect(lookup.failed.value).toBe(true)
    expect(lookup.result.value).toBeNull()
    expect(lookup.loading.value).toBe(false)

    fetchSpy.mockClear()
    input.value = '888'
    await nextTick()
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(ID_LOOKUP_DEBOUNCE_MS)
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('aborts superseded in-flight requests and aborts active request on unmount', async () => {
    const signals: AbortSignal[] = []
    const rejectors: Array<(err: unknown) => void> = []
    const fetchSpy = vi.fn((_id: number, signal?: AbortSignal) => {
      if (signal) signals.push(signal)
      return new Promise<LookupResult>((_res, rej) => {
        rejectors.push(rej)
      })
    })

    const { input, lookup, wrapper } = mountLookup({ fetch: fetchSpy })

    input.value = '101'
    await nextTick()
    await vi.advanceTimersByTimeAsync(ID_LOOKUP_DEBOUNCE_MS)
    expect(signals).toHaveLength(1)
    expect(signals[0]!.aborted).toBe(false)

    // Superseding input aborts the first in-flight request
    input.value = '202'
    await nextTick()
    expect(signals[0]!.aborted).toBe(true)

    // Rejecting the aborted request does not flip failed=true
    rejectors[0]!(new DOMException('Aborted', 'AbortError'))
    await nextTick()
    await Promise.resolve()
    expect(lookup.failed.value).toBe(false)

    await vi.advanceTimersByTimeAsync(ID_LOOKUP_DEBOUNCE_MS)
    expect(signals).toHaveLength(2)
    expect(signals[1]!.aborted).toBe(false)

    // Unmounting aborts the second in-flight request
    wrapper.unmount()
    expect(signals[1]!.aborted).toBe(true)
  })
})
