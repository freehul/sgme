import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发服务：Vite proxy /v1 → SGME Gateway
// 地址优先取环境变量 SGME_HTTP_URL（如 http://192.168.10.10:9910），缺省回退本机开发默认。
const sgmeTarget = (process.env.SGME_HTTP_URL || 'http://localhost:9910').replace(/\/+$/, '')
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: sgmeTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})