import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { defineComponent, ref, h, nextTick, watch } from 'vue'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import * as scrollAnchorModule from '../../src/composables/useScrollAnchor'
import { useLoadMoreAnchor } from '../../src/composables/useLoadMoreAnchor'

/**
 * `useLoadMoreAnchor` 的契约：load-more 之后「把用户正在读的那张卡放回原位」
 * 的时序。这段逻辑过去只活在 `Novels.vue` 里，是前端最吃时序的一段：
 *
 *  - 越过 `DEFAULT_MAX_ITEMS` 时，`useCursorPagination` 从**头部**裁掉最老的
 *    卡片，整份绝对定位的列表瞬间上移（实测 30 张约 2500px），用户正看着的
 *    那张跳走；补偿必须**恰好**落在 DOM 打补丁 + 瀑布流重排之后。
 *  - 头部**没被裁**（只是尾巴追加了一段）时绝不能补偿：多滚一次就是把用户
 *    往反方向甩一次。首条 id 没变 = 头没动——这是唯一可靠信号，因为裁剪与
 *    追加在同一个同步函数里完成，list 长度前后一样，长度 watcher 根本不触发。
 *  - 连续两次 load-more：上一次抓的锚点作废，否则旧锚点会在新数据上落地，
 *    把滚动位置拽到用户早就不在的地方。
 *  - 卸载后不许再补偿：列表已经不在，动了就是动别人页面的滚动位置。
 *
 * jsdom 没有布局，所以几何全靠注入：
 *  - 每张卡的 `getBoundingClientRect` 读的是它自己的 `style.top`（绝对值 =
 *    相对视口顶的偏移），正负都可控；
 *  - 「数据变更 → 瀑布流落位」由一个**模拟的 `useMasonryLayout`** 完成：它在
 *    首条 id 变化后**先等一跳 nextTick**再命令式写坐标——真实实现
 *    （`useMasonryLayout.layoutItems`）就是这个节奏。于是「两跳 nextTick」这条
 *    契约真的被测到了：少一跳就会量到**旧坐标**，补偿变成 0（见判据二的说明）。
 */

/** 单个卡片的注入高度：`bottom = top + CARD_HEIGHT`（用于「底边是否已滚出视口」的判定）。 */
const CARD_HEIGHT = 200

/** 裁掉头部 30 张的真实量级（useScrollAnchor 注释里的实测值）。 */
const TRIM_SHIFT_PX = 2500

let scrollByMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  // jsdom 的 `window.scrollBy` 是 "not implemented" 的空壳（还会打日志），
  // 换成 spy 才能断言「补偿了没有、补了多少」。
  scrollByMock = vi.fn()
  vi.stubGlobal('scrollBy', scrollByMock)
  // jsdom 不实现 `CSS.escape`（restoreScrollAnchor 用它拼属性选择器）。
  // 本文件的 id 都是纯数字，直接返回入参即可。
  vi.stubGlobal('CSS', { escape: (value: string) => value })
})

const mounted: VueWrapper[] = []

