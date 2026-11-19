import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mountWithQuery } from '../../test-utils/queryWrapper'

const getBusyTasksMock = vi.hoisted(() => vi.fn<() => Promise<{ funcs: string[] }>>())

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    taskApi: {
      ...actual.taskApi,
      getBusyTasks: getBusyTasksMock,
    },
  }
})

import { useTaskStatus } from '../useTaskStatus'

describe('useTaskStatus (Vue Query)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    getBusyTasksMock.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('polls every 5s while tasks are busy and stops when empty', async () => {
    getBusyTasksMock
      .mockResolvedValueOnce({ funcs: ['novel_ranking'] })
      .mockResolvedValueOnce({ funcs: [] })

    const { result, wrapper } = await mountWithQuery(() => useTaskStatus())
    await flushPromises(5)

    expect(result.isBusy('novel_ranking')).toBe(true)
    expect(result.busyFuncNames.value).toEqual(['novel_ranking'])

    // Advance 5s -> second poll finds no busy tasks and disables polling
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises(5)
    expect(result.isBusy('novel_ranking')).toBe(false)

    // No further polling when empty
    await vi.advanceTimersByTimeAsync(15000)
    expect(getBusyTasksMock).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('optimistically marks enqueued task busy and refetches cache', async () => {
    getBusyTasksMock
      .mockResolvedValueOnce({ funcs: [] })
      .mockResolvedValueOnce({ funcs: ['novel_search'] })

    const { result, wrapper } = await mountWithQuery(() => useTaskStatus())
    await flushPromises(5)
    expect(result.isBusy('novel_search')).toBe(false)

    result.notifyTaskEnqueued('novel_search')
    expect(result.isBusy('novel_search')).toBe(true)

    await flushPromises(5)
    expect(getBusyTasksMock).toHaveBeenCalledTimes(2)
    expect(result.isBusy('novel_search')).toBe(true)
    wrapper.unmount()
  })

  it('preserves previous busyFuncs when a background poll fails', async () => {
    getBusyTasksMock
      .mockResolvedValueOnce({ funcs: ['novel_follow'] })
      .mockRejectedValueOnce(new Error('timeout'))
      .mockResolvedValueOnce({ funcs: [] })

    const { result, wrapper } = await mountWithQuery(() => useTaskStatus())
    await flushPromises(5)
    expect(result.isBusy('novel_follow')).toBe(true)

    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises(5)
    expect(result.isBusy('novel_follow')).toBe(true)

    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises(5)
    expect(result.isBusy('novel_follow')).toBe(false)
    wrapper.unmount()
  })
})
