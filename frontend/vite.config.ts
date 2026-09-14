import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 端口由 port-manager 分配（见 docs/PORT_ALLOCATION_GUIDE.md），通过 frontend/.env 的 VITE_PORT 配置
// 根目录 ./start.sh 会导出 VITE_PORT / VITE_API_PORT / VITE_HOST（Vite 只读 process.env，不读 frontend/.env）
const apiPort = Number(process.env.VITE_API_PORT || 8002)

export default defineConfig({
  plugins: [react()],
  server: {
    // 0.0.0.0：本机与局域网（其他电脑经本机 IP 访问 demo）均可访问；--local 时 start.sh 会改成 127.0.0.1
    host: process.env.VITE_HOST || '0.0.0.0',
    port: Number(process.env.VITE_PORT || 8003),
    // /api 反代到后端：前端一律请求同源相对路径，因此无论从 127.0.0.1、局域网 IP
    // 还是域名访问都能用，不依赖写死 IP，也没有跨域问题
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${apiPort}`,
        // SSE（/api/tasks/:id/progress）不能被压缩缓冲，否则进度会攒着一次性吐出
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('accept-encoding', 'identity')
          })
        },
      },
    },
  },
})
