import { uploadWithProgress } from './client'
import type { UploadResponse } from '../types'

export const libraryApi = {
  /**
   * 批量上传本地书籍（我的上传）。multipart/form-data，字段名固定 `files`。
   * 收 .txt 与 .epub（同一批可混），逐条结果在响应体里；部分成功也是 200，
   * 无文件是 400。
   */
  async uploadFiles(
    files: File[],
    onUploadProgress?: (percent: number) => void,
    authorId?: number | null,
    signal?: AbortSignal,
  ): Promise<UploadResponse> {
    const form = new FormData()
    for (const file of files) form.append('files', file)
    if (authorId != null) form.append('author_id', String(authorId))

    return uploadWithProgress<UploadResponse>('/api/library/uploads', form, {
      onUploadProgress,
      signal,
    })
  },
}
