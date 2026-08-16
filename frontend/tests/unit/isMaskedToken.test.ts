import { describe, it, expect } from 'vitest'
import { isMaskedToken } from '../../src/composables/useTokens'

describe('isMaskedToken (token masking contract)', () => {
  it('recognises the bare **** mask', () => {
    expect(isMaskedToken('****')).toBe(true)
  })

  it('recognises **** + last 4 chars', () => {
    expect(isMaskedToken('****abcd')).toBe(true)
    expect(isMaskedToken('****7890')).toBe(true)
  })

  it('rejects truncated or malformed masks', () => {
    expect(isMaskedToken('****ab')).toBe(false)
    expect(isMaskedToken('****abcd1234')).toBe(false)
    expect(isMaskedToken('***abcd')).toBe(false)
  })

  it('rejects raw refresh tokens that merely look short', () => {
    expect(isMaskedToken('abcd')).toBe(false)
    expect(isMaskedToken('abcd1234')).toBe(false)
    expect(isMaskedToken('')).toBe(false)
  })
})
