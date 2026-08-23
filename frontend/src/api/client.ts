import axios from 'axios'

export const apiBaseUrl = '/api'

export const apiClient = axios.create({
  baseURL: apiBaseUrl,
  timeout: 30_000,
})

// Optional backend API key (config.yaml `security.api_key`). Empty means
// disabled; when configured at build time every proxied /api request must
// carry the header — including blob downloads made through this client.
const apiKey = import.meta.env.VITE_API_KEY as string | undefined
if (apiKey) {
  apiClient.interceptors.request.use((config) => {
    config.headers.set('X-API-Key', apiKey)
    return config
  })
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
