import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  downloadBlob,
  downloadUrl,
  filenameFromContentDisposition,
  formatNumber,
} from '../utils'

describe('formatNumber', () => {
  it('formats undefined, zero, below 10k, and 10k+ values', () => {
    expect(formatNumber(undefined)).toBe('0')
    expect(formatNumber(0)).toBe('0')
    expect(formatNumber(42)).toBe('42')
    expect(formatNumber(9999)).toBe('9999')
    expect(formatNumber(10000)).toBe('1.0w')
    expect(formatNumber(12345)).toBe('1.2w')
  })
})

describe('filenameFromContentDisposition', () => {
  it('returns fallback when header is absent or unmatched', () => {
    expect(filenameFromContentDisposition(undefined, 'fallback.txt')).toBe('fallback.txt')
    expect(filenameFromContentDisposition('', 'fallback.txt')).toBe('fallback.txt')
    expect(filenameFromContentDisposition('attachment', 'fallback.txt')).toBe('fallback.txt')
  })

  it('decodes RFC 6266 UTF-8 filename* parameter', () => {
    const header = "attachment; filename=\"ascii.epub\"; filename*=UTF-8''%E5%B0%8F%E8%AF%B4.epub"
    expect(filenameFromContentDisposition(header, 'fallback.epub')).toBe('小说.epub')
  })

  it('falls back to plain filename when UTF-8 filename* is malformed', () => {
    const header = "attachment; filename*=UTF-8''%E0%A4%A; filename=\"plain.epub\""
    expect(filenameFromContentDisposition(header, 'fallback.epub')).toBe('plain.epub')
  })

  it('parses quoted and unquoted plain filename parameters', () => {
    expect(filenameFromContentDisposition('attachment; filename="book.txt"', 'f.txt')).toBe('book.txt')
    expect(filenameFromContentDisposition('attachment; filename=a.txt; size=12', 'f.txt')).toBe('a.txt')
  })
})

describe('downloadBlob & downloadUrl', () => {
  const originalCreateObjectURL = URL.createObjectURL
  const originalRevokeObjectURL = URL.revokeObjectURL

  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    URL.createObjectURL = originalCreateObjectURL
    URL.revokeObjectURL = originalRevokeObjectURL
  })

  it('downloadBlob creates an object URL, clicks an anchor, and revokes after 1s', () => {
    const createSpy = vi.fn(() => 'blob:mock-123')
    const revokeSpy = vi.fn()
    URL.createObjectURL = createSpy
    URL.revokeObjectURL = revokeSpy

    let clickedHref = ''
    let clickedDownload = ''
    let attachedDuringClick = false

    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        clickedHref = this.href
        clickedDownload = this.download
        attachedDuringClick = document.body.contains(this)
      })

    const blob = new Blob(['hello'], { type: 'text/plain' })
    downloadBlob(blob, 'story.txt')

    expect(createSpy).toHaveBeenCalledWith(blob)
    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(attachedDuringClick).toBe(true)
    expect(clickedHref).toBe('blob:mock-123')
    expect(clickedDownload).toBe('story.txt')
    expect(document.body.querySelectorAll('a')).toHaveLength(0)

    expect(revokeSpy).not.toHaveBeenCalled()
    vi.advanceTimersByTime(999)
    expect(revokeSpy).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(revokeSpy).toHaveBeenCalledWith('blob:mock-123')
  })

  it('downloadUrl clicks a new-tab anchor with noopener and cleans up DOM', () => {
    let clickedHref = ''
    let clickedTarget = ''
    let clickedRel = ''
    let attachedDuringClick = false

    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        clickedHref = this.href
        clickedTarget = this.target
        clickedRel = this.rel
        attachedDuringClick = document.body.contains(this)
      })

    downloadUrl('/api/novels/1/download?format=epub')

    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(attachedDuringClick).toBe(true)
    expect(clickedHref).toContain('/api/novels/1/download?format=epub')
    expect(clickedTarget).toBe('_blank')
    expect(clickedRel).toBe('noopener')
    expect(document.body.querySelectorAll('a')).toHaveLength(0)
  })
})
