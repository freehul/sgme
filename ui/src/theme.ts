// 主题管理：浅/深双主题，支持跟随系统
// 持久化于 localStorage，初始化时挂在 <html data-theme> 上避免闪烁

import { ref } from 'vue'

const STORAGE_KEY = 'sgme_theme'
type ThemeMode = 'light' | 'dark' | 'system'

function systemTheme(): 'light' | 'dark' {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function resolve(mode: ThemeMode): 'light' | 'dark' {
  return mode === 'system' ? systemTheme() : mode
}

function stored(): ThemeMode {
  const v = localStorage.getItem(STORAGE_KEY)
  return v === 'light' || v === 'dark' || v === 'system' ? v : 'system'
}

export function applyTheme(mode: ThemeMode) {
  const t = resolve(mode)
  document.documentElement.setAttribute('data-theme', t)
  // 供 <meta name="color-scheme"> 与系统控件（滚动条/表单控件）适配
  document.documentElement.style.colorScheme = t
}

// 初始化（在 mount 前调用，避免首帧闪烁）
export function initTheme() {
  const mode = stored()
  applyTheme(mode)
  return mode
}

// 响应式主题状态
export function useTheme() {
  const mode = ref<ThemeMode>(stored())
  const theme = ref<'light' | 'dark'>(resolve(mode.value))

  // 跟随系统的变化
  const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
  mq?.addEventListener?.('change', () => {
    if (mode.value === 'system') {
      theme.value = systemTheme()
      applyTheme(mode.value)
    }
  })

  function set(next: ThemeMode) {
    mode.value = next
    theme.value = resolve(next)
    localStorage.setItem(STORAGE_KEY, next)
    applyTheme(next)
  }

  function toggle() {
    set(theme.value === 'dark' ? 'light' : 'dark')
  }

  return { mode, theme, set, toggle }
}