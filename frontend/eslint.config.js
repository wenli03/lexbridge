import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

/**
 * ESLint 扁平配置。
 *
 * 这个文件的核心职责不是代码风格——格式交给 Prettier——而是**把一条架构规则
 * 变成构建失败**：DR-6「前端 api 层是唯一发起网络请求处」。
 *
 * 为什么这条值得单独强制：越过 api 层直接 fetch 的代码能跑、能通过类型检查、
 * 能通过功能测试，只是绕过了认证头注入、traceId 生成、错误归一与 401 统一处置。
 * 它的症状是"某个页面偶尔出问题"，而排查要从几十个调用点里找那一个。
 * 写在文档里的约定挡不住它，只有 lint 挡得住。
 */
export default tseslint.config(
  {
    ignores: ['dist/**', 'node_modules/**', 'coverage/**', '*.config.js'],
  },

  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],

  {
    files: ['**/*.{ts,vue}'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        extraFileExtensions: ['.vue'],
      },
    },
    rules: {
      // ---------------------------------------------------------------------
      // DR-6：网络请求只能从 api 层发出
      // ---------------------------------------------------------------------
      'no-restricted-imports': [
        'error',
        {
          paths: [
            {
              name: 'axios',
              message:
                'DR-6：网络请求只能经 @/api/client 发出。直接使用 axios 会绕过认证头注入、traceId 生成、错误归一与 401 统一处置。',
            },
          ],
          patterns: [
            {
              group: ['@/api/client', '../api/client', '../../api/client'],
              importNames: ['default'],
              message:
                '请使用具名的 api 对象（import { api } from "@/api/client"），不要直接使用默认导出。',
            },
          ],
        },
      ],

      'no-restricted-globals': [
        'error',
        {
          name: 'fetch',
          message: 'DR-6：网络请求只能经 @/api/client 发出，不要直接调用 fetch。',
        },
      ],

      // ---------------------------------------------------------------------
      // 关闭纯格式规则
      // ---------------------------------------------------------------------
      // 这里的取舍值得说明：ESLint 的 Vue 插件默认带了一大批排版规则
      // （属性换行、缩进、自闭合标签、标签内外换行……）。本项目**没有引入 Prettier**，
      // 于是这些规则会产出几十条警告，而它们指向的都是无关紧要的排版差异。
      //
      // 后果不是"代码变好"，而是**开发者开始习惯性地忽略 lint 输出**——
      // 而同一个输出里还混着 DR-6 这类真正重要的架构违规。
      // 噪音淹没信号，比不检查更糟。
      //
      // 因此：ESLint 只管**正确性与架构**，排版交给编辑器与代码评审。
      // 若将来引入 Prettier，这些规则也应当由 Prettier 接管而不是重新打开。
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-newline': 'off',
      'vue/attributes-order': 'off',
      'vue/first-attribute-linebreak': 'off',

      // ---------------------------------------------------------------------
      // 保留的 Vue 规则：这些是正确性问题，不是排版问题
      // ---------------------------------------------------------------------
      'vue/multi-word-component-names': 'off',
      // v-html 是 XSS 入口。本项目当前完全不使用它；
      // 若将来需要，必须同时引入消毒库并在此处留下说明。
      'vue/no-v-html': 'error',
      'vue/no-parsing-error': 'error',
      'vue/no-mutating-props': 'error',
      'vue/require-v-for-key': 'error',
      'vue/no-unused-components': 'error',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // 显式 any 通常意味着类型没想清楚；本项目规模不大，值得强制
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },

  // -------------------------------------------------------------------------
  // api 层是规则的例外：它本来就是唯一该发请求的地方
  // -------------------------------------------------------------------------
  {
    files: ['src/api/**/*.ts'],
    rules: {
      'no-restricted-imports': 'off',
      'no-restricted-globals': 'off',
    },
  },

  // 测试文件放宽
  {
    files: ['**/*.spec.ts', '**/*.test.ts', 'tests/**/*'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
)
