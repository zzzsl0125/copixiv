import { describe, it, expect } from 'vitest'
import { buildQueries, formatNumber, safeFilename } from '../../src/lib/utils'

describe('buildQueries', () => {
  it('returns empty object for blank input', () => {
    expect(buildQueries('')).toEqual({})
    expect(buildQueries('  ')).toEqual({})
  })

  it('parses simple keyword', () => {
    expect(buildQueries('R-18')).toEqual({ 'R-18': 'keyword' })
  })

  it('parses typed conditions with colons', () => {
    expect(buildQueries('keyword:R-18;author_id:12345;tag:NTR;')).toEqual({
      'R-18': 'keyword',
      '12345': 'author_id',
      'NTR': 'tag',
    })
  })

  it('supports Chinese semicolons as separators', () => {
    expect(buildQueries('is_favourite:true；is_special_follow:true')).toEqual({
      true: 'is_special_follow',
    })
  })
})

describe('formatNumber', () => {
  it('returns "0" for falsy input', () => {
    expect(formatNumber(0)).toBe('0')
    expect(formatNumber(undefined)).toBe('0')
  })

  it('formats numbers below 10000 as-is', () => {
    expect(formatNumber(9999)).toBe('9999')
  })

  it('formats numbers >= 10000 with w suffix', () => {
    expect(formatNumber(10000)).toBe('1.0w')
    expect(formatNumber(25000)).toBe('2.5w')
  })
})

describe('safeFilename', () => {
  it('replaces illegal characters', () => {
    expect(safeFilename('a<b>c:d"e/f\\g|h?i*j')).toBe('a_b_c_d_e_f_g_h_i_j')
  })

  it('returns "untitled" for whitespace-only input', () => {
    expect(safeFilename('   ')).toBe('untitled')
  })
})
