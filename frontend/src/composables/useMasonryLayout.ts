import { ref, watch, nextTick, computed, type Ref, type ComputedRef } from 'vue'
import { useWindowSize, useElementSize, useDebounceFn } from '@vueuse/core'

/** 一列宽度变化后多久重排（拖窗口时避免每一像素都重排）。 */
export const MASONRY_RESIZE_DEBOUNCE_MS = 120
/** 默认列间距：与模板的 `gap-6` 一致（1.5rem = 24px）。 */
export const MASONRY_GAP_PX = 24
/** 容器还没量到宽度时的兜底估算：`max-w-7xl`(1280) 减去水平内边距。 */
const FALLBACK_CONTENT_MAX = 1280
const FALLBACK_HORIZONTAL_PADDING = 64

export interface MasonryPosition {
  left: number
  top: number
}

export interface UseMasonryLayoutOptions {
  /** 列间距（px），默认 24。 */
  gap?: number
}

/**
 * 瀑布流布局（列优先、最短列优先）。
 *
 * **关键设计：位置是「算出来的」，不是「渲染结构」。**
 *
 * 旧实现把 `columns: T[][]` 当渲染结构（模板 `v-for` 套 `v-for`），于是列数一变
 * `columns` 的二维形状就变 → `:key` 全变 → Vue 把**每一张卡片卸载再重挂**。
 * 实测（真实 Firefox，600 张卡）跨 1000px 断点拖窗口，主线程单帧卡 **1289 ms**。
 *
 * 现在卡片始终是一个**平坦列表**（数据顺序，`:key` 稳定），位置通过
 * `cardStyle(index)` 写 `left`/`top`。列数变化只重算位置 → **零组件重挂**，
 * 同一动作实测 **31 ms**（41 倍）。
 *
 * 三条纪律：
 * 1. **先渲染、后测量**：卡片先以 `visibility:hidden` 进入 DOM，`nextTick` 之后
 *    再批量读 `offsetHeight`——第一次读触发一次布局，之后 N 次读全部命中缓存。
 *    绝不「放一张 → 读一次」（那是 O(n) 次强制同步布局）。
 * 2. **只追加时只量新增的**：load-more 从既有列高继续，不重算历史。
 * 3. **不再逐条 `await nextTick()`**：串行闸门 `layoutRunning` 保留（并发保护），
 *    但循环内部没有异步步骤。
 */
