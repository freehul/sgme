import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发服务：Vite proxy /v1 → SGME Gateway (9910)
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: 'http://127.0.0.1:9910',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})