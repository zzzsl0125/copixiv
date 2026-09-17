<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import { novelApi, getApiErrorMessage } from '../../api'
import type { Novel, NovelUpdatePayload } from '../../types'

/**
 * 单本元数据编辑弹窗（卡片底部操作条的「编辑」）。
 *
 * 契约：docs/NOVEL_EDIT_DESIGN.md §4（不做字段锁）/ §5（PATCH 三态）/ §6.2。
 * 两条 2026-09-18 拍板直接决定这里的形态：
 *
 * - `title` / `series_name` **仅本地书可编辑**：pixiv 来源会无条件覆盖这两列，
 *   放开编辑只会制造"改完又变回去"的假象 → 置灰 + 说明，且不进 payload
 *   （服务端同样硬拒，见 update_metadata.py）。
 * - `caption` 全局不可编辑 → 置灰 + "暂不支持编辑"。
 *
 * 只提交**与初值不同**的字段：PATCH 的"未提交 = 不动"是我们唯一不误伤
 * 其余字段的保证。
 */

const props = defineProps<{
  isOpen: boolean
  /** 卡片手里的完整对象（初值来源）——保存成功后父组件会把它换成响应值。 */
  novel: Novel | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'updated', novel: Novel): void
  (e: 'error', message: string): void
}>()

