import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Relative base so the built assets work when served by the Python webserver
// from any path. In dev, proxy the JSON/SSE API to the Python backend.
export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
      },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
