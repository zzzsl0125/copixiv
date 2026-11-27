<script setup lang="ts">
import { computed } from 'vue'
import { useSources } from '../../composables/useSources'
import { formatNumber } from '../../lib/utils'
import type { Novel, TagPreference } from '../../types'

export interface NovelStateChange {
  id: number
  field: 'is_favourite' | 'is_special_follow'
  value: boolean
}

const props = defineProps<{
  novel: Novel
  isActive: boolean
  tagPreferenceMap?: ReadonlyMap<string, TagPreference['preference']>
  batchMode?: boolean
  batchSelected?: boolean
}>()

const emit = defineEmits<{
  search: [type: 'author' | 'series' | 'tag', value: string | number]
  'toggle-active': [id: number | string]
  'toggle-batch-select': [id: number]
  'toggle-favourite': [novel: Novel]
  'toggle-follow': [novel: Novel]
  download: [novel: Novel]
  edit: [novel: Novel]
}>()

const { getBadge } = useSources()

const showMobileActions = computed(() => props.isActive)
const hasEpubReady = computed(() => props.novel.has_epub === 2)
const isMissingSource = computed(() => props.novel.has_epub === 4)
const sourceBadge = computed(() => getBadge(props.novel.source))

const badges = computed(() => {
  const list: { key: string; label: string; class: string; title: string }[] = []
  if (sourceBadge.value) {
    list.push({
      key: props.novel.source,
      label: sourceBadge.value.label,
      class: sourceBadge.value.class,
      title: sourceBadge.value.title,
    })
  }
  if (hasEpubReady.value) {
    list.push({
      key: 'epub',
      label: 'EPUB',
      class: 'bg-green-100 text-green-800',
      title: '已生成 EPUB',
    })
  }
  if (isMissingSource.value) {
    list.push({
      key: 'missing',
      label: '缺文件',
      class: 'bg-red-100 text-red-800',
      title: '本地正文文件已丢失（has_epub=4），需重新下载',
    })
  }
  return list
})

const handleCardClick = (e: Event) => {
  e.stopPropagation()
  if (props.batchMode) {
    emit('toggle-batch-select', props.novel.id)
    return
  }
  emit('toggle-active', props.novel.id)
}

const stopPropagation = (e: Event) => {
  e.stopPropagation()
}

const handleTitleClick = (e: Event) => {
  if (props.batchMode) {
    e.preventDefault()
    e.stopPropagation()
    emit('toggle-batch-select', props.novel.id)
    return
  }
  stopPropagation(e)
}

const handleSeriesClick = (e: Event) => {
  if (!props.novel.series_id) return
  e.stopPropagation()
  emit('search', 'series', props.novel.series_id)
}

const handleAuthorClick = (e: Event) => {
  if (!props.novel.author_id) return
  e.stopPropagation()
  emit('search', 'author', props.novel.author_id)
}

const likeBorderClass = computed(() => {
  if (props.batchMode && props.batchSelected) {
    return 'ring-2 ring-blue-400 border-transparent shadow-[0_0_15px_rgba(59,130,246,0.25)]'
  }
  const likes = props.novel.like || 0
  if (likes >= 5000) return 'ring-2 ring-red-200 border-transparent shadow-[0_0_15px_rgba(239,68,68,0.15)]'
  if (likes >= 2500) return 'ring-2 ring-yellow-200 border-transparent shadow-[0_0_15px_rgba(234,179,8,0.15)]'
  if (likes >= 500) return 'ring-2 ring-green-200 border-transparent shadow-[0_0_15px_rgba(59,130,246,0.15)]'
  return 'border-gray-100'
})

const normalizedTagPreferenceMap = computed(() => {
  const map = props.tagPreferenceMap
  if (!map || map.size === 0) return null
  const next = new Map<string, TagPreference['preference']>()
  for (const [k, v] of map.entries()) {
    next.set(k.toLowerCase(), v)
  }
  return next
})

const getTagClass = (tag: string) => {
  const preference =
    props.tagPreferenceMap?.get(tag) ?? normalizedTagPreferenceMap.value?.get(tag.toLowerCase())
  if (preference === 'favourite') return 'bg-blue-100 text-blue-800 font-bold'
  if (preference === 'blocked') return 'line-through text-red-400 bg-gray-50'
  return 'bg-gray-100 text-gray-800'
}
</script>

