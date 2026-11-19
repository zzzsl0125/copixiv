interface ErrorResponseBody {
  detail?: unknown
}

interface ValidationIssue {
  msg?: unknown
}

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, body: unknown, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

/**
 * Extract FastAPI error detail from ``{"detail": "..."}`` or
 * ``{"detail": [{"msg": "..."}, ...]}`` (422 validation errors).
 */
export function extractFastApiDetail(body: unknown): string | null {
  if (!body || typeof body !== 'object') return null
  const detail = (body as ErrorResponseBody).detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const issue = detail.find(
      (item): item is ValidationIssue => Boolean(item && typeof item === 'object' && item.msg),
    )
    if (issue && typeof issue.msg === 'string' && issue.msg.trim()) return issue.msg
  }
  return null
}

/** Detect native fetch / XHR abort cancellations. */
export function isAbortError(err: unknown): boolean {
  if (err instanceof DOMException && err.name === 'AbortError') return true
  return (
    err !== null &&
    typeof err === 'object' &&
    'name' in err &&
    (err as { name?: unknown }).name === 'AbortError'
  )
}

/** Extract a human-readable message from an ``ApiError`` or generic ``Error``. */
export function getApiErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    return extractFastApiDetail(err.body) || err.message || fallback
  }
  if (err instanceof Error && err.message.trim()) return err.message
  return fallback
}
