// ESLint 配置（前端 src 下的 Vue SFC + 纯 JS）。
// 定位：捕获真错误（未定义变量、明显笔误），样式类规则尽量放宽，避免噪音阻塞 CI。
module.exports = {
  root: true,
  env: { browser: true, es2022: true, node: true },
  parserOptions: { ecmaVersion: 2022, sourceType: 'module' },
  extends: ['eslint:recommended', 'plugin:vue/vue3-essential'],
  rules: {
    // 未使用变量降级为警告（不阻塞 CI），但下划线前缀视为有意忽略
    'no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    'vue/multi-word-component-names': 'off',
    'no-empty': ['warn', { allowEmptyCatch: true }],
  },
  ignorePatterns: ['dist/', 'node_modules/'],
}
