/** 本地 TXT 上传（我的上传）领域类型。权威：docs/LIBRARY_DESIGN.md §4
 *  后端端点 POST /api/library/uploads（multipart，字段名 `files` 重复多次）。 */

/** 逐条上传结果：created=已入库 / duplicate=正文 sha256 命中已有本地书 / failed=本条失败。 */
export type UploadItemStatus = 'created' | 'duplicate' | 'failed'

export interface UploadItemResult {
  filename: string
  status: UploadItemStatus
  /** created：新书 id（负数锚点）；duplicate：已在库中那本的 id。failed 时后端可能不下发。 */
  novel_id?: number
  /** created：解析出的标题；duplicate：已在库中那本的标题。 */
  title?: string
  /** created：解析出的作者名。 */
  author_name?: string
  /** created：规范化后的正文字符数。 */
  text?: number
  /** failed：失败原因（如「无法识别编码…」）。 */
  error?: string
}

/** 批量上传响应：部分成功也是 200；无文件是 400。 */
export interface UploadResponse {
  results: UploadItemResult[]
  created: number
  duplicate: number
  failed: number
}

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
