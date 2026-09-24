/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API runs separately (FastAPI). In development Vite proxies /api to it, so the
// browser only ever talks to one origin and no secrets are compiled into the bundle.
const apiTarget = process.env.VITE_API_PROXY ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  // Bind IPv4 explicitly: Node may otherwise listen only on ::1, which browsers with
  // IPv6 disabled (e.g. LibreWolf's defaults) cannot reach via "localhost".
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: { '/api': { target: apiTarget, changeOrigin: true } },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    proxy: { '/api': { target: apiTarget, changeOrigin: true } },
  },
  build: { target: 'es2022', assetsInlineLimit: 0 },
  test: { environment: 'node', include: ['src/**/*.test.{ts,tsx}'] },
})