export function useMasonryLayout<T>(
  items: Ref<T[]> | ComputedRef<T[]>,
  options: UseMasonryLayoutOptions = {},
) {
  const gap = options.gap ?? MASONRY_GAP_PX

  // 响应式窗口宽度：列数仍然按窗口宽度决定（与旧行为逐项等价，避免
  // 「侧边栏展开/收起」这类容器抖动把列数来回翻）。
  const { width: windowWidth } = useWindowSize()

  // 容器实测宽度：绝对定位必须知道「一列到底多宽」，不能用窗口宽度估算
  // （主内容区比窗口窄，直接用窗口宽度会让列溢出）。
  const containerRef = ref<HTMLElement | null>(null)
  const { width: containerWidth } = useElementSize(containerRef)

  const columnCount = computed(() => {
    if (windowWidth.value >= 1000) return 3
    if (windowWidth.value >= 500) return 2
    return 1
  })

  /**
   * 布局用的可用宽度。
   *
   * 容器还没被 ResizeObserver 量到时（首帧、以及 jsdom 没有布局的场景）回落到
   * 按窗口宽度估算——否则 `columnWidth` 恒为 0，布局永远不跑。
   * 兜底值只在首帧/测试里生效，真实浏览器第二次 `nextTick` 就会被实测值替换。
   */
  const effectiveWidth = computed(() => {
    if (containerWidth.value > 0) return containerWidth.value
    return Math.max(0, Math.min(windowWidth.value, FALLBACK_CONTENT_MAX) - FALLBACK_HORIZONTAL_PADDING)
  })

  const columnWidth = computed(() => {
    const count = columnCount.value
    const width = effectiveWidth.value
    if (!width || !count) return 0
    return Math.floor((width - gap * (count - 1)) / count)
  })

  /** 每张卡片的位置，**下标与 `items` 同序**。 */
  const positions = ref<MasonryPosition[]>([])
  /** 瀑布流总高度（px）：给外层容器一个确定高度，绝对定位的子元素才不会塌。 */
  const totalHeight = ref(0)

  // 上一次布局的几何指纹：与自己不一致 → 不能「只追加」，必须全量重建。
  let laidOutColumnCount = 0
  let laidOutColumnWidth = 0
  // 上一次布局看到的**首条 item**。列表从头部裁剪时（`splice(0, drop)`
  // 后立刻 `push(...)`），长度可能前后相等、数组引用也没变——长度/引用两个
  // watcher 都不会触发。首条 item 的身份是「头部真的被裁了」的唯一可靠信号。
  let laidOutFirstItem: unknown = undefined
  // 累积列高（含每张卡之后的 gap），跨 load-more 保留。
  let colHeights: number[] = []

  // 卡片元素：模板用 `setItemRef(index, el)` 挂进来，顺序与 items 一致。
  const itemEls: (HTMLElement | null)[] = []

  /**
   * 把一个 ref 回调给的「东西」解包成真正的 DOM 元素。
   *
   * `:ref` 挂在**组件**上时（`<NovelCard :ref="...">`），Vue 传进来的是
   * **组件实例**（`{ $el, $, ... }`），不是元素。直接 `el.style.position = ...`
   * 会抛 `TypeError: t.style is undefined`——布局每次都在这里炸掉，
   * `totalHeight` 永远停在 0，网格容器 `visibility:hidden` 永不解除，
   * 页面表现为「无限转圈、卡片一张都没出来」。
   *
   * 单元测试里喂的是裸 DOM 元素，所以测不出这个差异；必须在这里兼容两种形状。
   */
  const toElement = (ref: unknown): HTMLElement | null => {
    if (!ref) return null
    // 组件实例：取 $el（Vue 3 里是根的 DOM 元素）
    if (typeof ref === 'object' && '$el' in (ref as Record<string, unknown>)) {
      const el = (ref as { $el?: unknown }).$el
      return el instanceof HTMLElement ? el : null
    }
    return ref instanceof HTMLElement ? ref : null
  }

  const setItemRef = (index: number, ref: unknown) => {
    if (index < 0) return
    itemEls[index] = toElement(ref)
  }

  const runLayout = async (force: boolean) => {
    const count = columnCount.value
    const cw = columnWidth.value
    // 宽度还没量到：等下一次（容器尺寸变化会再触发一次）。
    if (!cw || !count) return

    const list = items.value
    const total = list.length

    // 能否只量新增的那几张？要求：不强制、已有布局、列数与列宽都没变、
    // 且没有裁掉头部（positions 长度 ≤ 总数）。
    // 头部被裁过（首条身份变了）时不能只量新增：位置表是按**旧下标**排的，
    // 下移了 drop 条之后每条都错位。宁可全量重建（300 张 ≈ 45ms），也不能
    // 让卡片按错位的下标落位。
    const headChanged = total > 0 && items.value[0] !== laidOutFirstItem
    const canAppend =
      !force &&
      !headChanged &&
      positions.value.length > 0 &&
      positions.value.length <= total &&
      laidOutColumnCount === count &&
      laidOutColumnWidth === cw

    const start = canAppend ? positions.value.length : 0

    if (!canAppend) {
      // 全量重建：先清空位置（所有卡片立刻 hidden），再等 DOM 以新列宽就位。
      // 这一步不是「为了测量」而做的无用功——它是把「卡片宽度」这个写操作
      // 提交下去，随后的批量读才有意义。
      positions.value = []
      totalHeight.value = 0
      colHeights = new Array(count).fill(0)
    }

    await nextTick()

    // ---- 批量测高：1 次强制布局（第一读），其余命中布局缓存 ----
    const heights: number[] = []
    for (let i = start; i < total; i++) {
      const el = itemEls[i]
      heights.push(el ? el.offsetHeight : 0)
    }

    // ---- 纯 JS 分配：零 DOM 读 ----
    if (!canAppend) colHeights = new Array(count).fill(0)
    const nextPositions: MasonryPosition[] = canAppend
      ? positions.value.slice(0, start)
      : []

    for (let i = start; i < total; i++) {
      let minIndex = 0
      for (let k = 1; k < count; k++) {
        if (colHeights[k] < colHeights[minIndex]) minIndex = k
      }
      nextPositions.push({
        left: minIndex * (cw + gap),
        top: colHeights[minIndex],
      })
      colHeights[minIndex] += heights[i - start] + gap
    }

    laidOutColumnCount = count
    laidOutColumnWidth = cw
    laidOutFirstItem = total > 0 ? items.value[0] : undefined

    // ---- 写位置：**命令式直接改 DOM style**，不经过响应式 ----
    //
    // 为什么不是「赋一个 ref，让模板 :style 绑定去更新」：那样 300 张卡 = 300 个
    // NovelCard 组件全部重渲染（每个都有 computed / 事件监听重新求值），实测
    // 单帧还要 338ms。命令式写 300 个 `el.style.top` 是纯样式操作，实测 <10ms。
    //
    // 代价：这一层绕过了 Vue 的声明式绑定，所以必须自己保证
    //   1) 每张卡都被写到（含 start 之前的历史卡片——列宽变了它们也要挪）；
    //   2) 卡片换列时宽度也跟着写；
    //   3) 没位置的卡片隐藏，避免在 (0,0) 叠成一堆。
    const widthPx = cw + 'px'
    for (let i = 0; i < total; i++) {
      const el = itemEls[i]
      if (!el) continue
      const pos = nextPositions[i] as MasonryPosition | undefined
      el.style.position = 'absolute'
      el.style.width = widthPx
      if (pos) {
        el.style.left = pos.left + 'px'
        el.style.top = pos.top + 'px'
        el.style.visibility = 'visible'
      } else {
        el.style.visibility = 'hidden'
      }
    }

    positions.value = nextPositions
    totalHeight.value = colHeights.length
      ? Math.max(...colHeights.map(h => Math.max(0, h - gap)))
      : 0
  }

  // 串行闸门（旧实现同款，保留是对的）：同一时刻只允许一个布局循环，
  // 中途再来的触发被合并，当前循环结束后按「更强的那个」再跑一次。
  // 没有这道闸门时，两个触发交错会把同一批 item 分配两遍。
  let layoutRunning = false
  let layoutPending: boolean | null = null

  const layoutItems = (force = false) => {
    if (layoutRunning) {
      layoutPending = layoutPending === null ? force : layoutPending || force
      return
    }
    layoutRunning = true
    void runLayout(force).finally(() => {
      layoutRunning = false
      const next = layoutPending
      layoutPending = null
      if (next !== null) layoutItems(next)
    })
  }

  // ---- 触发 1：列宽变化（含列数变化）→ 卡片高度全部失效 → 必须全量重建 ----
  // 拖窗口时列宽会连续变化，所以走 debounce；首帧（还没任何位置）立即跑，
  // 否则初始布局会被推迟 120ms。
  const debouncedRelayout = useDebounceFn(() => layoutItems(true), MASONRY_RESIZE_DEBOUNCE_MS)
  watch(columnWidth, () => {
    if (positions.value.length === 0) layoutItems(true)
    else debouncedRelayout()
  })

  // ---- 触发 2：数组被整体替换（搜索 / 换书架 / 集合视图切换）→ 全量重建 ----
  // 只比引用：`push` 不会改引用，交给下面的长度 watch。
  watch(items, (next, prev) => {
    if (!prev || next === prev) return
    layoutItems(true)
  })

  // ---- 触发 3：长度变化（首屏 / load-more）→ 只量新增 ----
  watch(() => items.value.length, () => layoutItems(false), { immediate: true })

  // ---- 触发 4：首条 item 变化（列表越过上限、从头部裁掉最老的）→ 全量重建 ----
  // 长度可能是净零变化（裁 30 + 追加 30），所以必须单独看首条身份。
  watch(() => items.value[0], (next, prev) => {
    if (prev === undefined) return // 首屏由 immediate 的长度 watcher 负责
    if (next === prev) return
    layoutItems(true)
  })

  /** 外部强制重排（编辑保存改了卡片高度、集合视图换了一批数据）。 */
  const relayout = () => layoutItems(true)

  /**
   * 卡片的**静态**内联样式：只负责「一开始就绝对定位 + 宽度 + 隐藏」。
   *
   * `left`/`top` 故意**不**走响应式绑定——它们由 `runLayout` 命令式直接写到
   * 元素上（见那里的注释：走响应式会让 300 个组件全部重渲染，单帧 338ms）。
   * 这里给出一份初始值，让首帧就是 hidden + 正确宽度，不会出现「以流式布局
   * 堆叠一瞬再跳到绝对定位」的闪烁。
   */
  const cardStyle = (_index: number) => ({
    position: 'absolute' as const,
    width: columnWidth.value + 'px',
    left: '0px',
    top: '0px',
    visibility: 'hidden' as const,
  })

  return {
    /** 挂到滚动容器上（量宽度用）。 */
    containerRef,
    columnCount,
    columnWidth,
    totalHeight,
    positions,
    cardStyle,
    setItemRef,
    relayout,
  }
}
