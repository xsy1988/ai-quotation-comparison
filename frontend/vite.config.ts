import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 端口由 port-manager 分配（见 docs/PORT_ALLOCATION_GUIDE.md），通过 frontend/.env 的 VITE_PORT 配置
export default defineConfig({
  plugins: [react()],
  server: {
    // 0.0.0.0：本机与局域网（其他电脑经本机 IP 访问 demo）均可访问
    host: '0.0.0.0',
    port: Number(process.env.VITE_PORT || 8003),
  },
})
