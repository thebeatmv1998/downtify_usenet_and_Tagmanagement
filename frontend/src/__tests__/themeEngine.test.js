import { describe, expect, it } from 'vitest'
import { themeVariables } from '../model/themeEngine.js'

describe('theme engine variables', () => {
  it('maps the four basic colours onto the design tokens', () => {
    const vars = themeVariables({
      text_color: '#f0f0f0',
      primary_color: '#ff5500',
      secondary_color: '#ffaa00',
      background_color: '#101010',
    })
    expect(vars['--c-text']).toBe('#f0f0f0')
    expect(vars['--c-accent']).toBe('#ff5500')
    expect(vars['--c-accent-hi']).toBe('#ffaa00')
    expect(vars['--c-bg']).toBe('#101010')
  })

  it('derives readable text and surface shades', () => {
    const vars = themeVariables({
      text_color: '#eeeeee',
      background_color: '#000000',
    })
    expect(vars['--c-muted']).toContain('var(--c-text)')
    expect(vars['--c-surface']).toContain('color-mix')
    expect(vars['--c-line-3']).toContain('var(--c-bg)')
  })

  it('picks a dark foreground on a bright accent', () => {
    expect(themeVariables({ primary_color: '#ffee00' })['--c-on-accent']).toBe(
      '#0b0c0e'
    )
  })

  it('picks a light foreground on a dark accent', () => {
    expect(themeVariables({ primary_color: '#102030' })['--c-on-accent']).toBe(
      '#ffffff'
    )
  })

  it('sets the font on both body and display tokens', () => {
    const vars = themeVariables({ font_family: "'Georgia', serif" })
    expect(vars['--font-sans']).toBe("'Georgia', serif")
    expect(vars['--font-display']).toBe("'Georgia', serif")
  })

  it('derives a hover colour when only a primary colour is given', () => {
    const vars = themeVariables({ primary_color: '#ff0000' })
    expect(vars['--c-accent']).toBe('#ff0000')
    expect(vars['--c-accent-hi']).toContain('color-mix')
  })

  it('keeps a custom secondary colour instead of deriving one', () => {
    const vars = themeVariables({
      primary_color: '#ff0000',
      secondary_color: '#00ff00',
    })
    expect(vars['--c-accent-hi']).toBe('#00ff00')
  })

  it('returns no overrides for an empty theme', () => {
    expect(themeVariables({})).toEqual({})
  })
})