<template>
  <div
    class="relative group bg-white rounded-xl shadow-sm hover:shadow-xl duration-300 transition-[box-shadow,border-color] overflow-hidden flex flex-col border cursor-pointer"
    :class="likeBorderClass"
    @click="handleCardClick"
  >
    <button
      type="button"
      class="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-10 focus:px-3 focus:py-1.5 focus:rounded-md focus:bg-white focus:border focus:border-gray-300 focus:text-sm"
      :aria-expanded="isActive"
      @click="handleCardClick"
    >
      {{ batchMode ? (batchSelected ? '取消勾选' : '勾选') : (isActive ? '收起操作' : '展开操作') }}
    </button>
    <div class="p-5 grow flex flex-col">
      <div class="mb-1">
        <a
          v-if="novel.external_url"
          :href="novel.external_url"
          target="_blank"
          rel="noopener noreferrer"
          class="text-lg font-bold text-gray-900 hover:text-blue-600 inline-block"
          :title="novel.title"
          @click="handleTitleClick"
        >
          {{ novel.title }}
        </a>
        <span
          v-else
          class="text-lg font-bold text-gray-900 inline-block"
          :title="novel.title"
          @click="handleTitleClick"
        >
          {{ novel.title }}
        </span>
      </div>
      <div v-if="novel.series_name || novel.series_id" class="text-sm text-gray-500 mb-1 flex items-start">
        <span class="shrink-0 whitespace-nowrap">系列：</span>
        <span
          :class="[{ 'cursor-pointer hover:text-blue-600': novel.series_id }, 'wrap-break-word']"
          @click="handleSeriesClick"
        >
          {{ novel.series_name || `系列 #${novel.series_id}` }}<template v-if="novel.series_index"> #{{ novel.series_index }}</template>
        </span>
      </div>
      <div class="text-sm text-gray-500 mb-3 flex items-start">
        <span class="shrink-0 whitespace-nowrap">作者：</span>
        <span
          :class="[{ 'cursor-pointer hover:text-blue-600': novel.author_id }, 'wrap-break-word']"
          @click="handleAuthorClick"
        >
          {{ novel.author_name || '未知' }}
        </span>
      </div>

      <div class="flex flex-wrap gap-1">
        <span
          v-for="(tag, idx) in novel.tags || []"
          :key="idx"
          class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium cursor-pointer hover:bg-gray-200 hover:text-blue-600 transition-colors"
          :class="getTagClass(tag)"
          @click="stopPropagation($event); emit('search', 'tag', tag)"
        >
          {{ tag }}
        </span>
      </div>
    </div>

    <div class="px-5 py-4 bg-gray-50 border-t border-gray-100 flex flex-col gap-3 relative overflow-hidden">
      <div
        class="flex justify-between items-center text-xs text-gray-500 transition-opacity duration-300"
        :class="[batchMode ? 'opacity-100' : (showMobileActions ? 'opacity-0' : 'opacity-100 group-hover:opacity-0')]"
      >
        <div class="flex items-center gap-3">
          <span class="flex items-center gap-1" title="喜爱">❤️ {{ formatNumber(novel.like) }}</span>
          <span class="flex items-center gap-1" title="字数">📝 {{ formatNumber(novel.text) }}</span>
          <span class="flex items-center gap-1" title="日期">
            📅 {{ novel.create_time ? novel.create_time.substring(0, 10) : '—' }}
          </span>
        </div>
        <div v-if="badges.length" class="flex items-center gap-1 shrink-0" data-testid="novel-badges">
          <span
            v-for="badge in badges"
            :key="badge.key"
            class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold"
            :class="badge.class"
            :title="badge.title"
            :data-testid="`badge-${badge.key}`"
          >
            {{ badge.label }}
          </span>
        </div>
      </div>

      <div
        v-if="!batchMode"
        class="absolute inset-0 bg-white/95 border-t border-gray-100 flex items-center px-3 py-2 gap-2 transition-transform duration-300"
        :class="[
          'translate-y-full',
          'group-hover:translate-y-0',
          showMobileActions ? 'translate-y-0!' : '',
          showMobileActions ? 'backdrop-blur-sm' : 'group-hover:backdrop-blur-sm',
        ]"
        @click="stopPropagation"
      >
        <div class="flex-1 flex gap-2 h-full">
          <button
            class="flex-1 flex items-center justify-center text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors cursor-pointer"
            data-testid="novel-download"
            @click="stopPropagation($event); emit('download', novel)"
          >
            下载
          </button>
          <button
            class="flex-1 flex items-center justify-center text-xs font-medium rounded-lg transition-colors cursor-pointer"
            :class="novel.is_favourite ? 'text-white bg-red-500 hover:bg-red-600' : 'text-gray-700 bg-gray-100 hover:bg-gray-200'"
            data-testid="novel-favourite"
            @click="stopPropagation($event); emit('toggle-favourite', novel)"
          >
            {{ novel.is_favourite ? '已收藏' : '收藏' }}
          </button>
          <button
            v-if="novel.author_id != null"
            class="flex-1 flex items-center justify-center text-xs font-medium rounded-lg transition-colors cursor-pointer"
            :class="novel.is_special_follow ? 'text-white bg-purple-500 hover:bg-purple-600' : 'text-gray-700 bg-gray-100 hover:bg-gray-200'"
            data-testid="novel-follow"
            @click="stopPropagation($event); emit('toggle-follow', novel)"
          >
            {{ novel.is_special_follow ? '已追更' : '追更' }}
          </button>
          <button
            class="flex-1 flex items-center justify-center text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors cursor-pointer"
            data-testid="novel-edit"
            @click="stopPropagation($event); emit('edit', novel)"
          >
            编辑
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
