import axios from 'axios'

export const apiClient = axios.create({
  baseURL: '/api',
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
