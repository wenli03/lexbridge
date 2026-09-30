import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)

// Pinia 必须在 router 之前安装：路由守卫里会读取 auth store，
// 顺序反了会在首个导航时抛 "no active Pinia"
app.use(createPinia())
app.use(router)

// 全局错误兜底。Vue 默认把渲染期异常打到 console 就结束，
// 在生产上表现为"页面白屏但控制台里没人看"。
app.config.errorHandler = (err, _instance, info) => {
  // 此处只记录，不做上报——上报通道在 P4 与 traceId 一并接入
  console.error('[LexBridge] 未捕获的前端异常', { err, info })
}

app.mount('#app')
