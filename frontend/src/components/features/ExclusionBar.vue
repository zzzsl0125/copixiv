<script setup lang="ts">
import { EyeOff, Eye } from '@lucide/vue'

defineProps<{
  /** True while the main grid shows the excluded novels (查看被排除). */
  isViewingExcluded: boolean
  /** False in batch mode — the bar stays info-only (批量模式优先互斥). */
  interactive: boolean
}>()

const emit = defineEmits<{
  (e: 'toggle-view-excluded'): void
}>()
</script>

<template>
  <div
    class="bg-white border-b border-gray-200 shadow-sm"
    data-testid="exclusion-bar"
  >
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-1.5">
      <div class="flex items-center gap-x-3 text-xs">
        <span class="inline-flex items-center gap-1.5 text-amber-700 shrink-0">
          <EyeOff class="h-3.5 w-3.5" />
          已按厌恶标签排除部分小说
        </span>
        <button
          v-if="interactive"
          type="button"
          class="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded border border-gray-200 text-gray-600 bg-gray-50 hover:bg-gray-100 transition-colors"
          @click="emit('toggle-view-excluded')"
        >
          <Eye class="h-3.5 w-3.5" />
          {{ isViewingExcluded ? '返回浏览列表' : '查看被隐藏的小说' }}
        </button>
      </div>
    </div>
  </div>
</template>
