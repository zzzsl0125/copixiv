import { describe, it, expect, vi, beforeEach } from 'vitest'
import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'

const getConfigMock = vi.hoisted(() => vi.fn())

vi.mock('../../src/api', () => ({
  systemApi: { getConfig: getConfigMock },
}))

// useSystem keeps module-level state (cached config + in-flight promise),
// so each test re-imports the module to start from a clean slate.  The
// mock implementation must be installed BEFORE mounting: onMounted fires
// fetchConfig immediately.
async function freshUseSystem() {
  vi.resetModules()
  const { useSystem } = await import('../../src/composables/useSystem')
  const wrapper = mount(
    defineComponent({
      setup() {
        const state = useSystem()
        return { state }
      },
      template: '<div />',
    }),
  )
  return wrapper.vm.state as ReturnType<typeof useSystem>
}

describe('useSystem', () => {
  beforeEach(() => {
    // getConfigMock is shared across tests (hoisted) — reset its call log.
    getConfigMock.mockClear()
  })

  it('dedupes concurrent fetchConfig calls into one request', async () => {
    const config = { batch_download_naming: '{id}' }
    let resolveConfig!: (value: typeof config) => void
    getConfigMock.mockImplementation(
      () => new Promise<typeof config>((resolve) => { resolveConfig = resolve }),
    )
    const state = await freshUseSystem() // onMounted fires request #1

    // Both manual calls share the in-flight request from onMounted.
    const first = state.fetchConfig()
    const second = state.fetchConfig()

    expect(getConfigMock).toHaveBeenCalledTimes(1)

    resolveConfig(config)
    // Ref values are reactive proxies — compare deeply, not by identity.
    await expect(first).resolves.toEqual(config)
    await expect(second).resolves.toEqual(config)

    // A later call is served from the cache without a new request.
    await expect(state.fetchConfig()).resolves.toEqual(config)
    expect(getConfigMock).toHaveBeenCalledTimes(1)
  })

  it('resolves null and records the error when the config request fails', async () => {
    getConfigMock.mockRejectedValue(new Error('network down'))
    const state = await freshUseSystem() // onMounted already hit the failure

    await expect(state.fetchConfig()).resolves.toBeNull()
    expect(state.error.value).toBeInstanceOf(Error)
    expect(state.systemConfig.value).toBeNull()
    expect(getConfigMock).toHaveBeenCalledTimes(2)
  })

  it('exposes the fetched config on the shared ref', async () => {
    const config = { batch_download_naming: '{id}-{title}' }
    getConfigMock.mockResolvedValue(config)
    const state = await freshUseSystem()

    await state.fetchConfig()

    expect(state.systemConfig.value).toEqual(config)
    expect(state.loading.value).toBe(false)
  })
})
