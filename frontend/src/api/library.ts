import { apiClient } from './client'
import type { UploadResponse } from '../types'

export const libraryApi = {
  /**
   * 批量上传本地书籍（我的上传）。multipart/form-data，字段名固定 `files`
   * ——同一个字段重复多次承载多文件（后端按 `List[UploadFile]` 绑定）。
   * 收 .txt 与 .epub（同一批可混），逐条结果在响应体里；部分成功也是 200，
   * 无文件是 400。
   * 不手动设置 Content-Type：让浏览器带上含 boundary 的 multipart 头。
   *
   * `authorId`（可选）＝ 这一批书共用的预设作者（正整数 pixiv 作者 id）。
   * **不给就一个键都不加**：不预设的请求体与加这个字段之前一字不差，
   * 后端行为因此与今天完全一致。新书与重复件都会被应用这个作者。
   */
  async uploadFiles(
    files: File[],
    onUploadProgress?: (percent: number) => void,
    authorId?: number | null,
  ) {
    const form = new FormData()
    for (const file of files) form.append('files', file)
    if (authorId != null) form.append('author_id', String(authorId))

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
