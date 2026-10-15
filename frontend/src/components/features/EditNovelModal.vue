<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import { novelApi, getApiErrorMessage } from '../../api'
import { formatIdLookupHint, useIdLookup } from '../../composables'
import type { AuthorLookup, Novel, NovelUpdatePayload, SeriesLookup } from '../../types'

/**
 * 单本元数据编辑弹窗（卡片底部操作条的「编辑」）。
 *
 * 契约：docs/NOVEL_EDIT_DESIGN.md §4（不做字段锁）/ §5（PATCH 三态）/ §6.2。
 * 「哪个字段能改」**不在前端判断**：服务端在每本书上下发 `editable_fields`
 * （pixiv 书不含 `title` / `author_id` / `series_id` / `caption`），这里只读那个
 * 数组置灰、收口校验与 payload。原来按 `source === 'local'` 推的那份表已删除
 * ——它与 `update_metadata.py` 的执法规则是同一件事，留两份就会漂移成
 * "能点但保存 400"。
 *
 * 其余拍板仍然决定这里的形态：
 *
 * - `caption` **仅本地书可编辑**（2026-09-22 改版；此前写死 `disabled` 的"全局
 *   不可编辑"是拿 pixiv 书论证的——它在来源覆盖白名单里，手改会被顶回去。
 *   本地书永不进那条写路径、上传时这一列是 NULL，所以放开没有风险）。
 *   置灰依旧只认 `canEdit('caption')`，pixiv 书仍然是只读输入框。
 * - **作者编辑的是 pixiv 作者 ID，不是名字**（2026-09-19）。
 * - **系列同理，编辑 `series_id`**（2026-09-19）：名字由 id 解析
 *   （`series` 行 → 同系列小说行 → pixiv），上传的本地书因此能挂到 pixiv
 *   系列名下——卡片点系列、系列页、`series_id:` 搜索全按 id 工作。
 * - **解除关联的收尾两者不同**（2026-09-22）：`author_id` 留空保留显示名（名字
 *   有独立来源＝文件名解析），`series_id` 留空连名字与章节号一起清（本地书的
 *   系列名只可能由 id 解析而来，留着就是卡片上一串点不动、也删不掉的文字）。
 *   两句 hint 因此分开给（`emptyNote` / `unlinkNote`），见 `formatIdLookupHint`。
 * - **左下角「同步」**（仅 pixiv 书）：按 id 重新抓取来源元数据，响应刷新初值并
 *   上抛 `updated`（父组件原地换卡片）。本地书没有来源可同步，服务端也会 400。
 *
 * 两个 ID 字段共用 `useIdLookup`（防抖 / 过期响应丢弃 / 与当前值相同就不查 /
 * 失败不拦保存），所以"系列也一样"是构造上的一致，不是两段平行代码的巧合。
 *
 * 布局：「标题 + 作者 ID」一行、「系列 ID + 章节号」一行，两行同构（1 : 窄），
 * 每行下面一行 hint 说清解析结果（**只在字段可编辑时渲染**，不可编辑就不占行高，
 * 也不摆静态说明）。
 *
 * 只提交**与初值不同**的字段：PATCH 的"未提交 = 不动"是我们唯一不误伤
 * 其余字段的保证。
 */

const props = defineProps<{
  isOpen: boolean
  /** 卡片手里的完整对象（初值来源）——保存 / 同步成功后父组件会把它换成响应值。 */
  novel: Novel | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'updated', novel: Novel): void
  (e: 'deleted', novelId: number): void
  (e: 'error', message: string): void
}>()