afterEach(() => {
  // 用例五自己卸载过；重复卸载在 VTU 里是 no-op，这里再兜一层，保证容器不泄漏。
  for (const wrapper of mounted.splice(0)) {
    try {
      wrapper.unmount()
    } catch {
      /* 已经卸载过了 */
    }
  }
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

interface HarnessOptions {
  items: number[]
  /** 初始坐标：id → 卡片顶边相对视口顶的偏移（缺省 0）。 */
  tops?: Record<number, number>
  /** `false` = 容器还没渲染（首帧 / 空列表）：`containerRef` 始终是 null。 */
  withContainer?: boolean
  /** 传入则接线 `layoutRevision`（真实视图就是这样接的），否则走两跳 nextTick 旧路径。 */
  withRevision?: boolean
  /**
   * 模拟瀑布流「数据变更后第几跳才写坐标」。默认 1 = 真实实现的节奏；
   * 调大用来造出「布局比旧实现赌的两跳更晚」这一真实可能发生的时序。
   */
  layoutDelayTicks?: number
}

/**
 * 挂一个最小宿主：真实的 `v-for` + 真实 DOM 元素 + 模拟的瀑布流。
 *
 * 卡片坐标**不是**响应式 `:style`——真实视图里瀑布流也是命令式写 `left/top`
 * （`cardStyle()` 只允许返回 `position`，见 useMasonryLayout 的注释）。
 * 测试通过 `planLayout()` 预约「下一次数据变更后要落到的坐标」。
 */
function mountHarness(opts: HarnessOptions) {
  const items = ref(opts.items)
  const initialTops = new Map<number, number>(
    Object.entries(opts.tops ?? {}).map(([id, top]) => [Number(id), top]),
  )
  const containerRef = ref<HTMLElement | null>(null)
  // 视图里这一句是 `emit('novels-command', { type: 'load-more' })`。
  const trigger = vi.fn()
  const withContainer = opts.withContainer !== false
  // 模拟 useMasonryLayout 的 layoutRevision：每次真正写坐标时 +1。
  const layoutRevision = ref(0)

  /** 预约坐标：下一次「数据变更 → 瀑布流落位」时写进去。 */
  let plannedTops: Record<number, number> | null = null
  const planLayout = (tops: Record<number, number>) => {
    plannedTops = tops
  }

  /** 模拟瀑布流落位：命令式写坐标（真实实现同样绕开响应式绑定）。 */
  const writeLayout = () => {
    const plan = plannedTops
    plannedTops = null
    if (!plan) return
    const root = containerRef.value
    for (const [id, top] of Object.entries(plan)) {
      const el = root?.querySelector<HTMLElement>(`[data-anchor-id="${id}"]`)
      if (el) el.style.top = `${top}px`
    }
    // 真实实现里 runLayout 就在写完位置后递增；这里保持同一时序。
    layoutRevision.value++
  }

  /** 卡片的注入几何：读它自己的 `style.top`，于是正负、位移都可控。 */
  const rectOf = (el: HTMLElement): DOMRect => {
    const top = Number.parseFloat(el.style.top || '0')
    return {
      top,
      bottom: top + CARD_HEIGHT,
      height: CARD_HEIGHT,
      left: 0,
      right: 0,
      width: 0,
      x: 0,
      y: top,
      toJSON: () => ({}),
    } as DOMRect
  }

  const Harness = defineComponent({
    setup() {
      // 模拟 useMasonryLayout 的「首条 id 变了 → 全量重建」：DOM 打补丁要等
      // 一跳 nextTick，坐标在那之后才写得下去。视图里这个 watcher 建在
      // useLoadMoreAnchor 之前（Novels.vue 先 useMasonryLayout、后本组合式函数），
      // 这里保持同样的建立顺序。
      watch(
        () => items.value[0],
        async (next, prev) => {
          if (prev === undefined || next === prev) return
          for (let i = 0; i < (opts.layoutDelayTicks ?? 1); i++) await nextTick()
          writeLayout()
        },
      )
      const anchor = useLoadMoreAnchor({
        container: containerRef,
        // 视图传 `() => displayNovels.value[0]?.id`，这里等价地读数组首项。
        firstItem: () => items.value[0],
        ...(opts.withRevision ? { layoutRevision } : {}),
      })
      return { items, anchor }
    },
    render() {
      const children = (items.value as number[]).map((id) =>
        h('div', {
          key: id,
          'data-anchor-id': String(id),
          ref: (el: unknown) => {
            if (!el) return
            const elm = el as HTMLElement
            elm.style.position = 'absolute'
            // 初始坐标只写一次：之后由 planLayout / writeLayout 负责，
            // 绝不在渲染里重新绑定（那正是 cardStyle 那条契约的反面教材）。
            if (!elm.style.top) elm.style.top = `${initialTops.get(id) ?? 0}px`
            elm.getBoundingClientRect = () => rectOf(elm)
          },
        }),
      )
      if (!withContainer) return h('div', { 'data-testid': 'no-grid' })
      return h(
        'div',
        { ref: (el: unknown) => { containerRef.value = el as HTMLElement | null } },
        children,
      )
    },
  })

  const wrapper = mount(Harness)
  mounted.push(wrapper)
  return {
    wrapper,
    items,
    trigger,
    planLayout,
    layoutRevision,
    anchor: (wrapper.vm as unknown as { anchor: ReturnType<typeof useLoadMoreAnchor> }).anchor,
  }
}

/**
 * 走完一次「数据变更 → 两跳 nextTick → 补偿落地」的链条。
 * nextTick 链是微任务，`flushPromises()` 已经足够；再补一跳是不依赖事件循环细节。
 */
async function settle() {
  await flushPromises()
  await nextTick()
  await flushPromises()
}

describe('useLoadMoreAnchor', () => {
  /**
   * 契约一：**头没被裁就不许补偿**。
   *
   * 真实故障：正常滚动加载只往尾巴追加一页时，如果也执行补偿，用户会被往
   * 反方向甩一次。这种「多滚一下」的 bug 只在追加页到达时偶发，肉眼说不清
   * 是被谁动的。首条 id 是判断「头动了没有」的唯一可靠信号——裁剪与追加在
   * 同一个同步函数里完成，长度前后相等，长度 watcher 根本不触发。
   */
  it('首条 id 没变（只在尾巴追加）→ 放行加载，但绝不 scrollBy', async () => {
    const { items, trigger, anchor } = mountHarness({
      items: [1, 2, 3],
      // 卡 1 已整张滚出视口（bottom <= 0），锚点应是视口顶部那张卡 2（offset 20）。
      tops: { 1: -280, 2: 20, 3: 400 },
    })

    anchor.run(trigger)
    expect(trigger).toHaveBeenCalledTimes(1)
    expect(scrollByMock).not.toHaveBeenCalled()

    // 尾部追加一页：首条仍是 1 → watcher 不该触发
    items.value = [1, 2, 3, 4]
    await settle()

    expect(trigger).toHaveBeenCalledTimes(1)
    expect(scrollByMock).not.toHaveBeenCalled()
  })

  /**
   * 契约二（核心）：**头部真的被裁了，就把锚点卡片放回原来的视口偏移**。
   *
   * 真实故障：裁掉头部 30 张，整份列表上移约 2500px，用户正在读的那张瞬间
   * 跳出视口（「往下滚一页，阅读位置就没了」）。期望值是可算的：锚点卡 2
   * 原偏移 20px，重排后 -2480px，差值 -2500 → 正好 `scrollBy(0, -2500)`。
   *
   * 这条同时钉住**两跳 nextTick 的顺序**。实测过：把两跳改成跳（合并成一次
   * `await nextTick()`）后本用例必然失败——差一顿时瀑布流还没把新坐标写下去，
   * 量到的仍是旧位置，差值 0 → 悄悄不补偿，用户照样跳走。
   */
  it('首条 id 变了 → 两跳 nextTick 后按锚点差值补偿一次（精确到像素）', async () => {
    const { items, trigger, planLayout, anchor } = mountHarness({
      items: [1, 2, 3],
      tops: { 1: -280, 2: 20, 3: 400 },
    })

    anchor.run(trigger)
    expect(trigger).toHaveBeenCalledTimes(1)

    // 模拟「裁掉头部 + 追加一页、整份列表上移 2500px」：长度不变（3 → 3），
    // 只有首条 id 1 → 2 变了 —— 正是长度 watcher 看不见的那种变更。
    planLayout({ 2: 20 - TRIM_SHIFT_PX, 3: 400 - TRIM_SHIFT_PX, 4: 1200 - TRIM_SHIFT_PX })
    items.value = [2, 3, 4]

    // 补偿不能同步发生：此刻 DOM 还没打补丁、瀑布流还没落位。
    expect(scrollByMock).not.toHaveBeenCalled()

    await settle()

    expect(scrollByMock).toHaveBeenCalledTimes(1)
    expect(scrollByMock).toHaveBeenCalledWith(0, -TRIM_SHIFT_PX)
  })

  /**
   * 契约三：**上一次的锚点作废**。
   *
   * 真实故障：连点两次「加载更多」（或加载中点了一次）时，两个 watcher 同时
   * 活着；旧锚点是按用户上一次滚动位置抓的，数据一到它先落地，跟新锚点抢
   * 滚动位置——表现是「补偿完又跳一下」。只有最新一次抓的锚点算数。
   */
  it('连续两次 run() → 前一个 watcher 被取消，只补偿一次', async () => {
    const { items, trigger, planLayout, anchor } = mountHarness({
      items: [1, 2, 3],
      tops: { 1: -280, 2: 20, 3: 400 },
    })

    anchor.run(trigger)
    anchor.run(trigger)
    expect(trigger).toHaveBeenCalledTimes(2)

    planLayout({ 2: 20 - TRIM_SHIFT_PX, 3: 400 - TRIM_SHIFT_PX, 4: 1200 - TRIM_SHIFT_PX })
    items.value = [2, 3, 4]
    await settle()

    // 两个 watcher 都活下来的话这里会是 2 次。
    expect(scrollByMock).toHaveBeenCalledTimes(1)
    expect(scrollByMock).toHaveBeenCalledWith(0, -TRIM_SHIFT_PX)
  })

  /**
   * 契约四：**容器还没渲染时的快速通道**。
   *
   * 首帧 / 空列表时 `masonryContainerRef` 是 null：没有锚点可抓，但加载本身
   * 必须照常放行（否则用户点「加载更多」毫无反应）。不许抛，也不许挂 watcher。
   */
  it('容器为 null → 照样触发加载，不装 watcher、不抛错', async () => {
    const { items, trigger, anchor } = mountHarness({
      items: [1, 2, 3],
      tops: { 1: -280, 2: 20, 3: 400 },
      withContainer: false,
    })

    expect(() => anchor.run(trigger)).not.toThrow()
    expect(trigger).toHaveBeenCalledTimes(1)

    // 头变了也没有 watcher 在等 → 不该有补偿，更不该抛
    items.value = [2, 3, 4]
    await settle()

    expect(scrollByMock).not.toHaveBeenCalled()
  })

  /**
   * 契约五：**卸载即撤掉等待中的补偿**。
   *
   * 真实故障：load-more 还没落地就切走路由（或列表被 v-if 撤掉），两跳之后
   * 的 `restoreScrollAnchor` 会对着已经不在的列表调 `window.scrollBy`，
   * 把用户在新页面上的滚动位置拽走。卸载必须停表。
   *
   * 断言落在「`restoreScrollAnchor` 有没有被调用」而不是「`scrollBy` 有没有
   * 被调用」：卸载会把容器 DOM 一起带走，就算补偿逻辑还在跑，`restoreScrollAnchor`
   * 也会因为找不到容器而自己返回 false —— 那样即使删掉 `onBeforeUnmount` 这条
   * 用例也照样绿。钉住调用本身，才真的钉住「卸载停表」。
   */
  it('卸载时还有 watcher 在等 → 之后头变了也不再尝试补偿', async () => {
    const restoreSpy = vi.spyOn(scrollAnchorModule, 'restoreScrollAnchor')
    const { wrapper, items, trigger, planLayout, anchor } = mountHarness({
      items: [1, 2, 3],
      tops: { 1: -280, 2: 20, 3: 400 },
    })

    anchor.run(trigger)
    expect(trigger).toHaveBeenCalledTimes(1)

    wrapper.unmount()

    planLayout({ 2: 20 - TRIM_SHIFT_PX })
    items.value = [2, 3, 4]
    await settle()

    // 真正的契约：卸载后**压根不去试**补偿（而不是"试了但失败"）。
    expect(restoreSpy).not.toHaveBeenCalled()
    expect(scrollByMock).not.toHaveBeenCalled()
  })

  /**
   * 契约五（对照）：组件还活着时同样的数据变更**确实**会尝试补偿。
   *
   * 没有这一条，上面那条 `restoreScrollAnchor` 没被调用可能只是「这条路上
   * 本来就不调它」——比如 watcher 根本没触发。两者一起才说明：是卸载砍掉了
   * 那次调用。
   */
  it('对照组：未卸载的同一场景会调用补偿（说明上一条确实是被卸载砍掉的）', async () => {
    const restoreSpy = vi.spyOn(scrollAnchorModule, 'restoreScrollAnchor')
    const { items, trigger, planLayout, anchor } = mountHarness({
      items: [1, 2, 3],
      tops: { 1: -280, 2: 20, 3: 400 },
    })

    anchor.run(trigger)

    planLayout({ 2: 20 - TRIM_SHIFT_PX, 3: 400 - TRIM_SHIFT_PX, 4: 1200 - TRIM_SHIFT_PX })
    items.value = [2, 3, 4]
    await settle()

    expect(restoreSpy).toHaveBeenCalledTimes(1)
    expect(scrollByMock).toHaveBeenCalledTimes(1)
  })

  /**
   * 契约六：**抓不到锚点时不补偿**。
   *
   * 列表空着（或卡片还没有 `data-anchor-id`）时 `captureScrollAnchor` 返回
   * null：没有「用户正在读的那张」可锚，宁可不补偿也不猜一个位置跳过去。
   */
  it('容器里没有锚点卡片（空列表）→ 头变了也不 scrollBy、不抛错', async () => {
    const { items, trigger, anchor } = mountHarness({ items: [], tops: {} })

    anchor.run(trigger)
    expect(trigger).toHaveBeenCalledTimes(1)

    // 首条 id：undefined → 1，watcher 会触发；但锚点快照是 null。
    items.value = [1]
    await settle()

    expect(scrollByMock).not.toHaveBeenCalled()
  })

  /**
   * 契约七：**锚点卡片自己也被裁掉时宁可不补偿**。
   *
   * 裁掉不止一页（或加载期间用户又翻了几页）时，作为锚点的那张卡可能已经不在
   * DOM 里；这时补一个「差值」等于凭空跳转。`restoreScrollAnchor` 选择什么都
   * 不做——这是刻意的：不补偿最多跳一下，乱补偿会跳到完全无关的位置。
   */
  it('锚点卡片被裁掉（不在新列表里）→ 不 scrollBy', async () => {
    const { items, trigger, planLayout, anchor } = mountHarness({
      items: [1, 2, 3],
      tops: { 1: -280, 2: 20, 3: 400 },
    })

    anchor.run(trigger)

    // 裁掉头部 2 张：锚点卡 2 一起没了，首条 id 1 → 3。
    planLayout({ 3: 400 - TRIM_SHIFT_PX, 4: 1200 - TRIM_SHIFT_PX, 5: 1700 - TRIM_SHIFT_PX })
    items.value = [3, 4, 5]
    await settle()

    expect(scrollByMock).not.toHaveBeenCalled()
  })

  /**
   * 契约八：**`cancel()` 真的能单独撤掉一次等待中的补偿**（组件还活着的时候）。
   *
   * 契约五测的是「卸载后不再动滚动」，但组件卸载时 Vue 自己也会回收 setup 里
   * 建的 watcher——那条用例即使去掉内部的 `onBeforeUnmount` 也照样通过。这里
   * 在**没有卸载**的组件上显式 `cancel()`：只有真正的取消逻辑在，后面的头变化
   * 才不会补偿。这也是「上一个锚点作废」那条契约的单元化形式。
   */
  it('显式 cancel() 后头再变 → 不 scrollBy（取消不是空转）', async () => {
    const { items, trigger, planLayout, anchor } = mountHarness({
      items: [1, 2, 3],
      tops: { 1: -280, 2: 20, 3: 400 },
    })

    anchor.run(trigger)
    expect(trigger).toHaveBeenCalledTimes(1)

    anchor.cancel()

    planLayout({ 2: 20 - TRIM_SHIFT_PX, 3: 400 - TRIM_SHIFT_PX, 4: 1200 - TRIM_SHIFT_PX })
    items.value = [2, 3, 4]

    expect(scrollByMock).not.toHaveBeenCalled()
  })
  /**
   * 契约九：**接线 `layoutRevision` 时，补偿由「布局完成信号」驱动**。
   *
   * 这是本轮修掉的那处粗糙设计。旧实现靠 `await nextTick(); await nextTick()`
   * 赌「第二跳恰好落在瀑布流写位置之后」；而 `layoutItems` 是 fire-and-forget、
   * 内部还有串行闸门，位置何时落位对调用方**完全不可观测**。赌输时
   * `restoreScrollAnchor` 量到 delta≈0 会**静默返回 false**，用户被顶走约 2900px
   * 且没有任何报错、没有日志。
   *
   * 怎么证明「它真的在等信号」而不是碰巧先跑了：**信号落后于两跳**时仍然补对。
   * 这里让模拟瀑布流拖到第 5 跳才写坐标并递增信号——如果实现还在数两跳，
   * 它会量到旧坐标（delta≈0）而静默失手；只有真正等信号才会补出 -TRIM。
   */
  it('接线 layoutRevision：布局信号晚于两跳到达，补偿依然量到真实位移', async () => {
    const { items, trigger, anchor, planLayout, layoutRevision } = mountHarness({
      items: [1, 2, 3],
      tops: { 1: -280, 2: 20, 3: 400 },
      withRevision: true,
      layoutDelayTicks: 5,
    })

    anchor.run(trigger)
    // 先预约新坐标，**再**改数据：模拟瀑布流会在头变化后延迟写它。
    planLayout({ 2: 20 - TRIM_SHIFT_PX, 3: 400 - TRIM_SHIFT_PX, 4: 1200 - TRIM_SHIFT_PX })
    items.value = [2, 3, 4]

    // 走完 5 跳布局 + 补偿 flush。
    for (let i = 0; i < 14; i++) {
      await nextTick()
      await flushPromises()
    }

    // 锚点卡 2：原来 top=+20，落位后 20-TRIM → scrollBy 补 -TRIM。
    // 关键：若实现数两跳，此刻布局还没写坐标，delta=0，这里会是 0 次。
    expect(layoutRevision.value).toBeGreaterThan(0)
    expect(scrollByMock).toHaveBeenCalledTimes(1)
    expect(scrollByMock).toHaveBeenCalledWith(0, -TRIM_SHIFT_PX)
  })
})
