import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  formatNumber,
  filenameFromContentDisposition,
  downloadBlob,
} from '../../src/lib/utils'

// Search-condition parsing moved to the backend (parse_search_keyword) —
// the frontend sends the raw keyword string.  See tests/domain for the
// parser contract (ordered conditions, no value collisions).

describe('formatNumber', () => {
  it('returns "0" for falsy input', () => {
    expect(formatNumber(0)).toBe('0')
    expect(formatNumber(undefined)).toBe('0')
  })

  it('formats numbers below 10000 as-is', () => {
    expect(formatNumber(9999)).toBe('9999')
  })

  it('formats numbers >= 10000 with w suffix', () => {
    expect(formatNumber(10000)).toBe('1.0w')
    expect(formatNumber(25000)).toBe('2.5w')
  })
})

describe('filenameFromContentDisposition', () => {
  it('decodes filename*=UTF-8 values', () => {
    expect(
      filenameFromContentDisposition(
        "attachment; filename*=UTF-8''%E6%A0%87%E9%A2%98.zip",
        'fallback.zip',
      ),
    ).toBe('标题.zip')
  })

  it('falls back to plain filename', () => {
    expect(filenameFromContentDisposition('attachment; filename="a.zip"', 'fallback')).toBe('a.zip')
  })

  it('returns the fallback for missing or malformed headers', () => {
    expect(filenameFromContentDisposition(undefined, 'fallback.zip')).toBe('fallback.zip')
    expect(filenameFromContentDisposition('', 'fallback.zip')).toBe('fallback.zip')
  })
})

describe('downloadBlob', () => {
  const createObjectURL = vi.fn(() => 'blob:mock-url')
  const revokeObjectURL = vi.fn()

  beforeEach(() => {
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('creates a temporary link and triggers the download', () => {
    const blob = new Blob(['payload'])

    downloadBlob(blob, 'novel.txt')

    expect(createObjectURL).toHaveBeenCalledWith(blob)
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(1)
  })
})
