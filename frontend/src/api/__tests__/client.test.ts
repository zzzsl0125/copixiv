import { describe, expect, it } from 'vitest'
import {
  ApiError,
  buildApiUrl,
  extractFastApiDetail,
  getApiErrorMessage,
  isAbortError,
  unwrap,
  unwrapVoid,
} from '../index'

describe('api client & errors', () => {
  it('extracts string and 422 validation details from FastAPI error bodies', () => {
    expect(extractFastApiDetail({ detail: '同名账号已存在' })).toBe('同名账号已存在')
    expect(
      extractFastApiDetail({
        detail: [{ loc: ['query', 'order_by'], msg: 'Input should be valid' }],
      }),
    ).toBe('Input should be valid')
    expect(extractFastApiDetail({ detail: '   ' })).toBeNull()
    expect(extractFastApiDetail(null)).toBeNull()
  })

  it('formats ApiError, generic Error, and unknown values cleanly', () => {
    const apiErr = new ApiError(400, { detail: '该标签偏好已存在' }, 'fallback msg')
    expect(getApiErrorMessage(apiErr, 'default')).toBe('该标签偏好已存在')

    const plainApiErr = new ApiError(500, null, '服务器异常')
    expect(getApiErrorMessage(plainApiErr, 'default')).toBe('服务器异常')

    expect(getApiErrorMessage(new Error('network down'), 'default')).toBe('network down')
    expect(getApiErrorMessage('weird', 'default')).toBe('default')
  })

  it('detects AbortError across DOMException and plain error objects', () => {
    expect(isAbortError(new DOMException('Aborted', 'AbortError'))).toBe(true)
    expect(isAbortError({ name: 'AbortError' })).toBe(true)
    expect(isAbortError(new Error('boom'))).toBe(false)
    expect(isAbortError(null)).toBe(false)
  })

  it('unwrap and unwrapVoid throw ApiError with extracted detail on HTTP errors', () => {
    const okRes = unwrap({
      data: { id: 1 },
      response: new Response(null, { status: 200 }),
    })
    expect(okRes).toEqual({ id: 1 })

    expect(() =>
      unwrap(
        {
          error: { detail: '未找到小说' },
          response: new Response(null, { status: 404 }),
        },
        '默认失败',
      ),
    ).toThrowError(ApiError)

    expect(() =>
      unwrapVoid({
        error: undefined,
        response: new Response(null, { status: 204 }),
      }),
    ).not.toThrow()

    expect(() =>
      unwrapVoid(
        {
          error: { detail: '冲突' },
          response: new Response(null, { status: 400 }),
        },
        '默认失败',
      ),
    ).toThrow('冲突')
  })

  it('buildApiUrl prefixes /api cleanly', () => {
    expect(buildApiUrl('/novels/42/download?format=epub')).toBe(
      '/api/novels/42/download?format=epub',
    )
  })
})