// 与后端 update_metadata.py 的限值保持一致（本地校验只为即时反馈，
// 权威仍在服务端）。
const MAX_TITLE = 300
const MAX_TAGS = 50
const MAX_TAG_LENGTH = 100
const MAX_CAPTION = 2000
// 与后端 parse_tags 的切分符一致：用户粘贴 "R-18/魔法" 时按两个标签收下。
const TAG_SPLIT = /[/|#、\\]/

const loading = ref(false)
/** 「同步」进行中：与 `loading` 分开——保存按钮的文案只该被保存自己点亮。 */
const syncing = ref(false)
/** 「删除」进行中。 */
const deleting = ref(false)
const title = ref('')
const authorId = ref('')
const seriesId = ref('')
const seriesIndex = ref('')
const tags = ref<string[]>([])
const createTime = ref('')
const caption = ref('')
const tagInput = ref('')

/** 来源徽章与「同步」按钮看的是来源（'pixiv' 缺省）。 */
const isLocal = computed(() => props.novel?.source === 'local')
/** pixiv 书才有"从来源重新抓取"这回事；本地书服务端会 400，按钮不必出现。 */
const canSync = computed(() => !!props.novel && !isLocal.value)

/**
 * 这个字段现在可编辑吗？唯一依据是服务端下发的 `editable_fields`。
 *
 * 取不到（老响应 / 字段缺失）一律 `false`：宁可否决也不放行一个服务端会 400 的
 * 提交。置灰、本地校验、payload 三处共用它，字段策略只有这一处消费点。
 *
 * 形参收在 `NovelUpdatePayload` 的键上（= 可提交字段的全集，与策略数组同构）：
 * 写错字段名编译期就红，不必再抄一份字段清单。
 */
function canEdit(field: keyof NovelUpdatePayload): boolean {
  return props.novel?.editable_fields?.includes(field) ?? false
}

const authorLookup = useIdLookup<AuthorLookup>({
  input: authorId,
  currentId: () => props.novel?.author_id ?? null,
  fetch: (id) => novelApi.lookupAuthor(id),
  // 只读字段连请求都不发：pixiv 书的作者 id 不可改，解析出来也无处提交。
  enabled: () => canEdit('author_id'),
})

// 系列同理：`enabled` 让它在只读时一个请求都不发。
const seriesLookup = useIdLookup<SeriesLookup>({
  input: seriesId,
  currentId: () => props.novel?.series_id ?? null,
  fetch: (id) => novelApi.lookupSeries(id),
  enabled: () => canEdit('series_id'),
})

/**
 * 章节号：空 = 清空（null）；非法 = `undefined`（拦保存）。
 *
 * `0` 非法（2026-09-22 契约变更，服务端 400）：章节号是"系列里的第几篇"，
 * 从 1 起数；错误文案与服务端 detail 一字不差（同一句话，不写第二份）。
 */
const indexNumber = computed<number | null | undefined>(() => {
  const raw = seriesIndex.value.trim()
  if (!raw) return null
  if (!/^\d+$/.test(raw)) return undefined
  const parsed = Number(raw)
  return parsed > 0 ? parsed : undefined
})

/**
 * 把一本小说装进表单：打开时用 `props.novel`（卡片手里的初值），同步成功后用
 * 响应值（父组件此时也会把卡片换成同一个对象）。
 */
function loadFrom(novel: Novel | null) {
  title.value = novel?.title ?? ''
  authorId.value = novel?.author_id != null ? String(novel.author_id) : ''
  seriesId.value = novel?.series_id != null ? String(novel.series_id) : ''
  seriesIndex.value = novel?.series_index != null ? String(novel.series_index) : ''
  tags.value = [...(novel?.tags ?? [])]
  createTime.value = (novel?.create_time ?? '').slice(0, 10)
  caption.value = novel?.caption ?? ''
  tagInput.value = ''
  // 值没变时 watch 不触发（例如重新打开同一本），这里显式评估一次。
  authorLookup.schedule()
  seriesLookup.schedule()
}

watch(() => props.isOpen, (open) => {
  if (open) loadFrom(props.novel)
  // immediate：宿主若一开始就带着 isOpen=true 挂载（测试、或将来改成条件渲染），
  // 表单同样要装载初值——否则是一个"打开着但空的"编辑器。
}, { immediate: true })

/** 一行 hint 说清"这个 ID 会被解析成谁"。四档语气见 formatIdLookupHint。 */
const authorHint = computed(() => formatIdLookupHint({
  entity: '作者',
  idNumber: authorLookup.idNumber.value,
  currentId: props.novel?.author_id ?? null,
  currentName: props.novel?.author_name || '未知',
  loading: authorLookup.loading.value,
  failed: authorLookup.failed.value,
  found: authorLookup.result.value
    ? {
        name: authorLookup.result.value.author_name,
        novelCount: authorLookup.result.value.novel_count,
      }
    : null,
  emptyNote: '留空 = 不改作者（名字仍来自文件名解析）',
  // 解除关联保留名字：author_name 在上传时来自文件名解析，有独立来源。
  unlinkNote: `留空 = 解除作者关联；显示名保留「${props.novel?.author_name || '未知'}」`,
}))

const seriesHint = computed(() => formatIdLookupHint({
  entity: '系列',
  idNumber: seriesLookup.idNumber.value,
  currentId: props.novel?.series_id ?? null,
  currentName: props.novel?.series_name || '未知',
  loading: seriesLookup.loading.value,
  failed: seriesLookup.failed.value,
  found: seriesLookup.result.value
    ? {
        name: seriesLookup.result.value.series_name,
        novelCount: seriesLookup.result.value.novel_count,
      }
    : null,
  emptyNote: '留空 = 不改系列',
  // 解除关联连名字与章节号一起清（2026-09-22）：本地书的 series_name 上传时是
  // NULL，只可能由 series_id 解析而来——留着它就是一串清不掉的孤儿显示名
  // （名字本身提交即 422）。见后端 update_metadata.py::_series_columns。
  unlinkNote: `留空 = 解除系列关联，并清掉显示名「${props.novel?.series_name || '未知'}」与章节号`,
}))

const validationError = computed<string | null>(() => {
  // 校验**只针对可编辑的字段**：不可编辑的字段既不进 payload，就不该拦保存。
  // pixiv 书的标题（语料里确实有空标题的行）若仍拿它拦保存，这本书连改标签都
  // 做不了，而那个字段他又改不了。
  if (canEdit('title')) {
    if (!title.value.trim()) return '标题不能为空'
    if (title.value.trim().length > MAX_TITLE) return `标题过长（最多 ${MAX_TITLE} 字）`
  }
  if (canEdit('series_id') && seriesLookup.idNumber.value === undefined) {
    return '系列 ID 必须是正整数'
  }
  if (canEdit('author_id') && authorLookup.idNumber.value === undefined) {
    return '作者 ID 必须是正整数'
  }
  if (canEdit('series_index') && indexNumber.value === undefined) {
    return '章节号必须是正整数'   // 与后端 400 的 detail 同一句话
  }
  if (canEdit('tags') && tags.value.length > MAX_TAGS) return `标签过多（最多 ${MAX_TAGS} 个）`
  if (canEdit('tags') && tags.value.some((tag) => tag.length > MAX_TAG_LENGTH)) {
    return `单个标签最多 ${MAX_TAG_LENGTH} 字`
  }
  if (canEdit('caption') && caption.value.trim().length > MAX_CAPTION) {
    return `简介过长（最多 ${MAX_CAPTION} 字）`   // 与后端 400 的 detail 同一句话
  }
  return null
})

/** 与初值 diff —— 只把真正变化、且**允许编辑**的字段放进 payload。 */
function buildPayload(): NovelUpdatePayload {
  const novel = props.novel
  const payload: NovelUpdatePayload = {}
  if (!novel) return payload

  if (canEdit('title')) {
    const nextTitle = title.value.trim()
    if (nextTitle !== novel.title) payload.title = nextTitle
  }

  if (canEdit('series_id')) {
    // 系列只提交 id：名字由服务端解析后回写（客户端提交 series_name 会 422）。
    const nextSeriesId = seriesLookup.idNumber.value ?? null
    if (nextSeriesId !== (novel.series_id ?? null)) payload.series_id = nextSeriesId
  }

  if (canEdit('author_id')) {
    const nextAuthorId = authorLookup.idNumber.value ?? null
    if (nextAuthorId !== (novel.author_id ?? null)) payload.author_id = nextAuthorId
  }

  if (canEdit('series_index')) {
    const nextIndex = indexNumber.value ?? null
    if (nextIndex !== (novel.series_index ?? null)) payload.series_index = nextIndex
  }

  if (canEdit('tags')) {
    const nextTags = [...tags.value].sort()
    const prevTags = [...(novel.tags ?? [])].sort()
    if (nextTags.join('\u0000') !== prevTags.join('\u0000')) payload.tags = tags.value
  }

  if (canEdit('create_time')) {
    const nextDate = createTime.value.trim()
    if (nextDate !== (novel.create_time ?? '').slice(0, 10)) {
      payload.create_time = nextDate || null
    }
  }

  if (canEdit('caption')) {
    // 去首尾空白后与初值比：服务端也做同一套 `_text` 归一，两边口径一致才
    // 不会出现"看着没变却提交了"或"看着变了却没提交"。
    const nextCaption = caption.value.trim()
    // 空串提交 null（清空）：服务端把空串与 null 都落成 NULL，这里显式给 null
    // 让 PATCH 的三态在线上就是"清空"，不必依赖服务端兜底。
    if (nextCaption !== (novel.caption ?? '')) {
      payload.caption = nextCaption || null
    }
  }

  return payload
}

const hasChanges = computed(() => Object.keys(buildPayload()).length > 0)
// 同步期间禁用保存：两个请求写的是同一本书，谁后回来谁的数据就是卡片上的值，
// 让它们互斥就不必赌响应顺序。
const confirmDisabled = computed(
  () => !!validationError.value || !hasChanges.value || syncing.value || deleting.value,
)

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
  // 与同步、删除互斥：不能同时飞两个请求。
  if (!novel || loading.value || syncing.value || deleting.value || confirmDisabled.value) return

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

/**
 * 「同步」：按 pixiv id 重新抓取这本小说的元数据。
 *
 * 同步会**覆盖用户在表单里未保存的改动**（初值重装），这是刻意的：按钮的语义
 * 就是"以来源为准"，半新半旧的表单才是更难解释的状态。执行期间按钮禁用防连点，
 * 失败与保存失败走同一条 `error`（父组件已有 toast）。
 */
async function handleSync() {
  const novel = props.novel
  // 与保存、删除互斥（同一次写入不能同时飞两个请求），且只有 pixiv 书才有来源可抓。
  if (!novel || !canSync.value || syncing.value || loading.value || deleting.value) return

  syncing.value = true
  try {
    const updated = await novelApi.syncNovel(novel.id)
    loadFrom(updated)
    emit('updated', updated)
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '同步失败'))
  } finally {
    syncing.value = false
  }
}

