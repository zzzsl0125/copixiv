/**
 * Search conditions travel to the backend as the raw keyword string
 * (``"keyword:R-18;author_id:12345"``) — the backend's
 * ``parse_search_keyword`` is the single authoritative parser (ordered
 * ``(type, value)`` condition list).  The frontend no longer translates
 * the string into a lossy ``{value: type}`` dict.
 */

/** Format a number: 10000+ displays as x.xw */
export function formatNumber(num?: number): string {
  if (!num) return '0'
  if (num >= 10000) return (num / 10000).toFixed(1) + 'w'
  return num.toString()
}

/**
 * Parse a RFC 6266 `filename*` (UTF-8) or plain `filename` parameter.
 * Falls back to a default name when the header is absent/malformed.
 */
export function filenameFromContentDisposition(
  disposition: string | undefined,
  fallback: string,
): string {
  if (!disposition) return fallback
  const utf8 = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8?.[1]) {
    try {
      return decodeURIComponent(utf8[1])
    } catch {
      // fall through to plain filename
    }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/i)
  return plain?.[1] || fallback
}

/** Trigger a browser download for an in-memory Blob. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  // Delay revocation slightly for Safari; the object URL is cheap.
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/**
 * Trigger a browser download by navigating to a URL that is served with
 * `Content-Disposition: attachment`. Unlike blob: object URLs, a real
 * server URL works in in-app browsers / WebViews that lack blob-download
 * support, and the suggested filename comes from the server's header
 * (e.g. the novel title) instead of a random object-URL id.
 */
export function downloadUrl(url: string): void {
  const a = document.createElement('a')
  a.href = url
  a.target = '_blank'
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}
