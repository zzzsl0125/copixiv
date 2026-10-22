/**
 * 全局任务状态单例（零依赖，不引 Pinia）。
 *
 * A2（去 409）之后，后端不再对同一函数重复入队做去重；前端是唯一的重复
 * 防护：同一 ``task_func`` 已有 ``pending``/``running`` 历史行时，把会再次
 * 入队的按钮置灰。因此需要一份**跨视图共享**的任务函数忙闲表。
 *
 * 轮询策略：只有 ``busyFuncs`` 非空时才存在 5s 定时器（空闲不发请求）；
 * 唯一的空闲请求是 App 挂载时调用的 ``startTaskStatusPolling()``，用来捡起
 * 刷新页面之前就已经在跑的任务。整个应用只有这一个轮询器。
 *
 * 状态是模块级的：所有 ``useTaskStatus()`` 调用共享同一个 Set 与定时器。
 */
import { computed, ref } from 'vue'
import * as apiModule from '../api'
import type { TaskHistory } from '../types'

const POLL_MS = 5000

type TaskHistoryPage = { items: TaskHistory[]; total: number }
type TaskHistoryFetcher = (limit: number, offset: number) => Promise<TaskHistoryPage>

const busyFuncs = ref<Set<string>>(new Set())
const busyFuncNames = computed(() => Array.from(busyFuncs.value))
/**
 * 每次**成功**轮询后 +1。视图可 watch 它，用单例的轮询节拍刷新自己的列表
 * （例如 Tasks 历史页的 progress），无需再起第二个定时器。
 */
const tick = ref(0)
let timer: ReturnType<typeof setInterval> | null = null
let inFlight = false
// A notify/enqueue can land while a request is in flight.  The in-flight
// snapshot predates that row, so silently dropping the call would let a stale
// response clear the optimistic busy entry; coalesce into one follow-up run.
let rerunRequested = false

/**
 * Resolve ``taskApi.getTaskHistory`` lazily through the module namespace.
 *
 * Some tests / staging mocks replace the whole ``../api`` module without a
 * ``taskApi`` export; a namespace property access is simply ``undefined``
 * there (a named import binding could throw on access).
 */
function getHistoryFetcher(): TaskHistoryFetcher | null {
  try {
    const taskApi = (apiModule as unknown as {
      taskApi?: { getTaskHistory?: unknown }
    }).taskApi
    if (!taskApi || typeof taskApi.getTaskHistory !== 'function') return null
    return (taskApi.getTaskHistory as TaskHistoryFetcher).bind(taskApi)
  } catch {
    // A fully mocked api module can even throw while resolving a missing
    // export; treat "no taskApi" the same as "not available yet".
    return null
  }
}

function sameSet(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false
  for (const value of a) {
    if (!b.has(value)) return false
  }
  return true
}

function stopTimer(): void {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

/** 只在确实有活跃任务可轮询时才建立定时器（避免空转）。 */
function ensureTimer(): void {
  if (timer !== null) return
  if (getHistoryFetcher() === null) return
  timer = setInterval(() => {
    void refreshTaskStatus()
  }, POLL_MS)
}

/**
 * 拉一次任务历史并刷新 ``busyFuncs``。
 *
 * 失败的请求既不抛出、也不清空已知状态——否则一次网络抖动会让按钮提前
 * 解禁，失去唯一一道重复入队防护。定时器在失败时保持运行，下一拍恢复。
 */
async function refreshTaskStatus(): Promise<void> {
  if (inFlight) {
    rerunRequested = true
    return
  }
  const fetchHistory = getHistoryFetcher()
  if (fetchHistory === null) return

  inFlight = true
  try {
    const page = await fetchHistory(100, 0)
    const next = new Set<string>()
    for (const item of page.items ?? []) {
      const status = (item.status || '').toLowerCase()
      if (status !== 'pending' && status !== 'running') continue
      if (item.task_func) next.add(item.task_func)
    }
    // 只在集合真正变化时替换 ref，避免每个 5s 周期都触发一轮无意义重渲染。
    if (!sameSet(busyFuncs.value, next)) {
      busyFuncs.value = next
    }
    if (next.size > 0) ensureTimer()
    else stopTimer()
    // Successful poll only: views watching `tick` get the same 5s cadence as
    // the busy flags (a failed poll must not masquerade as fresh data).
    tick.value += 1
  } catch {
    // 失败轮询：吞掉错误，保留 busyFuncs 与定时器。
  } finally {
    inFlight = false
    if (rerunRequested) {
      rerunRequested = false
      void refreshTaskStatus()
    }
  }
}

/**
 * 入队成功后立即标记为忙（按钮无需等待一次往返就能置灰），并保证轮询已
 * 启动；随后再异步校准一次（历史行此刻通常已经存在）。
 */
function notifyTaskEnqueued(funcName: string): void {
  if (funcName && !busyFuncs.value.has(funcName)) {
    const next = new Set(busyFuncs.value)
    next.add(funcName)
    busyFuncs.value = next
  }
  if (funcName) ensureTimer()
  void refreshTaskStatus()
}

/** App 挂载时调一次：捡起刷新页面之前就在 pending/running 的任务。 */
function startTaskStatusPolling(): void {
  void refreshTaskStatus()
}

/** 清掉定时器（卸载 / 测试）。 */
function stopTaskStatusPolling(): void {
  rerunRequested = false
  stopTimer()
}

function isBusy(funcName?: string | null): boolean {
  return !!funcName && busyFuncs.value.has(funcName)
}

/** 仅测试用：把模块级单例恢复到初始状态。 */
export function __resetTaskStatusForTests(): void {
  stopTimer()
  busyFuncs.value = new Set()
  tick.value = 0
  inFlight = false
  rerunRequested = false
}

export function useTaskStatus() {
  return {
    busyFuncs,
    busyFuncNames,
    tick,
    isBusy,
    refreshTaskStatus,
    notifyTaskEnqueued,
    startTaskStatusPolling,
    stopTaskStatusPolling,
  }
}