/**
 * 「删除」：删除单本书籍及其本地文件。
 *
 * 弹出原生确认框防止误触；与保存、同步互斥；删除成功后通知父组件移除卡片并关闭弹窗。
 */
async function handleDelete() {
  const novel = props.novel
  if (!novel || deleting.value || loading.value || syncing.value) return

  const confirmMsg = `确定要删除小说《${novel.title}》吗？\n删除操作不可撤销：小说记录与本地文件都将被移除。`
  if (!window.confirm(confirmMsg)) return

  deleting.value = true
  try {
    await novelApi.deleteNovel(novel.id)
    emit('deleted', novel.id)
    emit('close')
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '删除失败'))
  } finally {
    deleting.value = false
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

      <!-- 标题 + 作者 ID 一行：与下面「系列 + 章节号」同一套 1:窄 布局。
           作者是 **id**，名字由它解析（下面整行 hint 给结果）。
           能不能改由服务端的字段策略说了算（pixiv 书标题 / 作者 / 系列都只读）。 -->
      <div class="flex gap-3">
        <div class="flex-1 min-w-0">
          <label class="block text-sm font-medium text-gray-700" for="edit-novel-title">标题</label>
          <input
            id="edit-novel-title"
            v-model="title"
            type="text"
            :disabled="!canEdit('title')"
            data-testid="edit-title"
            class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
          />
        </div>
        <div class="w-32 shrink-0">
          <label class="block text-sm font-medium text-gray-700" for="edit-novel-author">作者 ID</label>
          <input
            id="edit-novel-author"
            v-model="authorId"
            type="text"
            inputmode="numeric"
            autocomplete="off"
            placeholder="如 1234567"
            :disabled="!canEdit('author_id')"
            data-testid="edit-author-id"
            class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
          />
        </div>
      </div>
      <!-- 解析结果：绿＝确认、灰＝无需动作、黄＝能存但名字待补、红＝拦住保存。
           只在字段可编辑时渲染——不可编辑就没有"填了会怎样"可言，空 <p> 也不该
           占一行高度（v-if 收掉整行，而不是留个空壳）。 -->
      <p
        v-if="canEdit('author_id') && authorHint.text"
        class="-mt-2 text-xs"
        :class="{
          'text-gray-400': authorHint.tone === 'muted',
          'text-green-600': authorHint.tone === 'ok',
          'text-amber-600': authorHint.tone === 'warn',
          'text-red-600': authorHint.tone === 'error',
        }"
        data-testid="edit-author-hint"
      >
        {{ authorHint.text }}
      </p>

      <div class="flex gap-3">
        <div class="flex-1 min-w-0">
          <label class="block text-sm font-medium text-gray-700" for="edit-novel-series">系列 ID</label>
          <input
            id="edit-novel-series"
            v-model="seriesId"
            type="text"
            inputmode="numeric"
            autocomplete="off"
            :disabled="!canEdit('series_id')"
            placeholder="如 123456"
            data-testid="edit-series-id"
            class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
          />
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
      <!-- 与作者那一行的 hint 对称：只有可编辑的系列 ID 才有"填了会解析成什么"。 -->
      <p
        v-if="canEdit('series_id') && seriesHint.text"
        class="-mt-2 text-xs"
        :class="{
          'text-gray-400': seriesHint.tone === 'muted',
          'text-green-600': seriesHint.tone === 'ok',
          'text-amber-600': seriesHint.tone === 'warn',
          'text-red-600': seriesHint.tone === 'error',
        }"
        data-testid="edit-series-hint"
      >
        {{ seriesHint.text }}
      </p>

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
      </div>

      <div>
        <label class="block text-sm font-medium text-gray-700" for="edit-novel-caption">简介</label>
        <!-- 置灰依据同样是服务端下发的 editable_fields（2026-09-22 前这里写死
             disabled）：本地书可编辑（没有来源会覆盖它，上传时这一列是 NULL），
             pixiv 书仍只读——它是来源的展示性字段，提交会 400。 -->
        <textarea
          id="edit-novel-caption"
          v-model="caption"
          rows="3"
          :disabled="!canEdit('caption')"
          :maxlength="MAX_CAPTION"
          data-testid="edit-caption"
          class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm resize-none disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
        ></textarea>
      </div>

      <p v-if="validationError" class="text-xs text-red-600" data-testid="edit-validation-error">
        {{ validationError }}
      </p>
    </div>

    <!-- 「同步」与「删除」等次要动作放 DOM 末位：
         BaseModal 的 footer 是 row-reverse 且把 extra-buttons slot 放在 DOM 末位，
         所以渲染在最左（左下角）。
         「删除」在最左端（DOM 最后），「同步」（仅 pixiv 书有）在删除右侧。
         type="button"：footer 在 <form> 里，避免触发默认 submit。 -->
    <template #extra-buttons>
      <button
        v-if="canSync"
        type="button"
        :disabled="syncing || loading || deleting"
        data-testid="edit-sync"
        class="mt-3 w-full inline-flex justify-center items-center gap-1 rounded-md border border-gray-300 shadow-sm px-4 py-2 bg-white text-base font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        @click="handleSync"
      >
        <span
          v-if="syncing"
          class="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-300 border-t-gray-600"
          aria-hidden="true"
        ></span>
        {{ syncing ? '同步中…' : '同步' }}
      </button>

      <button
        type="button"
        :disabled="deleting || loading || syncing"
        data-testid="edit-delete"
        class="mt-3 w-full inline-flex justify-center items-center gap-1 rounded-md border border-red-200 shadow-sm px-4 py-2 bg-red-50 text-base font-medium text-red-600 hover:bg-red-100 hover:text-red-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500 sm:mt-0 sm:w-auto sm:text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        @click="handleDelete"
      >
        <span
          v-if="deleting"
          class="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-red-300 border-t-red-600"
          aria-hidden="true"
        ></span>
        {{ deleting ? '删除中…' : '删除' }}
      </button>
    </template>
  </BaseModal>
</template>
