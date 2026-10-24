import { onBeforeUnmount, watch, type Ref } from 'vue'
import { captureScrollAnchor, restoreScrollAnchor } from './useScrollAnchor'

/**
 * 「load-more 之后把用户正在读的那张卡放回原位」这条时序编排的**唯一实现**。
 *
 * 背景：`DEFAULT_MAX_ITEMS` 让 load-more 越过上限时从**头部**裁掉最老的卡片，
 * 把用户正在看的那张顶走（30 张约 2900px）。卡片是绝对定位的，原生 scroll
 * anchoring 对它不生效 —— 这条已由真机实测确认，不再是推断：
 *
 *   · 对照组（文档流内容，从顶部删 2000px）：`scrollY` 自动 -2000、锚点原地不动
 *     → 浏览器**已启用** scroll anchoring；
 *   · 真实列表（裁头 30 + 追加 30，手工补偿被中和）：`scrollY` 纹丝不动、锚点卡
 *     `top` 从 -317 变 -3231（上移 2914px）→ 它对绝对定位**确实不生效**。
 *
 * 所以必须自己补偿「视口顶部第一张卡」（见 `useScrollAnchor`）。
 *
 * 为什么监听「首条 id 变化」而不是「长度变化」：裁剪与追加在同一个同步函数里
 * 完成（push 30 → splice 30），**长度前后都是 300，watcher 根本不会触发**。
 * 首条 id 变了才是「头部真的被裁了」的唯一可靠信号。
 *
 * 抽成组合式函数是为了能独立测：这段代码的 bug 全在**时序**上（何时算锚点、
 * 何时作废、什么时候补偿），在视图里测不到，在 jsdom 里可以。
 */

/** 头部条目的身份：视图传 `() => displayNovels.value[0]?.id`。 */
export type HeadItemIdentity = string | number | null | undefined

export interface UseLoadMoreAnchorOptions {
  /** 网格容器（锚点查询根）：抓锚点与还原补偿都相对它做。 */
  container: Ref<HTMLElement | null>
  /** 头部条目的身份——「头部真的被裁了」的唯一可靠信号。 */
  firstItem: () => HeadItemIdentity
  /**
   * 布局完成计数器（接线时传 `useMasonryLayout` 的 `layoutRevision`）。
   *
   * **补偿必须等它增长，而不是数微任务跳数。** 旧实现是
   * `await nextTick(); await nextTick()`——赌「第二跳恰好落在瀑布流把位置写下去
   * 之后」。但 `layoutItems` 是 fire-and-forget、内部还有串行闸门（一次只跑一个
   * 循环、中途触发被合并），位置何时落位对调用方**完全不可观测**；赌输时
   * `restoreScrollAnchor` 会量到 delta≈0 并**静默返回 false**，于是用户被顶走
   * 约 2900px 而没有任何报错、没有任何日志。
   *
   * 不传则退化为「两跳 nextTick」的旧行为（留给未接线的调用方与部分单测）。
   */
  layoutRevision?: Ref<number>
}

export interface LoadMoreAnchor {
  /** 跑一次 load-more：抓锚点 → 作废上一次的等待 → 触发加载 → 等补偿时机。 */
  run: (trigger: () => void) => void
  /** 撤掉还在等的那次补偿（卸载时用；测试里也用得到）。 */
  cancel: () => void
}

/** 等 `revision` 越过 `baseline`；返回一个能提前放弃的 stop 函数。 */
function whenRevisionAdvances(
  revision: Ref<number>,
  baseline: number,
  onAdvance: () => void,
): () => void {
  let settled = false
  const stopWatch = watch(
    revision,
    () => {
      if (settled || revision.value <= baseline) return
      settled = true
      stopWatch()
      onAdvance()
    },
    { flush: 'post' },
  )
  // 布局可能在本 watcher 建立之前就完成了（例如裁剪发生在同一次 flush 的
  // 更早阶段）：立刻检查一次，不要等一个永远不会再来的变化。
  if (revision.value > baseline) {
    settled = true
    stopWatch()
    onAdvance()
    return () => {}
  }
  return () => { settled = true; stopWatch() }
}

export function useLoadMoreAnchor(options: UseLoadMoreAnchorOptions): LoadMoreAnchor {
  // 不用 ref：这里只是「停表函数」的槽位，没有任何渲染/计算依赖它。
  let stopWatch: (() => void) | null = null

  function cancel() {
    stopWatch?.()
    stopWatch = null
  }

  function run(trigger: () => void) {
    const root = options.container.value
    if (!root) {
      // 容器还没渲染（首帧 / 空列表）：没有锚点可抓，但加载照样要放行。
      trigger()
      return
    }
    const anchor = captureScrollAnchor(root)
    // 「这次加载之前」的布局代数：补偿要等它**涨过这个值**，才敢认为位置落位。
    const baseline = options.layoutRevision?.value
    // 已经有什么在等：上一次的锚点作废（用户可能又滚了，新锚点更准）。
    cancel()

    trigger()

    let restoreStop: (() => void) | null = null
    const restore = () => {
      restoreStop = null
      stopWatch = null
      restoreScrollAnchor(root, anchor)
    }

    const stopHead = watch(
      () => options.firstItem(),
      () => {
        stopHead()
        const revision = options.layoutRevision
        if (!revision || baseline === undefined) {
          // 未接线：退化为旧的两跳 nextTick（保留原注释的语义）。
          //   一跳等 items → DOM 打补丁，一跳等瀑布流把位置算完写下去。
          void Promise.resolve().then(() => Promise.resolve()).then(restore)
          return
        }
        restoreStop = whenRevisionAdvances(revision, baseline, restore)
      },
      { once: true },
    )

    stopWatch = () => { stopHead(); restoreStop?.(); stopWatch = null }
  }

  // 卸载时把还在等的补偿撤掉：否则下一次头变会去动一个已经不在的列表。
  onBeforeUnmount(cancel)

  return { run, cancel }
}
