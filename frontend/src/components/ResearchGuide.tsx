import { Link } from 'react-router-dom'
import { ArrowUpRight } from 'lucide-react'

export function ResearchGuide({ page: _page }: { page: 'home' }) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-4 border-b border-border pb-5">
      <div><p className="mb-1.5 text-[10px] font-medium tracking-[0.16em] text-muted">WORKSPACE</p><h1 className="text-2xl font-semibold tracking-tight">研究概览</h1></div>
      <Link to="/screener" className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-xs font-medium text-white transition-opacity hover:opacity-90">探索策略<ArrowUpRight className="h-3.5 w-3.5" /></Link>
    </header>
  )
}
