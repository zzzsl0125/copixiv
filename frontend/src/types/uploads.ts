/** 本地书籍上传（我的上传）领域类型。权威：docs/LIBRARY_DESIGN.md §4/§4.1
 *  后端端点 POST /api/library/uploads（multipart，字段名 `files` 重复多次）。
 *  收 .txt 与 .epub：TXT 解码入库，EPUB 抽正文 + 保留原件。
 *
 *  API 实体来自生成的 OpenAPI 类型；下面的标签常量与格式化函数是客户端专有的。
 */
import type { components } from './api.generated'

type S = components['schemas']

/** 逐条上传结果：created=已入库 / duplicate=正文 sha256 命中已有本地书 / failed=本条失败。 */
export type UploadItemStatus = S['UploadResultItem']['status']

export type UploadItemResult = S['UploadResultItem']

/** 批量上传响应：部分成功也是 200；无文件是 400。 */
export type UploadResponse = S['UploadResponse']

/** 单条结果的中文标签（created/duplicate/failed 三态的唯一文案来源）。 */
export const UPLOAD_STATUS_LABEL: Record<UploadItemStatus, string> = {
  created: '已入库',
  duplicate: '已在库中',
  failed: '失败',
}

/** 人类可读的文件体积（待上传列表用）。 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