// 与后端 update_metadata.py 的限值保持一致（本地校验只为即时反馈，
// 权威仍在服务端）。
const MAX_TITLE = 300
const MAX_AUTHOR_NAME = 200
const MAX_SERIES_NAME = 300
const MAX_TAGS = 50
const MAX_TAG_LENGTH = 100
// 与后端 parse_tags 的切分符一致：用户粘贴 "R-18/魔法" 时按两个标签收下。
const TAG_SPLIT = /[/|#、\\]/

const loading = ref(false)
const title = ref('')
const authorName = ref('')
const seriesName = ref('')
const seriesIndex = ref('')
const tags = ref<string[]>([])
const createTime = ref('')
const tagInput = ref('')

const isLocal = computed(() => props.novel?.source === 'local')

/** 每次打开都从卡片手里的对象重读初值（保存后那个对象已被响应替换）。 */
function reset() {
  const novel = props.novel
  title.value = novel?.title ?? ''
  authorName.value = novel?.author_name ?? ''
  seriesName.value = novel?.series_name ?? ''
  seriesIndex.value = novel?.series_index != null ? String(novel.series_index) : ''
  tags.value = [...(novel?.tags ?? [])]
  createTime.value = (novel?.create_time ?? '').slice(0, 10)
  tagInput.value = ''
}

watch(() => props.isOpen, (open) => {
  if (open) reset()
  // immediate：宿主若一开始就带着 isOpen=true 挂载（测试、或将来改成条件渲染），
  // 表单同样要装载初值——否则是一个"打开着但空的"编辑器。
}, { immediate: true })

/** 章节号：空 = 清空（null）；非数字 = 非法（undefined）。 */
const indexNumber = computed<number | null | undefined>(() => {
  const raw = seriesIndex.value.trim()
  if (!raw) return null
  if (!/^\d+$/.test(raw)) return undefined
  return Number(raw)
})

const validationError = computed<string | null>(() => {
  // 标题 / 系列名的校验**只在它们是可编辑的时候生效**（本地书）。pixiv 书这两个
  // 字段置灰，若仍拿它们拦保存，一本标题为空的 pixiv 书（语料里确实有）会永远
  // 停在"标题不能为空"上——连改标签都做不了，而那个字段他又改不了。
  if (isLocal.value) {
    if (!title.value.trim()) return '标题不能为空'
    if (title.value.trim().length > MAX_TITLE) return `标题过长（最多 ${MAX_TITLE} 字）`
    if (seriesName.value.trim().length > MAX_SERIES_NAME) {
      return `系列名过长（最多 ${MAX_SERIES_NAME} 字）`
    }
  }
  if (authorName.value.trim().length > MAX_AUTHOR_NAME) {
    return `作者名过长（最多 ${MAX_AUTHOR_NAME} 字）`
  }
  if (indexNumber.value === undefined) return '章节号必须是非负整数'
  if (tags.value.length > MAX_TAGS) return `标签过多（最多 ${MAX_TAGS} 个）`
  if (tags.value.some((tag) => tag.length > MAX_TAG_LENGTH)) {
    return `单个标签最多 ${MAX_TAG_LENGTH} 字`
  }
  return null
})

/** 与初值 diff —— 只把真正变化的字段放进 payload。 */
function buildPayload(): NovelUpdatePayload {
  const novel = props.novel
  const payload: NovelUpdatePayload = {}
  if (!novel) return payload

  if (isLocal.value) {
    const nextTitle = title.value.trim()
    if (nextTitle !== novel.title) payload.title = nextTitle

    const nextSeries = seriesName.value.trim() || null
    if (nextSeries !== (novel.series_name ?? null)) payload.series_name = nextSeries
  }

  const nextAuthor = authorName.value.trim()
  if (nextAuthor !== (novel.author_name ?? '')) payload.author_name = nextAuthor

  const nextIndex = indexNumber.value ?? null
  if (nextIndex !== (novel.series_index ?? null)) payload.series_index = nextIndex

  const nextTags = [...tags.value].sort()
  const prevTags = [...(novel.tags ?? [])].sort()
  if (nextTags.join('\u0000') !== prevTags.join('\u0000')) payload.tags = tags.value

  const nextDate = createTime.value.trim()
  if (nextDate !== (novel.create_time ?? '').slice(0, 10)) {
    payload.create_time = nextDate || null
  }

  return payload
}

const hasChanges = computed(() => Object.keys(buildPayload()).length > 0)
const confirmDisabled = computed(() => !!validationError.value || !hasChanges.value)

function addTag() {
  const parts = tagInput.value.split(TAG_SPLIT).map((t) => t.trim()).filter(Boolean)
  if (parts.length === 0) return
  const seen = new Set(tags.value.map((t) => t.toLowerCase()))
  for (const part of parts) {
    // 大小写按后端 normalize_tag 的语义去重；落库值以响应为准。
    if (seen.has(part.toLowerCase())) continue
    seen.add(part.toLowerCase())
    tags.value.push(part)
  }
  tagInput.value = ''
}

function removeTag(tag: string) {
  tags.value = tags.value.filter((t) => t !== tag)
}

async function handleConfirm() {
  const novel = props.novel
  if (!novel || loading.value || confirmDisabled.value) return

  loading.value = true
  try {
    const updated = await novelApi.updateNovel(novel.id, buildPayload())
    emit('updated', updated)
    emit('close')
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '保存失败'))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    title="编辑书籍信息"
    :loading="loading"
    confirm-text="保存"
    :confirm-disabled="confirmDisabled"
    @close="emit('close')"
    @confirm="handleConfirm"
  >
    <div v-if="novel" class="space-y-4 text-sm text-gray-700">
      <!-- 顶部：来源徽章 + id + 只读统计（机器事实不进编辑器） -->
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500">
        <span
          class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold"
          :class="isLocal ? 'bg-amber-100 text-amber-800' : 'bg-gray-100 text-gray-700'"
          data-testid="edit-source-badge"
        >
          {{ isLocal ? '本地' : 'pixiv' }}
        </span>
        <span>#{{ novel.id }}</span>
        <span title="喜爱">❤️ {{ novel.like }}</span>
        <span title="浏览">👁 {{ novel.view }}</span>
        <span title="字数">📝 {{ novel.text }}</span>
      </div>

      <div>
        <label class="block text-sm font-medium text-gray-700" for="edit-novel-title">标题</label>
        <input
          id="edit-novel-title"
          v-model="title"
          type="text"
          :disabled="!isLocal"
          data-testid="edit-title"
          class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
        />
        <p v-if="!isLocal" class="mt-1 text-xs text-gray-400" data-testid="edit-title-hint">
          pixiv 来源会覆盖，以来源为准（仅本地书可改）
        </p>
      </div>

      <div>
        <label class="block text-sm font-medium text-gray-700" for="edit-novel-author">作者</label>
        <input
          id="edit-novel-author"
          v-model="authorName"
          type="text"
          placeholder="留空则记为「未知作者」"
          data-testid="edit-author"
          class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
        />
        <!-- pixiv 书的作者名是**本书这一列**（列表读的就是 novel.author_name），
             改它不会动 author 表，作者页与同作者其它作品显示原名称——值得先说清。 -->
        <p v-if="!isLocal" class="mt-1 text-xs text-gray-400" data-testid="edit-author-hint">
          只改本书的显示名；作者页与其名下其它作品不变
        </p>
      </div>

      <div class="flex gap-3">
        <div class="flex-1 min-w-0">
          <label class="block text-sm font-medium text-gray-700" for="edit-novel-series">系列</label>
          <input
            id="edit-novel-series"
            v-model="seriesName"
            type="text"
            :disabled="!isLocal"
            data-testid="edit-series"
            class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
          />
          <p v-if="!isLocal" class="mt-1 text-xs text-gray-400" data-testid="edit-series-hint">
            pixiv 来源会覆盖，以来源为准（仅本地书可改）
          </p>
        </div>
        <div class="w-24 shrink-0">
          <label class="block text-sm font-medium text-gray-700" for="edit-novel-index">章节号</label>
          <input
            id="edit-novel-index"
            v-model="seriesIndex"
            type="text"
            inputmode="numeric"
            placeholder="—"
            data-testid="edit-series-index"
            class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
          />
        </div>
      </div>

      <div>
        <label class="block text-sm font-medium text-gray-700" for="edit-novel-tag-input">标签</label>
        <div v-if="tags.length" class="mt-1 mb-2 flex flex-wrap gap-1" data-testid="edit-tags">
          <span
            v-for="tag in tags"
            :key="tag"
            class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-blue-50 text-blue-700"
          >
            {{ tag }}
            <button
              type="button"
              class="text-blue-400 hover:text-blue-700 cursor-pointer"
              :aria-label="`删除标签 ${tag}`"
              :data-testid="`edit-tag-remove-${tag}`"
              @click="removeTag(tag)"
            >
              ×
            </button>
          </span>
        </div>
        <input
          id="edit-novel-tag-input"
          v-model="tagInput"
          type="text"
          autocomplete="off"
          placeholder="输入标签后回车添加（可用 / # 、 分隔多个）"
          data-testid="edit-tag-input"
          class="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
          @keydown.enter.prevent="addTag"
        />
        <p class="mt-1 text-xs text-gray-400">
          标签在服务端统一小写并与别名合并，保存后以返回结果为准。
        </p>
      </div>

      <div>
        <label class="block text-sm font-medium text-gray-700" for="edit-novel-date">发布日期</label>
        <input
          id="edit-novel-date"
          v-model="createTime"
          type="date"
          data-testid="edit-date"
          class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
        />
        <p class="mt-1 text-xs text-gray-400">留空则清除日期。</p>
      </div>

      <div>
        <label class="block text-sm font-medium text-gray-700" for="edit-novel-caption">简介</label>
        <textarea
          id="edit-novel-caption"
          :value="novel.caption || ''"
          rows="2"
          disabled
          data-testid="edit-caption"
          class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm sm:text-sm bg-gray-100 text-gray-500 cursor-not-allowed resize-none"
        ></textarea>
        <p class="mt-1 text-xs text-gray-400" data-testid="edit-caption-hint">暂不支持编辑</p>
      </div>

      <p v-if="validationError" class="text-xs text-red-600" data-testid="edit-validation-error">
        {{ validationError }}
      </p>
    </div>
  </BaseModal>
</template>
