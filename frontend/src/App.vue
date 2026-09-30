<script setup lang="ts">
/**
 * 根组件。
 *
 * 只做一件事：把路由出口渲染出来。布局的职责交给 layouts/AppLayout.vue——
 * 根组件一旦开始承担布局，登录页（不该有侧边栏）与主应用（该有）就得靠
 * 条件判断挤在同一个模板里，那是布局逻辑开始腐化的起点。
 */
</script>

<template>
  <RouterView />
</template>
