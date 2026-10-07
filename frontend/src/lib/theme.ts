// User preference and resolved palette are separate: charts consume the resolved theme.
import { useEffect, useState } from 'react'

const KEY = 'tf-theme'
const EVENT = 'tf-theme-change'
const MEDIA = '(prefers-color-scheme: dark)'

export type Theme = 'dark' | 'light'
export type ThemePreference = Theme | 'system'

export function getThemePreference(): ThemePreference {
  try {
    const value = localStorage.getItem(KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch { return 'system' }
}

export function getTheme(): Theme {
  const preference = getThemePreference()
  return preference === 'system' ? (window.matchMedia(MEDIA).matches ? 'dark' : 'light') : preference
}

export function setTheme(preference: ThemePreference) {
  try { localStorage.setItem(KEY, preference) } catch { /* ignore */ }
  document.documentElement.classList.toggle('dark', getTheme() === 'dark')
  window.dispatchEvent(new CustomEvent(EVENT))
}

export function toggleTheme(): Theme {
  const next = getTheme() === 'dark' ? 'light' : 'dark'
  setTheme(next)
  return next
}

export function useThemePreference(): ThemePreference {
  const [preference, set] = useState(getThemePreference)
  useEffect(() => {
    const sync = () => set(getThemePreference())
    window.addEventListener(EVENT, sync)
    window.addEventListener('storage', sync)
    return () => { window.removeEventListener(EVENT, sync); window.removeEventListener('storage', sync) }
  }, [])
  return preference
}

export function useTheme(): Theme {
  const [theme, set] = useState<Theme>(getTheme)
  useEffect(() => {
    const media = window.matchMedia(MEDIA)
    const sync = () => {
      const next = getTheme()
      document.documentElement.classList.toggle('dark', next === 'dark')
      set(next)
    }
    sync()
    media.addEventListener('change', sync)
    window.addEventListener(EVENT, sync)
    window.addEventListener('storage', sync)
    return () => {
      media.removeEventListener('change', sync)
      window.removeEventListener(EVENT, sync)
      window.removeEventListener('storage', sync)
    }
  }, [])
  return theme
}

// ================================================================
// 图表调色板 — ECharts / lightweight-charts 画布不吃 CSS 变量,
// 所有图表组件统一从这里取色, 主题切换时依赖 useTheme 重建 option。
// bull/bear/accent 等语义色双主题一致, 不在此重复定义。
// ================================================================

export interface ChartTheme {
  /** 轴刻度/图例等常规文字 */
  text: string
  /** 信息条/图例里的强调文字 */
  textStrong: string
  /** 网格线 */
  grid: string
  /** 轴线/边框 */
  border: string
  /** 十字光标线 */
  crosshair: string
  /** 十字光标轴标签背景 */
  crosshairLabelBg: string
  /** tooltip 背景 */
  tooltipBg: string
  /** tooltip 边框 */
  tooltipBorder: string
  /** tooltip 文字 */
  tooltipText: string
  /** 半透明信息条背景 (K线图左上角 OHLC 条) */
  infoBarBg: string
  /** dataZoom 滑块填充 */
  zoomFill: string
  /** 分时图均价线以外的弱填充 */
  fillSubtle: string
}

const DARK: ChartTheme = {
  text: '#A1A1AA',
  textStrong: '#E4E4E7',
  grid: 'rgba(255,255,255,0.06)',
  border: '#27272A',
  crosshair: 'rgba(255,255,255,0.25)',
  crosshairLabelBg: '#333',
  tooltipBg: 'rgba(24,24,27,0.95)',
  tooltipBorder: 'rgba(255,255,255,0.1)',
  tooltipText: '#E4E4E7',
  infoBarBg: 'rgba(39,39,42,0.6)',
  zoomFill: 'rgba(255,255,255,0.06)',
  fillSubtle: 'rgba(255,255,255,0.04)',
}

const LIGHT: ChartTheme = {
  text: '#71717A',
  textStrong: '#27272A',
  grid: 'rgba(0,0,0,0.06)',
  border: '#E4E4E7',
  crosshair: 'rgba(0,0,0,0.3)',
  crosshairLabelBg: '#52525B',
  tooltipBg: 'rgba(255,255,255,0.97)',
  tooltipBorder: 'rgba(0,0,0,0.1)',
  tooltipText: '#27272A',
  infoBarBg: 'rgba(244,244,245,0.85)',
  zoomFill: 'rgba(0,0,0,0.06)',
  fillSubtle: 'rgba(0,0,0,0.04)',
}

export function chartTheme(theme: Theme): ChartTheme {
  return theme === 'dark' ? DARK : LIGHT
}

/** hook: 当前主题的图表调色板 (主题切换自动触发重渲染)。 */
export function useChartTheme(): ChartTheme {
  return chartTheme(useTheme())
}
