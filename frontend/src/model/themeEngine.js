// Theme engine: turn the admin's colour/font/CSS settings into the CSS
// variables the design tokens use (see styles/tokens.css). Kept free of
// Vue imports so it can run before the app mounts (no flash of the
// default palette) and in tests.
//
// Only literal values reach this module: the server validates colours
// and the font list. Custom CSS is assigned as a text node, never as
// HTML.

export const DEFAULT_THEME = {
  text_color: '',
  primary_color: '',
  secondary_color: '',
  background_color: '',
  font_family: '',
  custom_css: '',
}

const CUSTOM_STYLE_ID = 'downtify-custom-css'

//: Variables this module owns. Everything is removed before a re-apply so
//: clearing a field restores the stylesheet's own value.
const OWNED_VARIABLES = [
  '--c-text',
  '--c-text-2',
  '--c-text-3',
  '--c-muted',
  '--c-faint',
  '--c-accent',
  '--c-accent-hi',
  '--c-on-accent',
  '--c-bg',
  '--c-side',
  '--c-surface',
  '--c-surface-2',
  '--c-raised',
  '--c-line',
  '--c-line-2',
  '--c-line-3',
  '--c-glass',
  '--font-sans',
  '--font-display',
]

function clean(value) {
  return typeof value === 'string' ? value.trim() : ''
}

function parseRgb(value) {
  const text = clean(value)
  const hex = text.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i)
  if (hex) {
    const raw =
      hex[1].length === 3
        ? hex[1]
            .split('')
            .map((part) => part + part)
            .join('')
        : hex[1]
    return [0, 2, 4].map((offset) => parseInt(raw.slice(offset, offset + 2), 16))
  }
  const rgb = text.match(/^rgba?\(([^)]+)\)$/i)
  if (rgb) {
    const parts = rgb[1]
      .split(/[,\s/]+/)
      .filter(Boolean)
      .slice(0, 3)
      .map((part) => (part.endsWith('%') ? (parseFloat(part) * 255) / 100 : parseFloat(part)))
    if (parts.length === 3 && parts.every((part) => Number.isFinite(part))) {
      return parts
    }
  }
  return null
}

/** Readable foreground for a filled accent button. */
function onAccentFor(color) {
  const rgb = parseRgb(color)
  if (rgb === null) return ''
  const [r, g, b] = rgb.map((channel) => {
    const value = channel / 255
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  })
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
  return luminance > 0.55 ? '#0b0c0e' : '#ffffff'
}

function applyCustomCss(css) {
  const text = clean(css)
  let style = document.getElementById(CUSTOM_STYLE_ID)
  if (!text) {
    style?.remove()
    return
  }
  if (!style) {
    style = document.createElement('style')
    style.id = CUSTOM_STYLE_ID
    document.head.appendChild(style)
  }
  style.textContent = text
}

export function applyTheme(theme = {}) {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  for (const variable of OWNED_VARIABLES) {
    root.style.removeProperty(variable)
  }
  for (const [variable, value] of Object.entries(themeVariables(theme))) {
    root.style.setProperty(variable, value)
  }
  applyCustomCss(theme.custom_css)
}

/** CSS variables a theme resolves to (pure; used by applyTheme/tests). */
export function themeVariables(theme = {}) {
  const text = clean(theme.text_color)
  const primary = clean(theme.primary_color)
  const secondary = clean(theme.secondary_color)
  const background = clean(theme.background_color)
  const font = clean(theme.font_family)
  const values = {}

  if (text) {
    values['--c-text'] = text
    values['--c-text-2'] =
      'color-mix(in srgb, var(--c-text) 86%, var(--c-bg))'
    values['--c-text-3'] =
      'color-mix(in srgb, var(--c-text) 72%, var(--c-bg))'
    values['--c-muted'] =
      'color-mix(in srgb, var(--c-text) 60%, var(--c-bg))'
    values['--c-faint'] =
      'color-mix(in srgb, var(--c-text) 44%, var(--c-bg))'
  }
  if (background) {
    values['--c-bg'] = background
    values['--c-side'] =
      'color-mix(in srgb, var(--c-bg) 97%, var(--c-text))'
    values['--c-surface'] =
      'color-mix(in srgb, var(--c-bg) 94%, var(--c-text))'
    values['--c-surface-2'] =
      'color-mix(in srgb, var(--c-bg) 90%, var(--c-text))'
    values['--c-raised'] =
      'color-mix(in srgb, var(--c-bg) 84%, var(--c-text))'
    values['--c-line'] =
      'color-mix(in srgb, var(--c-bg) 92%, var(--c-text))'
    values['--c-line-2'] =
      'color-mix(in srgb, var(--c-bg) 90%, var(--c-text))'
    values['--c-line-3'] =
      'color-mix(in srgb, var(--c-bg) 86%, var(--c-text))'
    values['--c-glass'] =
      'color-mix(in srgb, var(--c-bg) 86%, transparent)'
  }
  if (primary) {
    values['--c-accent'] = primary
    values['--c-accent-hi'] =
      secondary || 'color-mix(in srgb, var(--c-accent) 82%, white)'
    const onAccent = onAccentFor(primary)
    if (onAccent) values['--c-on-accent'] = onAccent
  } else if (secondary) {
    values['--c-accent-hi'] = secondary
  }
  if (font) {
    values['--font-sans'] = font
    values['--font-display'] = font
  }
  return values
}

export function resetTheme() {
  applyTheme(DEFAULT_THEME)
}
