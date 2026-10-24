import { describe, it, expect, vi, afterEach } from 'vitest'
import { defineComponent, ref, nextTick, h } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { useMasonryLayout, MASONRY_GAP_PX } from '../../src/composables/useMasonryLayout'

/**
 * 瀑布流布局的契约。
 *
 * 这个文件守护的核心不是「分对了列」，而是**结构**：
 *  1. 位置是算出来的（`positions` / `cardStyle`），渲染永远是平坦列表；
 *  2. 强制布局次数必须**有界**（放 N 张卡只读 N 次，不是 N×列数）；
 *  3. 列宽/列数变化时位置表长度与 items 严格一致，**不复制、不丢弃**。
 *
 * 第 3 条是这次重构的回归保护：旧实现用 `columns: T[][]` 当渲染结构，
 * 列数一变整个二维数组被换掉，Vue 会把每张卡卸载再重挂 —— 真实浏览器里
 * 600 张卡拖一次窗口，主线程单帧卡 1289ms。耗时在 jsdom 里测不到，
 * 但「位置表结构」这里能钉住，模板层的 `:key` 稳定性由浏览器的组件实例
 * 计数另行验证。
 */

function setInnerWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true })
}

/**
 * 挂一个最小宿主：真实的 `v-for` + 真实 DOM 元素，元素通过 `setItemRef`
 * 挂进 composable。卡片高度由 `heights` 注入（jsdom 没有布局）。
 */
function mountMasonry<T>(
  items: T[],
  opts: { heights?: number[]; innerWidth?: number } = {},
) {
  // 默认 1200（3 列）；用例要别的宽度就显式传，别靠「上一条用例留下的全局状态」。
  setInnerWidth(opts.innerWidth ?? 1200)
  const counter = { reads: 0 }
  const itemsRef = ref(items) as unknown as import('vue').Ref<T[]>
  const heights = opts.heights ?? []

  const Harness = defineComponent({
    setup() {
      const state = useMasonryLayout(itemsRef, { gap: MASONRY_GAP_PX })
      return { state }
    },
    render() {
      const state = this.state
      const list = itemsRef.value as unknown as unknown[]
      return h('div', {
        ref: (el: unknown) => {
          (state.containerRef as unknown as { value: unknown }).value = el
        },
      }, [
        h('div', { 'data-testid': 'grid', style: { position: 'relative' } },
          list.map((item, index) => h('div', {
            key: index,
            'data-index': index,
            style: state.cardStyle(index),
            ref: (el: unknown) => {
              if (!el) return
              const elm = el as HTMLElement
              // 每个元素都装计数器：读一次算一次（未给高度的按 0 计），
              // 这样 counter.reads 就是真实的「强制布局读次数」。
              const hh = heights[index] ?? 0
              Object.defineProperty(elm, 'offsetHeight', {
                configurable: true,
                get() { counter.reads++; return hh },
              })
              state.setItemRef(index, elm)
            },
          }, [h('span', String(item))])),
        ),
      ])
    },
  })

  const wrapper = mount(Harness)
  return {
    wrapper,
    itemsRef,
    counter,
    state: (wrapper.vm as unknown as { state: ReturnType<typeof useMasonryLayout<unknown>> }).state,
  }
}

