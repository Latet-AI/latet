import type { SVGProps } from 'react'

// A small hand-drawn icon set (16 px grid, 1.5 px strokes). Decorative unless given a title by the caller.

const PATHS = {
  live: 'M1.5 8h3l2-5 3 10 2-5h3',
  approvals: 'M2 9.5l1.8-6h8.4L14 9.5V13H2zM2 9.5h3.5l1 1.5h3l1-1.5H14M6 6.5l1.5 1.5 2.5-2.5',
  bell: 'M4 11.5V7a4 4 0 0 1 8 0v4.5l1.5 1H2.5zM6.5 14.5h3',
  sessions: 'M3 3.5h10M3 8h10M3 12.5h6',
  stats: 'M3 13V9M8 13V3.5M13 13V6.5',
  vault: 'M3.5 7V5a4.5 4.5 0 0 1 9 0v2M3 7h10v6.5H3zM8 9.5v1.5',
  rules: 'M2.5 4.5h5M10.5 4.5h3M2.5 11.5h2M7.5 11.5h6M9 3v3M6 10v3',
  playground: 'M6 2.5v4L2.8 12a1 1 0 0 0 .9 1.5h8.6a1 1 0 0 0 .9-1.5L10 6.5v-4M5 2.5h6M4.5 10h7',
  integrations: 'M6 2v3M10 2v3M4 5h8v2.5a4 4 0 0 1-8 0zM8 11.5V14',
  check: 'M3 8.5l3 3 7-7',
  x: 'M4 4l8 8M12 4l-8 8',
  plus: 'M8 3v10M3 8h10',
  minus: 'M3 8h10',
  copy: 'M5.5 5.5V3h7.5v7.5h-2.5M3 5.5h7.5V13H3z',
  eye: 'M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8zM8 10a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
  eyeOff: 'M2 2l12 12M6.5 4a6.5 6.5 0 0 1 1.5-.5c4 0 6.5 4.5 6.5 4.5a11 11 0 0 1-1.6 2.1M10.7 11.6A6 6 0 0 1 8 12.5C4 12.5 1.5 8 1.5 8a11 11 0 0 1 2.8-3.2',
  trash: 'M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.5h6.6L12 4',
  chevronLeft: 'M10 3L5 8l5 5',
  chevronRight: 'M6 3l5 5-5 5',
  chevronDown: 'M3 6l5 5 5-5',
  search: 'M7 12A5 5 0 1 0 7 2a5 5 0 0 0 0 10zM10.5 10.5L14 14',
  sun: 'M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1',
  moon: 'M13.5 9.5A6 6 0 0 1 6.5 2.5a6 6 0 1 0 7 7z',
  monitor: 'M2 3h12v8H2zM5.5 14h5M8 11v3',
  pause: 'M5.5 3.5v9M10.5 3.5v9',
  play: 'M5 3l8 5-8 5z',
  arrowLeft: 'M13 8H3M7 4L3 8l4 4',
  arrowDown: 'M8 3v10M4 9l4 4 4-4',
  external: 'M9 2.5h4.5V7M13.5 2.5L7 9M11.5 9.5v4h-9v-9h4',
  refresh: 'M13 3v3.5H9.5M3 13V9.5h3.5M12.6 6.5A5 5 0 0 0 3.5 5.5M3.4 9.5a5 5 0 0 0 9.1 1',
  info: 'M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM8 7.5V11M8 5v.01',
  shield: 'M8 1.5l5.5 2v4.2c0 3.2-2.3 5.8-5.5 6.8-3.2-1-5.5-3.6-5.5-6.8V3.5zM5.5 8l2 2 3-3.5',
  ban: 'M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM3.4 3.4l9.2 9.2',
  leak: 'M8 1.8S3.5 7 3.5 10a4.5 4.5 0 0 0 9 0C12.5 7 8 1.8 8 1.8zM8 7v3.5M8 12.3v.01',
  gauge: 'M2 11.5a6 6 0 1 1 12 0M8 11.5l3-4',
  question: 'M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM6.2 6.2A1.9 1.9 0 1 1 8 8.5V9.5M8 11.8v.01',
  filter: 'M2 3h12l-4.5 5.5V13l-3 1.5V8.5z',
  key: 'M10 9.5A3.5 3.5 0 1 0 6.5 6a3.5 3.5 0 0 0 .4 1.6L2 12.5V14h2.5v-1.5H6V11h1.5l1-1a3.5 3.5 0 0 0 1.5-.5zM10.5 5.5v.01',
  terminal: 'M2 3h12v10H2zM4.5 6l2 2-2 2M8 10.5h3.5',
  keyboard: 'M1.5 4h13v8h-13zM4 6.5v.01M6.5 6.5v.01M9 6.5v.01M11.5 6.5v.01M5 9.5h6',
  dot: 'M8 10a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
  download: 'M8 2.5v8M4.5 7L8 10.5 11.5 7M2.5 13.5h11',
  upload: 'M8 10.5v-8M4.5 6L8 2.5 11.5 6M2.5 13.5h11',
  image: 'M2 3h12v10H2zM2 11l3.5-3.5 2.5 2.5 2-2 4 4M10.5 6.5v.01',
  book: 'M8 4.5C6.5 3.4 4.5 3 2 3v9.5c2.5 0 4.5.4 6 1.5 1.5-1.1 3.5-1.5 6-1.5V3c-2.5 0-4.5.4-6 1.5zM8 4.5V14',
} as const

export type IconName = keyof typeof PATHS

interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'name'> {
  name: IconName
  size?: number
  label?: string
}

export function Icon({ name, size = 16, label, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={label ? undefined : true}
      role={label ? 'img' : undefined}
      aria-label={label}
      focusable="false"
      {...rest}
    >
      <path d={PATHS[name]} />
    </svg>
  )
}

/** Filled status glyphs: the mark inside stays legible on any surface. */
export function StatusGlyph({ status, size = 16, label }: { status: 'ok' | 'warn' | 'fail'; size?: number; label?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden={label ? undefined : true}
      role={label ? 'img' : undefined}
      aria-label={label}
      focusable="false"
      className="flex-none"
    >
      {status === 'ok' && (
        <>
          <circle cx="8" cy="8" r="7" fill="var(--good)" />
          <path d="M4.8 8.3l2.1 2.1 4.3-4.6" fill="none" stroke="#fff" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
        </>
      )}
      {status === 'warn' && (
        <>
          <path d="M8 1.4l7 12.4H1z" fill="var(--warn)" strokeLinejoin="round" />
          <path d="M8 6v3.6M8 11.6v.01" stroke="#1a1408" strokeWidth="1.7" strokeLinecap="round" />
        </>
      )}
      {status === 'fail' && (
        <>
          <circle cx="8" cy="8" r="7" fill="var(--crit)" />
          <path d="M5.4 5.4l5.2 5.2M10.6 5.4l-5.2 5.2" stroke="#fff" strokeWidth="1.7" strokeLinecap="round" />
        </>
      )}
    </svg>
  )
}

/** Latet's mark: the dark one in the light theme, the white one in the dark theme (public/logo*.png). */
export function LogoMark({ size = 22, animate = false }: { size?: number; animate?: boolean }) {
  const base = import.meta.env.BASE_URL
  const shown = `flex-none${animate ? ' logo-in' : ''}`
  return (
    <>
      <img src={`${base}logo.png`} width={size} height={size} alt="" aria-hidden="true" className={`${shown} dark:hidden`} />
      <img src={`${base}logo-white.png`} width={size} height={size} alt="" aria-hidden="true" className={`${shown} hidden dark:block`} />
    </>
  )
}
