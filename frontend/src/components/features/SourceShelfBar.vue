<script setup lang="ts">
/**
 * 声明式来源书架顶部工具条（docs/MULTI_SOURCE_DESIGN.md §6.2）。
 *
 * 与 `BatchBar` / `ExclusionBar` 共用 `lib/ui.ts` 栏样式：
 * - `file_upload`：触发上传弹窗（如「我的上传」）；
 * - `ref_fetch`：粘贴 URL / ID 调用通用 `POST /api/sources/fetch` 任务。
 */
import { computed, ref, type Component } from 'vue'
import { BookOpen, Download, Globe, Plus, Sparkles, Upload } from '@lucide/vue'
import { getApiErrorMessage } from '../../api'
import { sourceApi } from '../../api/sources'
import { useToast } from '../../composables'
import { BAR_SHELL_CLASS, barBtnClass, barContainerClass } from '../../lib/ui'
import type { SourceFetchResponse, SourceShelfDef } from '../../types/sources'

const props = defineProps<{
  shelf: SourceShelfDef
  source: string
  displayName?: string
}>()

const emit = defineEmits<{
  (e: 'upload'): void
  (e: 'fetched', payload: SourceFetchResponse): void
}>()

const toast = useToast()
const refInput = ref('')
const force = ref(false)
const submitting = ref(false)

const SHELF_ICONS: Record<string, Component> = {
  sparkles: Sparkles,
  globe: Globe,
  upload: Upload,
  book: BookOpen,
}

const shelfIcon = computed<Component>(() => SHELF_ICONS[props.shelf.icon] ?? BookOpen)
const shelfTitle = computed(() => props.shelf.label || props.displayName || props.source)

const parsedRefs = computed(() =>
  refInput.value
    .split(/[\r\n\s,，]+/)
    .map((item) => item.trim())
    .filter(Boolean),
)

async function handleFetchSubmit() {
  if (submitting.value || parsedRefs.value.length === 0) return
  submitting.value = true
  try {
    const result = await sourceApi.fetchRefs({
      source: props.source,
      refs: parsedRefs.value,
      force: force.value,
    })
    toast.success(
      `已提交 ${shelfTitle.value} 抓取任务 #${result.task_id}（共 ${result.ref_count} 条）`,
    )
    refInput.value = ''
    emit('fetched', result)
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '提交抓取任务失败'))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div :class="BAR_SHELL_CLASS" data-testid="source-shelf-bar">
    <div :class="barContainerClass()">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span class="inline-flex items-center gap-1.5 text-sm font-semibold text-blue-700 shrink-0">
          <component :is="shelfIcon" class="h-4 w-4" />
          {{ shelfTitle }}
        </span>

        <div
          v-if="shelf.intake_action === 'file_upload'"
          class="ml-auto flex flex-wrap items-center gap-2"
        >
          <button
            type="button"
            :class="barBtnClass('primary')"
            data-testid="shelf-bar-upload"
            @click="emit('upload')"
          >
            <Plus class="h-4 w-4" />
            {{ shelf.intake_button_text || '上传书籍' }}
          </button>
        </div>

        <form
          v-else-if="shelf.intake_action === 'ref_fetch'"
          class="flex-1 flex flex-wrap items-center gap-2 min-w-0"
          @submit.prevent="void handleFetchSubmit()"
        >
          <textarea
            v-model="refInput"
            rows="1"
            :placeholder="shelf.intake_placeholder || '输入 URL 或 ID（支持换行、空格或逗号分隔）'"
            :disabled="submitting"
            aria-label="抓取引用"
            data-testid="shelf-bar-ref-input"
            class="flex-1 min-w-48 px-3 py-1.5 text-sm leading-5 border border-gray-300 rounded-md resize-none focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:bg-gray-100"
            @keydown.enter.exact.prevent="void handleFetchSubmit()"
          ></textarea>

          <label class="inline-flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer shrink-0 select-none">
            <input
              v-model="force"
              type="checkbox"
              :disabled="submitting"
              data-testid="shelf-bar-force"
              class="h-3.5 w-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
            />
            强制重抓
          </label>

          <button
            type="submit"
            :class="barBtnClass('primary')"
            :disabled="submitting || parsedRefs.length === 0"
            data-testid="shelf-bar-ref-submit"
          >
            <Download class="h-4 w-4" />
            {{ submitting ? '提交中…' : (shelf.intake_button_text || '抓取') }}
          </button>
        </form>
      </div>
    </div>
  </div>
</template>
