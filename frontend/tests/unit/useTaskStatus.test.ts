import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  useTaskStatus,
  __resetTaskStatusForTests,
} from '../../src/composables/useTaskStatus'
import type { TaskHistory } from '../../src/types'

const getTaskHistoryMock = vi.hoisted(() => vi.fn())

vi.mock('../../src/api', () => ({
  taskApi: { getTaskHistory: getTaskHistoryMock },
}))

const POLL_MS = 5000

function historyItem(overrides: Partial<TaskHistory> = {}): TaskHistory {
  return {
    id: 1,
    name: 'display label',
    status: 'pending',
    start_time: '2026-01-01T00:00:00+00:00',
    ...overrides,
  } as TaskHistory
}

function historyPage(items: TaskHistory[]) {
  return { items, total: items.length }
}

/** Flush microtasks without advancing the 5s interval. */
const flushMicrotasks = () => vi.advanceTimersByTimeAsync(0)

describe('useTaskStatus (global task-status singleton)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    __resetTaskStatusForTests()
    getTaskHistoryMock.mockReset()
    getTaskHistoryMock.mockResolvedValue(historyPage([]))
  })

  afterEach(() => {
    __resetTaskStatusForTests()
    vi.useRealTimers()
  })

  it('notifyTaskEnqueued marks the function busy immediately and starts the interval', async () => {
    getTaskHistoryMock.mockResolvedValue(
      historyPage([historyItem({ task_func: 'batch_operation', status: 'pending' })]),
    )
    const status = useTaskStatus()

    status.notifyTaskEnqueued('batch_operation')

    // No round-trip needed: the button grays out synchronously.
    expect(status.isBusy('batch_operation')).toBe(true)
    expect(status.busyFuncNames.value).toEqual(['batch_operation'])
    expect(vi.getTimerCount()).toBe(1)

    await flushMicrotasks()

    // The follow-up refresh agrees with the optimistic state.
    expect(getTaskHistoryMock).toHaveBeenCalledWith(100, 0)
    expect(status.isBusy('batch_operation')).toBe(true)
    expect(vi.getTimerCount()).toBe(1)
  })

  it('refreshTaskStatus tracks pending/running task_func rows and clears on terminal states', async () => {
    getTaskHistoryMock.mockResolvedValue(historyPage([
      historyItem({ id: 1, task_func: 'batch_operation', status: 'pending' }),
      historyItem({ id: 2, task_func: 'batch_export', status: 'running' }),
      // Terminal rows do not count.
      historyItem({ id: 3, task_func: 'failed_retry', status: 'success' }),
      // A legacy pending row without task_func cannot be attributed to a function.
      historyItem({ id: 4, task_func: null, status: 'pending' }),
    ]))
    const status = useTaskStatus()

    await status.refreshTaskStatus()

    expect([...status.busyFuncs.value].sort()).toEqual([
      'batch_export',
      'batch_operation',
    ])
    expect(status.busyFuncNames.value.sort()).toEqual([
      'batch_export',
      'batch_operation',
    ])
    expect(status.isBusy('failed_retry')).toBe(false)
    expect(vi.getTimerCount()).toBe(1)

    // Same value → same ref (no pointless re-render for watchers).
    const stable = status.busyFuncs.value
    await status.refreshTaskStatus()
    expect(status.busyFuncs.value).toBe(stable)

    // Everything reached a terminal state → busy set empties, interval stops.
    getTaskHistoryMock.mockResolvedValue(historyPage([
      historyItem({ id: 1, task_func: 'batch_operation', status: 'success' }),
      historyItem({ id: 2, task_func: 'batch_export', status: 'failed' }),
    ]))

    await status.refreshTaskStatus()

    expect(status.busyFuncs.value.size).toBe(0)
    expect(status.busyFuncNames.value).toEqual([])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('a failed history request neither throws nor clears the known set', async () => {
    getTaskHistoryMock.mockResolvedValue(
      historyPage([historyItem({ task_func: 'failed_retry', status: 'running' })]),
    )
    const status = useTaskStatus()
    await status.refreshTaskStatus()
    const known = status.busyFuncs.value
    expect(status.isBusy('failed_retry')).toBe(true)

    getTaskHistoryMock.mockRejectedValue(new Error('network down'))

    await expect(status.refreshTaskStatus()).resolves.toBeUndefined()

    expect(status.isBusy('failed_retry')).toBe(true)
    expect(status.busyFuncs.value).toBe(known)
    // The interval survives a transient failure so the next tick can recover.
    expect(vi.getTimerCount()).toBe(1)
  })

  it('tick advances after each successful poll and stalls on a failed one', async () => {
    const status = useTaskStatus()
    expect(status.tick.value).toBe(0)

    await status.refreshTaskStatus()
    expect(status.tick.value).toBe(1)

    await status.refreshTaskStatus()
    expect(status.tick.value).toBe(2)

    getTaskHistoryMock.mockRejectedValue(new Error('network down'))
    await status.refreshTaskStatus()
    // A failed poll must not drive view refreshes with stale data.
    expect(status.tick.value).toBe(2)
  })

  it('startTaskStatusPolling does exactly one initial fetch and stays idle', async () => {
    const status = useTaskStatus()

    status.startTaskStatusPolling()
    await flushMicrotasks()

    expect(getTaskHistoryMock).toHaveBeenCalledTimes(1)
    // Nothing active → no interval exists yet.
    expect(vi.getTimerCount()).toBe(0)

    await vi.advanceTimersByTimeAsync(POLL_MS * 2)
    expect(getTaskHistoryMock).toHaveBeenCalledTimes(1)
  })

  it('a poll already in flight cannot erase an enqueue that happened meanwhile', async () => {
    let resolveFirst!: (page: { items: TaskHistory[]; total: number }) => void
    getTaskHistoryMock
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValue(
        historyPage([historyItem({ task_func: 'batch_operation', status: 'pending' })]),
      )
    const status = useTaskStatus()

    status.startTaskStatusPolling()
    expect(getTaskHistoryMock).toHaveBeenCalledTimes(1)

    // Enqueue lands while the stale snapshot request is still open.
    status.notifyTaskEnqueued('batch_operation')
    expect(status.isBusy('batch_operation')).toBe(true)

    resolveFirst(historyPage([]))
    await flushMicrotasks()
    await flushMicrotasks()

    // The coalesced follow-up re-reads the row instead of leaving the optimistic
    // entry cleared by the stale response.
    expect(getTaskHistoryMock).toHaveBeenCalledTimes(2)
    expect(status.isBusy('batch_operation')).toBe(true)
    expect(vi.getTimerCount()).toBe(1)
  })

  it('polls once per interval only while busy, then stops polling when idle', async () => {
    getTaskHistoryMock.mockResolvedValue(
      historyPage([historyItem({ task_func: 'batch_operation', status: 'pending' })]),
    )
    const status = useTaskStatus()
    await status.refreshTaskStatus()
    expect(getTaskHistoryMock).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(1)

    // Busy: exactly one poll per 5s tick.
    await vi.advanceTimersByTimeAsync(POLL_MS)
    await flushMicrotasks()
    expect(getTaskHistoryMock).toHaveBeenCalledTimes(2)

    // The next poll observes a terminal row → set clears and the timer dies.
    getTaskHistoryMock.mockResolvedValue(
      historyPage([historyItem({ task_func: 'batch_operation', status: 'success' })]),
    )
    await vi.advanceTimersByTimeAsync(POLL_MS)
    await flushMicrotasks()
    expect(getTaskHistoryMock).toHaveBeenCalledTimes(3)
    expect(status.busyFuncs.value.size).toBe(0)
    expect(vi.getTimerCount()).toBe(0)

    // Idle: advancing further must not produce any request.
    await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    expect(getTaskHistoryMock).toHaveBeenCalledTimes(3)
  })
})
