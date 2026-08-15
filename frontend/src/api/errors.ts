import { AxiosError } from 'axios'

interface ErrorResponse {
  detail?: unknown
}

interface ValidationIssue {
  msg?: unknown
}

/**
 * Extract a human-readable message from an Axios / FastAPI error.
 *
 * FastAPI sends either ``{"detail": "..."}`` (HTTPException / DomainError)
 * or ``{"detail": [{"msg": "..."}, ...]}`` (422 validation errors).
 */
export function getApiErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof AxiosError) {
    const detail = (err.response?.data as ErrorResponse | undefined)?.detail
    if (typeof detail === 'string' && detail.trim()) return detail
    if (Array.isArray(detail)) {
      const issue = detail.find(
        (item): item is ValidationIssue => Boolean(item && typeof item === 'object' && item.msg),
      )
      if (issue && typeof issue.msg === 'string' && issue.msg.trim()) return issue.msg
    }
    return fallback
  }
  if (err instanceof Error) return err.message
  return fallback
}
