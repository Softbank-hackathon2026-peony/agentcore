import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 개발 중에는 compose의 nginx(8080)를 거쳐 백엔드를 호출한다. 인증 흐름이 운영과 같아진다.
    proxy: { '/api': { target: 'http://localhost:8080' } },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
