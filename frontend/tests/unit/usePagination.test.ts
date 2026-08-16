import { describe, it, expect, vi } from 'vitest'
import { usePagination } from '../../src/composables/usePagination'

describe('usePagination.refresh (flicker-free polling)', () => {
  it('preserves row identity and patches only changed fields', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce({
        items: [{ id: 1, status: 'running', result: { summary: '打包中 1/10 篇' } }],
        total: 5,
      })
      .mockResolvedValueOnce({
        items: [{ id: 1, status: 'running', result: { summary: '打包中 5/10 篇' } }],
        total: 5,
      })

    const p = usePagination(fetcher)
    await p.loadData()
    const rowBefore = p.items.value[0]

    await p.refresh({ silent: true })

    // Same object identity → Vue performs no row-level re-render.
    expect(p.items.value[0]).toBe(rowBefore)
    // Changed field patched in place.
    expect((rowBefore as { result: { summary: string } }).result.summary)
      .toBe('打包中 5/10 篇')
  })

  it('keeps the same array reference when nothing changed', async () => {
    const row = { id: 1, status: 'running', result: { summary: 'A' } }
    const fetcher = vi.fn().mockResolvedValue({ items: [row], total: 5 })

    const p = usePagination(fetcher)
    await p.loadData()
    const arrBefore = p.items.value

    await p.refresh({ silent: true })

    // Identical content → no assignment at all → zero re-render.
    expect(p.items.value).toBe(arrBefore)
  })
})
