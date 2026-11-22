import createClient, { type Middleware } from 'openapi-fetch'
import type { paths } from '../types/api.generated'
import { ApiError, extractFastApiDetail } from './errors'

const apiBaseUrl = '/api'

// Optional backend API key (config.yaml `security.api_key`). Empty means
// disabled; when configured at build time every proxied /api request carries
// X-API-Key via the middleware below.
const apiKey = (import.meta.env.VITE_API_KEY as string | undefined) || ''

const authMiddleware: Middleware = {
  onRequest({ request }) {
    if (apiKey) {
      request.headers.set('X-API-Key', apiKey)
    }
    return request
  },
}

export const apiClient = createClient<paths>({
  baseUrl: '',
  querySerializer: {
    // FastAPI binds repeated query params (excluded_ids=1&excluded_ids=2) to list[int].
    array: { style: 'form', explode: true },
  },
})

apiClient.use(authMiddleware)

export function unwrap<T>(
  result: { data?: T; error?: unknown; response: Response },
  fallback = '请求失败',
): T {
  if (result.error !== undefined || !result.response.ok || result.data === undefined) {
    throw new ApiError(
      result.response.status,
      result.error,
      extractFastApiDetail(result.error) || fallback,
    )
  }
  return result.data
}

export function unwrapVoid(
  result: { error?: unknown; response: Response },
  fallback = '请求失败',
): void {
  if (result.error !== undefined || !result.response.ok) {
    throw new ApiError(
      result.response.status,
      result.error,
      extractFastApiDetail(result.error) || fallback,
    )
  }
}

/**
 * Build a browser-navigable API URL for a download. When an API key is
 * configured it is appended as `?api_key=` because a plain anchor/window
 * navigation cannot carry the X-API-Key header — this lets us download by
 * navigating directly to the endpoint (blob: object URLs are unsupported in
 * some in-app browsers / WebViews, so the frontend must not rely on them).
 */
export function buildApiUrl(path: string): string {
  if (!apiKey) return `${apiBaseUrl}${path}`
  const sep = path.includes('?') ? '&' : '?'
  return `${apiBaseUrl}${path}${sep}api_key=${encodeURIComponent(apiKey)}`
}

/**
 * XHR helper for multipart uploads that need real byte progress events
 * (`fetch` has no standardized upload progress callback).
 */
export function uploadWithProgress<T>(
  path: string,
  form: FormData,
  options?: { onUploadProgress?: (percent: number) => void; signal?: AbortSignal },
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${apiBaseUrl}${path}`)
    if (apiKey) xhr.setRequestHeader('X-API-Key', apiKey)

    const onAbort = () => xhr.abort()
    options?.signal?.addEventListener('abort', onAbort, { once: true })

    xhr.upload.onprogress = (event) => {
      if (!options?.onUploadProgress || !event.lengthComputable || event.total <= 0) return
      options.onUploadProgress(Math.round((event.loaded / event.total) * 100))
    }
    xhr.onload = () => {
      options?.signal?.removeEventListener('abort', onAbort)
      let parsed: unknown = null
      try {
        parsed = xhr.responseText ? JSON.parse(xhr.responseText) : null
      } catch {
        parsed = xhr.responseText
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(parsed as T)
      } else {
        reject(new ApiError(xhr.status, parsed, extractFastApiDetail(parsed) || '上传失败'))
      }
    }
    xhr.onerror = () => {
      options?.signal?.removeEventListener('abort', onAbort)
      reject(new ApiError(xhr.status || 0, null, '网络错误，上传失败'))
    }
    xhr.onabort = () => {
      options?.signal?.removeEventListener('abort', onAbort)
      reject(new DOMException('Upload aborted', 'AbortError'))
    }
    xhr.send(form)
  })
}
