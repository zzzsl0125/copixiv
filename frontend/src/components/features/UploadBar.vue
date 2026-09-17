<script setup lang="ts">
/**
 * 「我的上传」书架的工具条（docs/LIBRARY_DESIGN.md §5）。
 *
 * 与 `BatchBar` 同构：同一套栏外壳 / 容器 / 按钮样式（`lib/ui.ts`），
 * 渲染在 `NovelHeader` 之下的 sticky 头部里，只在「我的上传」section 且
 * 非批量模式时出现——批量模式由 BatchBar 接管，两条栏不叠。
 *
 * 上传按钮**不放侧边栏**：侧边栏只留导航入口（与「我的收藏」一致），
 * 动作按钮统一待在列表顶部栏这一层。
 */
import { Upload, Plus } from '@lucide/vue'
import { BAR_SHELL_CLASS, barContainerClass, barBtnClass } from '../../lib/ui'

const emit = defineEmits<{
  (e: 'upload'): void
}>()
</script>

<template>
  <div :class="BAR_SHELL_CLASS" data-testid="uploads-bar">
    <div :class="barContainerClass()">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span class="inline-flex items-center gap-1.5 text-sm font-semibold text-blue-700 shrink-0">
          <Upload class="h-4 w-4" /> 我的上传
        </span>

        <div class="ml-auto flex flex-wrap items-center gap-2">
          <button
            type="button"
            :class="barBtnClass('primary')"
            data-testid="uploads-bar-upload"
            @click="emit('upload')"
          >
            <Plus class="h-4 w-4" /> 上传 TXT
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
