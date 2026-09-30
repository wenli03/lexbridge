/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

/**
 * 构建期注入的环境变量。
 *
 * 只允许放**非敏感**配置。前端产物是公开可下载的，任何写进这里的值
 * 都等于公开——包括以 VITE_ 开头的"环境变量"。API 密钥永远不进前端，
 * 模型调用全部由后端服务代理。
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string
  readonly VITE_APP_TITLE: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
