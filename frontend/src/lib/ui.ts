/**
 * 列表顶部栏的共用样式（docs/LIBRARY_DESIGN.md §5）。
 *
 * BatchBar（批量模式）/ ExclusionBar（厌恶标签排除）/ UploadBar（我的上传）
 * 三条栏过去各抄一份同样的 Tailwind 串——收成单源，避免改一处漏两处。
 *
 * 为什么共享「类名」而不是抽一层外壳组件：三条栏的 DOM 结构本就一致
 * （外壳 → 定宽容器 → flex 内容行），抽组件只会给每条栏的内容多套一层缩进，
 * diff 噪音大而收益相同。真正会漂移的是这些串，故把它们收到这里。
 */

/** 栏外壳：白底 + 下边框 + 轻阴影，作为列表 sticky 头部里的一条。 */
export const BAR_SHELL_CLASS = 'bg-white border-b border-gray-200 shadow-sm'

/** 栏内容容器：与页面主体同宽；`dense` 用于单行信息条（排除条）。 */
export function barContainerClass(dense = false): string {
  return `max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 ${dense ? 'py-1.5' : 'py-3'}`
}

/** 栏内按钮：default（灰）/ primary（蓝）/ danger（红）三态。 */
export function barBtnClass(variant: 'default' | 'primary' | 'danger' = 'default'): string {
  const base =
    'inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md ' +
    'transition-colors disabled:opacity-40 disabled:cursor-not-allowed'
  if (variant === 'danger') return `${base} text-white bg-red-500 hover:bg-red-600`
  if (variant === 'primary') return `${base} text-white bg-blue-500 hover:bg-blue-600`
  return `${base} text-gray-700 bg-gray-100 hover:bg-gray-200`
}
