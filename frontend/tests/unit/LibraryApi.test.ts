import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * libraryApi.uploadFiles 的 wire 契约（docs/LIBRARY_DESIGN.md §4）：
 * - POST /library/uploads（baseURL '/api'）；
 * - multipart 字段名是 `files`，**同一个键重复多次**（后端按 List[UploadFile] 绑定；
 *   用 files[] 或 JSON 数组序列化都会 422）；
 * - 可选的整批预设作者是 `author_id`：给了才出现，**没给就一个键都不加**
 *   （不预设的请求体与加这个字段之前一字不差）；
 * - 不手写 Content-Type（必须让浏览器补 boundary）；
 * - 上传不走全局 30s 超时（timeout: 0），响应直接透传 UploadResponse。
 */

const clientMock = vi.hoisted(() => ({
  post: vi.fn(),
}))

vi.mock('../../src/api/client', () => ({
  apiClient: { post: clientMock.post },
  buildApiUrl: (p: string) => p,
  apiBaseUrl: '/api',
}))

async function loadApi() {
  vi.resetModules()
  return import('../../src/api/library')
}

const makeFile = (name: string, size = 8) =>
  new File([new Uint8Array(size)], name, { type: 'text/plain' })

describe('libraryApi.uploadFiles', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('POST /library/uploads，files 字段重复多次，不手写 Content-Type / 不限超时', async () => {
    const response = {
      results: [{ filename: 'a.txt', status: 'created', novel_id: -1, title: 'A' }],
      created: 1,
      duplicate: 0,
      failed: 0,
    }
    clientMock.post.mockResolvedValue({ data: response })
    const { libraryApi } = await loadApi()

    const files = [makeFile('a.txt'), makeFile('b.txt')]
    const result = await libraryApi.uploadFiles(files)

    expect(clientMock.post).toHaveBeenCalledTimes(1)
    const [url, body, config] = clientMock.post.mock.calls[0]
    expect(url).toBe('/library/uploads')

    const form = body as FormData
    expect(form).toBeInstanceOf(FormData)
    expect(form.getAll('files')).toHaveLength(2)
    expect((form.getAll('files') as File[]).map((f) => f.name)).toEqual(['a.txt', 'b.txt'])

    const headers = (config as { headers?: Record<string, unknown> }).headers
    expect(headers?.['Content-Type']).toBeUndefined()
    expect((config as { timeout?: number }).timeout).toBe(0)
    expect(result).toEqual(response)
  })

  it('透传进度回调：把已发送字节比例换算成 0-100 的整数', async () => {
    clientMock.post.mockResolvedValue({
      data: { results: [], created: 0, duplicate: 0, failed: 0 },
    })
    const { libraryApi } = await loadApi()

    const seen: number[] = []
    await libraryApi.uploadFiles([makeFile('a.txt', 100)], (p) => seen.push(p))

    const config = clientMock.post.mock.calls[0][2] as {
      onUploadProgress: (e: { loaded: number; total?: number }) => void
    }
    config.onUploadProgress({ loaded: 50, total: 200 })
    config.onUploadProgress({ loaded: 200, total: 200 })
    // total 缺失（部分环境不提供）时不上报，避免出现 NaN%
    config.onUploadProgress({ loaded: 10, total: 0 })
    config.onUploadProgress({ loaded: 10 })

    expect(seen).toEqual([25, 100])
  })

  it('整体失败（400 / 网络）原样抛出，交给调用方 getApiErrorMessage', async () => {
    clientMock.post.mockRejectedValue(new Error('Request failed with status code 400'))
    const { libraryApi } = await loadApi()

    await expect(libraryApi.uploadFiles([makeFile('a.txt')])).rejects.toThrow('400')
  })

  it('给了预设作者 → FormData 里带 author_id（字符串），files 字段照旧', async () => {
    clientMock.post.mockResolvedValue({
      data: { results: [], created: 0, duplicate: 0, failed: 0 },
    })
    const { libraryApi } = await loadApi()

    await libraryApi.uploadFiles([makeFile('a.txt'), makeFile('b.txt')], undefined, 1234567)

    const form = clientMock.post.mock.calls[0][1] as FormData
    expect(form.get('author_id')).toBe('1234567')
    expect((form.getAll('files') as File[]).map((f) => f.name)).toEqual(['a.txt', 'b.txt'])
  })

  it('没给预设作者（缺省或显式 null）→ 一个 author_id 都不出现', async () => {
    clientMock.post.mockResolvedValue({
      data: { results: [], created: 0, duplicate: 0, failed: 0 },
    })
    const { libraryApi } = await loadApi()

    await libraryApi.uploadFiles([makeFile('a.txt')])
    await libraryApi.uploadFiles([makeFile('b.txt')], undefined, null)

    const [first, second] = clientMock.post.mock.calls
    expect((first[1] as FormData).has('author_id')).toBe(false)
    expect((second[1] as FormData).has('author_id')).toBe(false)
  })
})
