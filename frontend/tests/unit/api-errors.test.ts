import { describe, it, expect } from 'vitest'
import { AxiosError, AxiosHeaders } from 'axios'
import { getApiErrorMessage } from '../../src/api/errors'

function axiosErrorWith(data: unknown, status = 400): AxiosError {
  const err = new AxiosError(`Request failed with status code ${status}`)
  err.response = {
    data,
    status,
    statusText: '',
    headers: {},
    config: { headers: new AxiosHeaders() },
  }
  return err
}

describe('getApiErrorMessage', () => {
  it('extracts a string detail from FastAPI DomainError bodies', () => {
    const msg = getApiErrorMessage(axiosErrorWith({ detail: '未找到匹配条件的小说' }), '兜底')
    expect(msg).toBe('未找到匹配条件的小说')
  })

  it('extracts the first issue msg from 422 validation arrays', () => {
    const err = axiosErrorWith(
      { detail: [{ loc: ['body', 'limit'], msg: 'Input should be less than or equal to 500' }] },
      422,
    )
    expect(getApiErrorMessage(err, '兜底')).toBe('Input should be less than or equal to 500')
  })

  it('falls back when the detail is empty or absent', () => {
    expect(getApiErrorMessage(axiosErrorWith({ detail: '' }), '兜底')).toBe('兜底')
    expect(getApiErrorMessage(axiosErrorWith({}), '兜底')).toBe('兜底')
    expect(getApiErrorMessage(axiosErrorWith({ detail: [{ loc: ['x'] }] }), '兜底')).toBe('兜底')
  })

  it('uses the message of plain Error instances', () => {
    expect(getApiErrorMessage(new Error('network down'), '兜底')).toBe('network down')
  })

  it('returns the fallback for unknown values', () => {
    expect(getApiErrorMessage('oops', '兜底')).toBe('兜底')
    expect(getApiErrorMessage(undefined, '兜底')).toBe('兜底')
  })
})