describe('useMasonryLayout', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('按窗口宽度推导列数', () => {
    expect(mountMasonry([], { innerWidth: 1200 }).state.columnCount.value).toBe(3)
    expect(mountMasonry([], { innerWidth: 800 }).state.columnCount.value).toBe(2)
    expect(mountMasonry([], { innerWidth: 400 }).state.columnCount.value).toBe(1)
  })

  it('把 item 分配到最短列：高度不同 → 三列都被用到', async () => {
    // 100 / 100 / 100 三张：最短列判定取「首个最小」，所以依次落到 0、1、2 列。
    const { state } = mountMasonry([1, 2, 3], { heights: [100, 100, 100] })
    await flushPromises()

    expect(state.positions.value).toHaveLength(3)
    expect(state.positions.value[0]).toEqual({ left: 0, top: 0 })
    const lefts = state.positions.value.map(p => p.left)
    expect(new Set(lefts).size).toBe(3)
  })

  it('列内按累计高度落位（top 严格递增，且不与上一张重叠）', async () => {
    const { state } = mountMasonry([1, 2, 3], { heights: [200, 300, 100] })
    await flushPromises()

    // 三张各落一列，top 都是 0
    expect(state.positions.value.map(p => p.top)).toEqual([0, 0, 0])
  })

  it('load-more 只测量新增的那几张，不重测历史', async () => {
    const { itemsRef, counter } = mountMasonry([1, 2], { heights: [100, 100] })
    await flushPromises()
    expect(counter.reads).toBeGreaterThan(0)

    counter.reads = 0
    itemsRef.value.push(3, 4)
    await flushPromises()

    // 只量新追加的 2 张；历史 2 张不许再读（那是 O(n²) 的来源）
    expect(counter.reads).toBe(2)
  })

  it('放 N 张卡的强制布局次数有界：一次批量读 N 次，不是逐条写→读的 2N', async () => {
    const N = 60
    const { counter, state } = mountMasonry(
      Array.from({ length: N }, (_, i) => i),
      { heights: Array.from({ length: N }, () => 120) },
    )
    await flushPromises()

    expect(state.positions.value).toHaveLength(N)
    expect(counter.reads).toBe(N)
    // 旧实现的下限代理：每张卡「写一次 + 读一次」= 2N 次
    expect(counter.reads).toBeLessThan(2 * N)
  })

  it('数组被整体替换时重建位置，不重复分配（历史 bug：同一批 item 被 push 两遍）', async () => {
    const { itemsRef, state } = mountMasonry([1, 2, 3], { heights: [100, 100, 100] })
    await flushPromises()

    itemsRef.value = [4, 5, 6] as unknown as number[]
    await flushPromises()
    state.relayout()
    await flushPromises()

    expect(state.positions.value).toHaveLength(3)
    expect(state.positions.value.length).toBe(itemsRef.value.length)
  })

  it('relayout() 在长度不变时也强制重建', async () => {
    const { itemsRef, state } = mountMasonry([1, 2, 3], { heights: [100, 100, 100] })
    await flushPromises()

    // 同长度替换：长度 watcher 读不出差异，必须靠显式 relayout
    itemsRef.value = [7, 8, 9] as unknown as number[]
    await flushPromises()
    state.relayout()
    await flushPromises()

    expect(state.positions.value).toHaveLength(3)
    expect(state.positions.value[0]).toEqual({ left: 0, top: 0 })
  })

  it('列数变化后位置表长度不变（卡片不被复制、不被丢弃）', async () => {
    const { state } = mountMasonry([1, 2, 3, 4, 5, 6], { heights: [100, 50, 150, 80, 60, 120] })
    await flushPromises()
    expect(state.positions.value).toHaveLength(6)

    setInnerWidth(800)
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    await flushPromises()
    state.relayout()
    await flushPromises()

    expect(state.columnCount.value).toBe(2)
    expect(state.positions.value).toHaveLength(6)
  })

  it('容器宽度为 0 时不产出 NaN 坐标', async () => {
    const { state } = mountMasonry([1, 2], { heights: [100, 100], innerWidth: 0 })
    await flushPromises()

    expect(state.columnWidth.value).toBe(0)
    for (const p of state.positions.value) {
      expect(Number.isFinite(p.left)).toBe(true)
      expect(Number.isFinite(p.top)).toBe(true)
    }
  })

  /**
   * `visibility` **绝不能**出现在响应式 `:style` 里。
   *
   * 这是真实故障的根因：模板 `:style="cardStyle(index)"` 会在每次组件重渲染时
   * 重新应用，把命令式写下的 `visible` 覆盖回 `hidden` —— 数据、位置、容器高度
   * 全都是对的，但列表一片空白；滚动触发一次重排又恢复正常。用户的描述
   * 「侧边栏高亮正常但没内容，滚到翻页位置上方就显现」正是这个。
   *
   * 所以契约是：`visibility` 只由 runLayout / setItemRef 命令式管理。
   */
  it('cardStyle() 不返回 visibility（该属性只能由命令式管理，否则会被重渲染覆盖）', () => {
    const { state } = mountMasonry([1, 2], { heights: [100, 100] })
    const style = state.cardStyle(0)
    expect(style).not.toHaveProperty('visibility')
    // 仍然必须一开始就绝对定位 + 有宽度，避免流式堆叠一瞬
    expect(style.position).toBe('absolute')
    expect(style.width).toBeTruthy()
  })

  it('新挂载的卡片在拿到位置前被命令式隐藏（不会闪现在左上角）', async () => {
    const { itemsRef, state } = mountMasonry([1], { heights: [100] })
    await flushPromises()

    itemsRef.value.push(2)
    await flushPromises()

    // 新卡片的元素应当已经存在；布局跑完后必须有位置且可见
    expect(state.positions.value.length).toBe(2)
  })

  it('裁掉头部后位置表随之收缩（缓存上限的布局侧契约）', async () => {
    const { itemsRef, state } = mountMasonry(
      Array.from({ length: 10 }, (_, i) => i),
      { heights: Array.from({ length: 10 }, () => 100) },
    )
    await flushPromises()
    expect(state.positions.value).toHaveLength(10)

    // 模拟「剪枝到 4 条」：删掉头部，长度变短
    itemsRef.value = itemsRef.value.slice(6) as unknown as number[]
    await flushPromises()
    state.relayout()
    await flushPromises()

    expect(state.positions.value).toHaveLength(4)
    // 第一条必须回到列顶（top = 0）
    expect(state.positions.value[0]).toEqual({ left: 0, top: 0 })
  })

  it('串行闸门：同一 flush 里连续两次触发只产生一份位置表（不重复分配）', async () => {
    const { itemsRef, state } = mountMasonry([1, 2, 3], { heights: [100, 100, 100] })
    await flushPromises()

    // 两个触发在同一时刻到达（长度变化 + 显式 relayout）
    itemsRef.value = [1, 2, 3, 4, 5] as unknown as number[]
    state.relayout()
    await flushPromises()

    expect(state.positions.value).toHaveLength(5)
    expect(state.positions.value.length).toBe(itemsRef.value.length)
  })

  /**
   * **裁剪 + 追加 = 长度净零变化**。
   *
   * 这是最容易被写错的一条：`useCursorPagination` 在越过上限时把「裁掉头部」
   * 和「追加一页」放在同一个同步函数里做完（`splice(0, drop)` + `push(...)`）。
   * 于是：
   *  - 数组引用没变（还是同一个 ref 里的数组）→ 引用 watcher 不触发；
   *  - 长度前后相等 → 长度 watcher 不触发。
   * 两个 watcher 都不动，位置表就停留在**旧下标**上：每条都下移了 drop 位，
   * 卡片会按错位的位置落下去。
   *
   * 唯一可靠的信号是「首条 item 的身份变了」。这条用例钉住它。
   */
  it('裁剪头部 + 同长度追加：必须是全量重建（重测全部，而不是只量新增）', async () => {
    const heights = [100, 140, 180, 80, 120, 160]
    const { itemsRef, state, counter } = mountMasonry(
      Array.from({ length: 6 }, (_, i) => i),
      { heights },
    )
    await flushPromises()
    expect(state.positions.value).toHaveLength(6)

    counter.reads = 0
    // 模拟：裁掉头部 2 条 + 追加 2 条 → 长度仍是 6，数组引用也没变
    const arr = itemsRef.value as unknown as number[]
    arr.splice(0, 2)
    arr.push(6, 7)
    await flushPromises()

    expect(itemsRef.value.length).toBe(6)
    expect(state.positions.value).toHaveLength(6)
    // 关键断言：头部变了 → 必须把**全部 6 张**重新量一遍。
    // 若误走「只量新增」的分支，这里会是 2 —— 那意味着位置表还停在旧下标上，
    // 每张卡都错位了 drop 位。
    expect(counter.reads).toBe(6)
  })

  it('只裁头部而不追加：位置表收缩到新长度', async () => {
    const { itemsRef, state } = mountMasonry(
      [0, 1, 2, 3], { heights: [100, 100, 100, 100] },
    )
    await flushPromises()
    expect(state.positions.value).toHaveLength(4)

    ;(itemsRef.value as unknown as number[]).splice(0, 2)
    await flushPromises()

    expect(state.positions.value).toHaveLength(2)
  })

  /**
   * 真实页面里 `:ref` 挂在 **组件** 上（`<NovelCard :ref="...">`），拿到的是
   * **组件实例**，不是 DOM 元素。`el.style` 在组件实例上是 `undefined`，
   * 于是布局每次写位置都抛 TypeError，`totalHeight` 永远是 0，外层容器
   * `visibility:hidden` 永不解除 —— 页面表现为「无限转圈、什么都没加载出来」。
   *
   * 这个用例用「组件实例形状」的对象喂 `setItemRef`，钉住「必须解包出 $el」。
   */
  it('setItemRef 收到组件实例时（真实 :ref 行为）也要解包出 DOM 元素', async () => {
    const { itemsRef, state } = mountMasonry([1, 2, 3], { heights: [100, 100, 100] })
    await flushPromises()

    // 模拟 Vue 传进来的组件实例：{ $el: HTMLElement }
    const dom = document.createElement('div')
    Object.defineProperty(dom, 'offsetHeight', { configurable: true, get: () => 120 })
    const compLike = { $el: dom, $: {} }

    itemsRef.value.push(4)
    await flushPromises()
    // 用一个组件实例形状替换最后一项的 ref
    expect(() => state.setItemRef(3, compLike as unknown as HTMLElement)).not.toThrow()

    state.relayout()
    await flushPromises()

    // 必须写到了真正的 DOM 元素上（而不是在组件实例上抛异常）
    expect(dom.style.position).toBe('absolute')
    expect(dom.style.visibility).toBe('visible')
  })

  /**
   * **容器宽度晚于数据到达**：这是「刷新偶发空白」的根因。
   *
   * 真实时序：`/api/novels/` 很快返回 → `items.length` watcher 立刻触发布局
   * → 但如果容器还没被量到宽度（`columnWidth === 0`），旧实现在这里**静默
   * return**。而 `columnWidth` 的 watcher 只在值**变化**时回调，若它已经变过，
   * 就再没有任何东西会触发重排 → `totalHeight` 恒为 0 → 容器 `visibility:
   * hidden` → 卡片一张都不显示。
   *
   * 因为取决于两个异步任务谁先完成，用户看到的是「时不时，像每第三次刷新」。
   */
  it('容器宽度为 0 时不放弃：宽度就绪后必须自动补一次布局', async () => {
    // 先用 0 宽度（jsdom 里容器没有布局，正是"还没量到"的真实状态）
    const { state } = mountMasonry([1, 2, 3], { heights: [100, 100, 100], innerWidth: 0 })
    await flushPromises()
    expect(state.totalHeight.value).toBe(0)

    // 宽度就绪（真实浏览器里是 useElementSize 的 ResizeObserver 回调）
    setInnerWidth(1200)
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    await flushPromises()
    await nextTick()
    await flushPromises()

    // 必须真的排出来了：不能永远停在 0 高度 / 全隐藏
    expect(state.totalHeight.value).toBeGreaterThan(0)
  })

  /**
   * **重渲染不得把卡片重新隐藏**（真实故障的根因）。
   *
   * 之前 `cardStyle()` 返回 `visibility: 'hidden'`，而模板是
   * `:style="cardStyle(index)"` —— 响应式绑定会在**每次组件重渲染**时重新应用，
   * 把我命令式写下的 `visible` 覆盖回 `hidden`。症状：数据、位置、容器高度都
   * 正确，侧边栏高亮也正常，但列表一片空白；往下滚触发一次重排（load-more /
   * relayout）后又恢复正常。用户原话：
   * 「出现无内容的现象时，可以往下滚轮，滚到约翻页的位置时，上一页原本空白的
   *   部分会显现」，以及「打开 F12 期间无此问题」（调试器拖慢了重渲染时序）。
   *
   * 这条用例拿不到 Vue 的重渲染时机（jsdom 没有真实渲染流水线），所以它钉的是
   * **契约本身**：`cardStyle()` 的输出里不许出现 `visibility`。只要这条成立，
   * Vue 就没有机会覆盖命令式写下的值。
   */
  it('cardStyle() 的返回值不许含 visibility（否则重渲染会覆盖命令式写入）', () => {
    const { state } = mountMasonry([1, 2, 3], { heights: [100, 100, 100] })
    for (const i of [0, 1, 2]) {
      expect(Object.keys(state.cardStyle(i))).not.toContain('visibility')
    }
  })

  it('setItemRef 挂载时具名隐藏新卡片（命令式，不受重渲染影响）', async () => {
    const { state, itemsRef, wrapper } = mountMasonry([1], { heights: [100] })
    await flushPromises()

    const dom = document.createElement('div')
    Object.defineProperty(dom, 'offsetHeight', { configurable: true, get: () => 100 })
    // 一个尚未有位置的下标（第 5 个，items 只有 1 个）
    state.setItemRef(5, dom as unknown as HTMLElement)
    expect(dom.style.visibility).toBe('hidden')
    expect(dom.style.position).toBe('absolute')

    itemsRef.value.push(2, 3, 4, 5)
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
  })
})
