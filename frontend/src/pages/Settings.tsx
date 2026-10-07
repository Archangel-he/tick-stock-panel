/**
 * 统一设置页面 — Tab 切换外壳。
 *
 * 通过 URL query param ?tab=xxx 同步 Tab 状态。
 */
import { useSearchParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { BarChart3, Database, Radio, SlidersHorizontal, Sparkles, Settings2, Clock3 } from 'lucide-react'
import { SettingsAIPanel } from './settings/AI'
import { SettingsMonitoringPanel } from './settings/Monitoring'
import { SettingsExtPagesPanel } from './settings/ExtPages'
import { SettingsMenuSettingsPanel } from './settings/MenuSettings'
import { SettingsTimeoutPanel } from './settings/Timeout'
import { SettingsSystemPanel } from './settings/System'
import { SettingsDataSourcesPanel } from './settings/DataSources'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

import type { ComponentType } from 'react'

// ===== Tab 定义 =====

type TabDef = {
  key: string
  label: string
  icon: ComponentType<{ className?: string }>
  panel: ComponentType<{ highlight?: string }>
  badge?: string
}

const TABS: readonly TabDef[] = [
  { key: 'data-sources', label: '数据源',     icon: Database,  panel: SettingsDataSourcesPanel },
  { key: 'ai',         label: 'AI 设置',    icon: Sparkles,  panel: SettingsAIPanel },
  { key: 'monitoring', label: '实时监控',   icon: Radio,     panel: SettingsMonitoringPanel },
  { key: 'ext-pages',  label: '扩展页面',   icon: BarChart3, panel: SettingsExtPagesPanel },
  { key: 'timeout',    label: '网络设置',   icon: Clock3,    panel: SettingsTimeoutPanel },
  { key: 'menus',      label: '菜单设置',   icon: SlidersHorizontal, panel: SettingsMenuSettingsPanel },
  { key: 'system',     label: '系统设置',   icon: Settings2, panel: SettingsSystemPanel },
]

type TabKey = (typeof TABS)[number]['key']

export function Settings() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab') as TabKey | null
  const activeTab = TABS.find((t) => t.key === tabParam) ?? TABS[0]
  const highlight = searchParams.get('highlight') ?? ''

  return (
    <>
      <PageHeader
        title="设置"
        subtitle="数据、连接与工作台偏好"
      />
      <div className="px-5 py-6 md:px-8">
        <div className="mx-auto max-w-6xl">
          <nav aria-label="设置分类" className="mb-6 flex gap-1 overflow-x-auto border-b border-border/70">
            {TABS.map(({ key, label, icon: Icon }) => (
              <button
                key={key}
                type="button"
                aria-current={activeTab.key === key ? 'page' : undefined}
                onClick={() => setSearchParams({ tab: key }, { replace: true })}
                className={cn(
                  'relative -mb-px inline-flex shrink-0 items-center gap-2 border-b-2 px-3 py-3 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50',
                  activeTab.key === key
                    ? 'border-accent text-foreground font-medium'
                    : 'border-transparent text-muted hover:text-foreground hover:border-border',
                )}
              >
                <Icon className="h-4 w-4" />
                {label}
              </button>
            ))}
          </nav>
          <motion.div
            key={activeTab.key}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.15 }}
            className="min-w-0"
          >
            {activeTab.key === 'monitoring'
            ? <SettingsMonitoringPanel highlight={highlight} />
            : <activeTab.panel highlight={highlight} />}
          </motion.div>
        </div>
      </div>
    </>
  )
}
