import { defineConfig, type ProxyOptions } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// Shared /api proxy: dev server and preview server both forward API
// calls to the backend, so the built bundle can be served as-is.
const apiProxy: Record<string, string | ProxyOptions> = {
  '/api': {
    target: 'http://127.0.0.1:9000',
    changeOrigin: true,
  },
}

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    proxy: apiProxy,
  },
  preview: {
    host: '0.0.0.0',
    port: 5173,
    proxy: apiProxy,
  },
})
