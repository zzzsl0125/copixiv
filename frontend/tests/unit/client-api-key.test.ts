import { describe, it, expect, vi } from 'vitest'

const axiosMock = vi.hoisted(() => ({
  requestUse: vi.fn(),
}))

vi.mock('axios', () => ({
  default: {
    create: () => ({
      interceptors: {
        request: { use: axiosMock.requestUse },
      },
    }),
  },
}))

describe('apiClient X-API-Key interceptor', () => {
  it('attaches the header when VITE_API_KEY is configured', async () => {
    vi.stubEnv('VITE_API_KEY', 'secret-key')
    vi.resetModules()

    await import('../../src/api/client')
    expect(axiosMock.requestUse).toHaveBeenCalledTimes(1)

    const interceptor = axiosMock.requestUse.mock.calls[0][0] as (
      config: unknown,
    ) => unknown
    const config = {
      headers: { set: vi.fn() },
    }
    interceptor(config)

    expect(config.headers.set).toHaveBeenCalledWith('X-API-Key', 'secret-key')

    vi.unstubAllEnvs()
    vi.resetModules()
  })

  it('does not register an interceptor when no key is configured', async () => {
    axiosMock.requestUse.mockClear()
    vi.stubEnv('VITE_API_KEY', '')
    vi.resetModules()

    await import('../../src/api/client')
    expect(axiosMock.requestUse).not.toHaveBeenCalled()

    vi.unstubAllEnvs()
    vi.resetModules()
  })
})

describe('buildApiUrl (navigation-based downloads)', () => {
  it('appends api_key as a query param when a key is configured', async () => {
    vi.stubEnv('VITE_API_KEY', 'my-secret')
    vi.resetModules()

    const { buildApiUrl } = await import('../../src/api/client')
    expect(buildApiUrl('/novels/42/download?format=txt')).toBe(
      '/api/novels/42/download?format=txt&api_key=my-secret',
    )
    expect(buildApiUrl('/novels/export/7/download')).toBe(
      '/api/novels/export/7/download?api_key=my-secret',
    )

    vi.unstubAllEnvs()
    vi.resetModules()
  })

  it('leaves the URL untouched when no key is configured', async () => {
    vi.stubEnv('VITE_API_KEY', '')
    vi.resetModules()

    const { buildApiUrl } = await import('../../src/api/client')
    expect(buildApiUrl('/novels/42/download?format=txt')).toBe(
      '/api/novels/42/download?format=txt',
    )

    vi.unstubAllEnvs()
    vi.resetModules()
  })
})
