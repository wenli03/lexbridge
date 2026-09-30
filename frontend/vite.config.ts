import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue(), tailwindcss()],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    port: 5173,
    // 开发期把 /api 代理到后端。生产由 nginx 做同样的事（见 nginx.conf）。
    //
    // 开发与生产的代理规则必须一致，否则会出现「本地能跑、部署后 404」
    // 这类只在交付当天暴露的问题。
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
      // SSE 单独配置：默认代理会缓冲响应，导致流式输出变成"憋到最后一次性吐出"。
      // 用正则匹配三条流式端点（咨询结果 / 追问恢复 / 入库进度），
      // 与 nginx.conf 的 location 保持完全一致——两边不一致就会出现
      // "本地开发是流式的、部署后变成一次性输出"这种只在交付当天暴露的问题。
      '^/api/(consult-sessions/.*/(stream|resume)|knowledge/jobs/[^/]+/stream)$': {
        target: 'http://localhost:8080',
        changeOrigin: true,
        // 关闭压缩是 SSE 的必要条件——压缩会把分帧缓冲掉
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('Accept-Encoding', 'identity')
          })
        },
      },
    },
  },

  build: {
    outDir: 'dist',
    // 生产构建不生成 sourcemap：本仓库公开，sourcemap 会把源码结构一起发出去
    sourcemap: false,
    chunkSizeWarningLimit: 800,
  },

  test: {
    environment: 'jsdom',
    globals: true,
  },
})
