import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// Shared /api proxy: dev server and preview server both forward API
// calls to the backend, so the built bundle can be served as-is.
const apiProxy = {
  '/api': {
    target: 'http://127.0.0.1:9000',
    changeOrigin: true,
    // Rewrite absolute redirect URLs (e.g. FastAPI trailing-slash redirects)
    // back to relative paths so the browser stays behind the proxy.
    configure: (proxy) => {
      proxy.on('proxyRes', (proxyRes) => {
        const location = proxyRes.headers['location']
        if (location && location.startsWith('http://127.0.0.1:9000')) {
          proxyRes.headers['location'] = location.replace(
            'http://127.0.0.1:9000',
            '',
          )
        }
      })
    },
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
