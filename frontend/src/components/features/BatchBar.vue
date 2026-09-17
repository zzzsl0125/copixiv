<script setup lang="ts">
import { ListChecks, Download, Tags, TagX, Trash2, Eye } from '@lucide/vue'
import { BAR_SHELL_CLASS, barContainerClass, barBtnClass } from '../../lib/ui'

defineProps<{
  matchedCount: number
  countLoading: boolean
  selectedCount: number
  hasSelection: boolean
  hasFilter: boolean
  selectAllLoading: boolean
  /** True while the main grid is showing the selected novels (查看已选). */
  isViewingSelected: boolean
}>()

const emit = defineEmits<{
  (e: 'select-all'): void
  (e: 'clear'): void
  (e: 'toggle-view-selected'): void
  (e: 'export'): void
  (e: 'add-tags'): void
  (e: 'remove-tags'): void
  (e: 'delete'): void
}>()
</script>

<template>
  <div :class="BAR_SHELL_CLASS" data-testid="batch-bar">
    <div :class="barContainerClass()">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
        <!-- 模式标识 + 查看已选 -->
        <span class="inline-flex items-center gap-1.5 text-sm font-semibold text-blue-700 shrink-0">
          <ListChecks class="h-4 w-4" /> 批量模式
        </span>
        <button
          type="button"
          :class="barBtnClass()"
          :disabled="!hasSelection"
          @click="emit('toggle-view-selected')"
        >
          <Eye class="h-4 w-4" />
          {{ isViewingSelected ? '返回搜索列表' : `查看已选 (${selectedCount})` }}
        </button>

        <!-- 选择操作 -->
        <button
          v-if="!isViewingSelected"
          type="button"
          :class="barBtnClass()"
          :disabled="countLoading || matchedCount === 0 || selectAllLoading"
          @click="emit('select-all')"
        >
          {{ selectAllLoading ? '全选中…' : `全选匹配 (${matchedCount})` }}
        </button>
        <button
          type="button"
          :class="barBtnClass()"
          :disabled="!hasSelection"
          @click="emit('clear')"
        >
          清除选择
        </button>

        <!-- 操作 -->
        <div class="ml-auto flex flex-wrap items-center gap-2">
          <button type="button" :class="barBtnClass()" :disabled="!hasSelection" @click="emit('export')">
            <Download class="h-4 w-4" /> 导出
          </button>
          <button type="button" :class="barBtnClass()" :disabled="!hasSelection" @click="emit('add-tags')">
            <Tags class="h-4 w-4" /> 添加标签
          </button>
          <button type="button" :class="barBtnClass()" :disabled="!hasSelection" @click="emit('remove-tags')">
            <TagX class="h-4 w-4" /> 移除标签
          </button>
          <button type="button" :class="barBtnClass('danger')" :disabled="!hasSelection" @click="emit('delete')">
            <Trash2 class="h-4 w-4" /> 删除
          </button>
        </div>

        <!-- 引导提示：无选择时提示先缩小范围（内联，非模态） -->
        <p v-if="!countLoading && !hasSelection" class="w-full text-xs leading-5 bg-amber-50 border border-amber-200 text-amber-800 rounded-md px-3 py-2">
          推荐先行搜索以缩小范围，再点击卡片空白处选中小说。全选/清除仅作用于当前搜索范围。
        </p>
      </div>
    </div>
  </div>
</template>
