// @vitest-environment jsdom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getTheme, getThemePreference, setTheme, useTheme } from './theme'

let dark = false
let listeners: Set<() => void>
let root: Root | undefined
const container = document.createElement('div')
beforeEach(() => {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value) })
  dark = false
  listeners = new Set()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('matchMedia', () => ({
    get matches() { return dark },
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  }))
})
afterEach(() => {
  if (root) act(() => root?.unmount())
  root = undefined
  vi.unstubAllGlobals()
})
it('defaults to system and preserves an explicit saved choice', () => {
  expect(getThemePreference()).toBe('system')
  dark = true
  expect(getTheme()).toBe('dark')
  setTheme('light')
  expect(getTheme()).toBe('light')
  expect(document.documentElement.classList.contains('dark')).toBe(false)
})
it('updates the page and chart hook when system appearance changes', () => {
  function Probe() { return createElement('span', null, useTheme()) }
  root = createRoot(container)
  act(() => root?.render(createElement(Probe)))
  expect(container.textContent).toBe('light')
  act(() => { dark = true; listeners.forEach(fn => fn()) })
  expect(container.textContent).toBe('dark')
  expect(document.documentElement.classList.contains('dark')).toBe(true)
  act(() => setTheme('light'))
  act(() => { dark = false; listeners.forEach(fn => fn()); dark = true; listeners.forEach(fn => fn()) })
  expect(container.textContent).toBe('light')
  act(() => setTheme('system'))
  expect(container.textContent).toBe('dark')
  act(() => { localStorage.setItem('tf-theme', 'light'); window.dispatchEvent(new Event('storage')) })
  expect(container.textContent).toBe('light')
})
