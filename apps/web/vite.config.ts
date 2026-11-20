import { loadEnv } from 'vite'
import { configDefaults, defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const service = mode === 'service'
  const prototype = mode === 'prototype'
  const surface = prototype ? 'prototype' : env.VITE_APP_MODE || (service ? 'service' : mode === 'demo' ? 'demo' : '')
  return {
  define: { 'import.meta.env.VITE_APP_MODE': JSON.stringify(surface) },
  plugins: [react()],
  server: {
    host: '127.0.0.1', port: Number(env.BARO_WEB_PORT || (prototype ? 5175 : service ? 5174 : 5173)), strictPort: true,
    proxy: { '/api': { target: env.BARO_API_TARGET || (prototype ? 'http://127.0.0.1:8002' : service ? 'http://127.0.0.1:8001' : 'http://127.0.0.1:8000'), rewrite: (path) => path.replace(/^\/api/, '') } },
  },
  test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'], restoreMocks: true, exclude: [...configDefaults.exclude, 'scripts/booth-server.test.mjs'] },
  }
})
