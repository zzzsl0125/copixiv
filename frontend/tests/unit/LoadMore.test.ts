import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import LoadMore from '../../src/components/features/LoadMore.vue'

/**
 * LoadMore 用 IntersectionObserver 触发无限滚动（jsdom 没有它）。
 * 这里用一个可手动触发的桩，钉住三件事：
 *   1. 只在「相交 + 未加载中 + 还有更多 + 有数据」时 emit；
 *   2. 加载结束后 pause → 300ms → resume，重建 observer 让仍停在视口里的
 *      哨兵再触发一次（旧实现是 unobserve + observe）；
 *   3. 卸载时清掉待执行的 resume 定时器。
 */

type IOCallback = (
  entries: IntersectionObserverEntry[],
  observer: IntersectionObserver,
) => void

class MockIntersectionObserver {
  static instances: MockIntersectionObserver[] = []

  readonly callback: IOCallback
  readonly elements = new Set<Element>()
  disconnectCount = 0

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback as IOCallback
    MockIntersectionObserver.instances.push(this)
  }

  observe(el: Element) {
    this.elements.add(el)
  }

  unobserve(el: Element) {
    this.elements.delete(el)
  }

  disconnect() {
    this.elements.clear()
    this.disconnectCount += 1
  }

  takeRecords(): IntersectionObserverEntry[] {
    return []
  }

  /** 模拟一次相交回调（默认相交）。 */
  trigger(isIntersecting = true) {
    this.callback(
      [{ isIntersecting } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    )
  }
}

function mountLoadMore(overrides: Partial<{
  loading: boolean
  noMoreData: boolean
  hasData: boolean
}> = {}) {
  return mount(LoadMore, {
    props: { loading: false, noMoreData: false, hasData: true, ...overrides },
  })
}

describe('LoadMore (IntersectionObserver)', () => {
  beforeEach(() => {
    MockIntersectionObserver.instances = []
    vi.stubGlobal('IntersectionObserver', MockIntersectionObserver)
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('相交时 emit load-more；加载中 / 没有更多 / 没有数据时不 emit', async () => {
    const wrapper = mountLoadMore()
    await nextTick()
    expect(MockIntersectionObserver.instances).toHaveLength(1)

    MockIntersectionObserver.instances[0].trigger()
    expect(wrapper.emitted('load-more')).toHaveLength(1)

    const loading = mountLoadMore({ loading: true })
    await nextTick()
    MockIntersectionObserver.instances[1].trigger()
    expect(loading.emitted('load-more')).toBeUndefined()

    const noMore = mountLoadMore({ noMoreData: true })
    await nextTick()
    MockIntersectionObserver.instances[2].trigger()
    expect(noMore.emitted('load-more')).toBeUndefined()

    // hasData=false 时哨兵根本不渲染，observer 不会建。
    mountLoadMore({ hasData: false })
    await nextTick()
    expect(MockIntersectionObserver.instances).toHaveLength(3)
  })

  it('加载结束后 pause，300ms 后 resume 并重新观察（仍相交则再次 emit）', async () => {
    const wrapper = mountLoadMore()
    await nextTick()
    const first = MockIntersectionObserver.instances[0]
    expect(first.elements.size).toBe(1)

    await wrapper.setProps({ loading: true })
    // 加载中不重建 observer。
    expect(MockIntersectionObserver.instances).toHaveLength(1)

    await wrapper.setProps({ loading: false })
    // 加载结束 → 立即暂停并断开旧 observer。
    expect(first.disconnectCount).toBe(1)
    expect(first.elements.size).toBe(0)

    vi.advanceTimersByTime(299)
    await nextTick()
    expect(MockIntersectionObserver.instances).toHaveLength(1)

    vi.advanceTimersByTime(1)
    await nextTick()
    expect(MockIntersectionObserver.instances).toHaveLength(2)

    const second = MockIntersectionObserver.instances[1]
    expect(second.elements.size).toBe(1)
    second.trigger()
    expect(wrapper.emitted('load-more')).toHaveLength(1)
  })

  it('卸载时清掉待执行的 resume 定时器', async () => {
    const wrapper = mountLoadMore()
    await nextTick()
    await wrapper.setProps({ loading: true })
    await wrapper.setProps({ loading: false })
    expect(vi.getTimerCount()).toBe(1)

    wrapper.unmount()
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(1000)
    expect(MockIntersectionObserver.instances).toHaveLength(1)
  })

  it('环境没有 IntersectionObserver 时不抛错（VueUse 自动降级）', async () => {
    vi.unstubAllGlobals()
    expect(() => mountLoadMore()).not.toThrow()
    await nextTick()
    expect(MockIntersectionObserver.instances).toHaveLength(0)
  })

  /**
   * 上限状态下**不需要**任何节流：自动加载是自限的。
   *
   * 历史：这里曾经有一个 `autoLoad` prop（「到上限就停自动加载」），依据是一次
   * 误诊 —— 用 `window.scrollTo()` 循环模拟连续滚动，测到 25 秒 77 个请求，
   * 据此认定「哨兵会无限自触发」。真机复测推翻了它：
   *
   *   到 300 张上限后，把锚点补偿（`window.scrollBy`）临时改成 no-op，好让哨兵
   *   **真的**停在视口里（`secTop=564`，`atBottom=0`），然后完全静止 25 秒：
   *   只发了 **3 个**请求就自己停了（200ms 分辨率采样，之后 24 秒恒为 0）。
   *
   * 原因是每次加载都把哨兵推出视口（实测 `secTop` 564 → 714 → 949），而
   * IntersectionObserver 只在「不可见 → 可见」的**变化**时回调，不会因为
   * 「一直可见」而持续回调。所以这条用例钉住的是「加载完只重观察一次」。
   */
  it('加载结束后只重观察一次：哨兵回到可见才再发下一次（不会凭"一直在视口里"重复发）', async () => {
    const wrapper = mountLoadMore()
    await nextTick()

    // 哨兵可见 → 发一次
    MockIntersectionObserver.instances[0].trigger()
    expect(wrapper.emitted('load-more')).toHaveLength(1)

    // 加载结束 → pause + 300ms 后 resume（重建 observer）
    await wrapper.setProps({ loading: true })
    await wrapper.setProps({ loading: false })
    vi.advanceTimersByTime(300)
    await nextTick()
    expect(MockIntersectionObserver.instances).toHaveLength(2)

    // 关键：**新** observer 还没触发，就绝不能再发（这是"无限循环"的反面）
    expect(wrapper.emitted('load-more')).toHaveLength(1)

    // 只有哨兵重新进入可见区，才发下一次
    MockIntersectionObserver.instances[1].trigger()
    expect(wrapper.emitted('load-more')).toHaveLength(2)
  })
})
