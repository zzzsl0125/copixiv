import { apiClient } from './client'
import type { UploadResponse } from '../types'

export const libraryApi = {
  /**
   * 批量上传本地 TXT（我的上传）。multipart/form-data，字段名固定 `files`
   * ——同一个字段重复多次承载多文件（后端按 `List[UploadFile]` 绑定）。
   * 部分成功也是 200，逐条结果在响应体里；无文件是 400。
   * 不手动设置 Content-Type：让浏览器带上含 boundary 的 multipart 头。
   */
  async uploadFiles(files: File[], onUploadProgress?: (percent: number) => void) {
    const form = new FormData()
    for (const file of files) form.append('files', file)

    const response = await apiClient.post('/library/uploads', form, {
      // 上传耗时随文件体积增长，不走全局 30s 超时。
      timeout: 0,
      onUploadProgress: (event) => {
        if (!onUploadProgress) return
        const total = event.total ?? 0
        if (total <= 0) return
        onUploadProgress(Math.round((event.loaded / total) * 100))
      },
    })
    return response.data as UploadResponse
  },
}
