interface LogoProps {
  className?: string
  size?: number
  style?: React.CSSProperties
}

export function Logo({ className, size = 32, style }: LogoProps) {
  return (
    <svg
      viewBox="0 0 32 32"
      width={size}
      height={size}
      fill="none"
      className={className}
      style={style}
      role="img"
      aria-label="量化研究台"
    >
      <rect x="2.5" y="2.5" width="27" height="27" rx="6" stroke="currentColor" strokeWidth="2" />
      <path d="M7 22 L13 16 L18 19 L25 10" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="25" cy="10" r="2" fill="currentColor" />
    </svg>
  )
}
