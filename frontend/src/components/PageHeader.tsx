import { cn } from '@/lib/cn'

interface Props {
  title: string
  subtitle?: React.ReactNode
  /** 标题右侧、subtitle 之前的额外节点(如状态徽标) */
  titleExtra?: React.ReactNode
  right?: React.ReactNode
  className?: string
}

export function PageHeader({ title, subtitle, titleExtra, right, className }: Props) {
  return (
    <header
      className={cn(
        'px-7 py-5 border-b border-border/70 flex flex-wrap items-center justify-between gap-4 bg-base',
        className,
      )}
    >
      <div className="min-w-0 flex flex-col gap-1.5">
        <h1 className="text-2xl font-semibold tracking-tight whitespace-nowrap">{title}</h1>
        {titleExtra}
        {subtitle && <span className="text-xs text-muted leading-relaxed">{subtitle}</span>}
      </div>
      {right}
    </header>
  )
}
