<script setup lang="ts">
import { computed, ref, type PropType } from 'vue'
import { novelApi } from '../../api'
import { getApiErrorMessage } from '../../api/errors'
import { downloadBlob, filenameFromContentDisposition, formatNumber } from '../../lib/utils'
import { useToast } from '../../composables'
import type { Novel, TagPreference } from '../../types'

export interface NovelStateChange {
  id: number
  field: 'is_favourite' | 'is_special_follow'
  value: number
}

const props = defineProps({
  novel: { type: Object as PropType<Novel>, required: true },
  isActive: { type: Boolean, required: true },
  tagPreferences: { type: Array as PropType<TagPreference[]>, default: () => [] },
})

const emit = defineEmits<{
  (e: 'search', type: 'author' | 'series' | 'tag', value: string | number): void
  (e: 'toggle-active', id: number | string): void
  (e: 'state-changed', payload: NovelStateChange): void
}>()

const toast = useToast()
const downloading = ref(false)
const showMobileActions = computed(() => props.isActive)
const hasEpubReady = computed(() => props.novel.has_epub === 2)

const toggleActions = (e: Event) => {
  e.stopPropagation()
  emit('toggle-active', props.novel.id)
}

const stopPropagation = (e: Event) => { e.stopPropagation() }

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

const handleToggleFavourite = async (e: Event) => {
  e.stopPropagation()
  try {
    await novelApi.toggleFavourite(props.novel.id)
    emit('state-changed', {
      id: props.novel.id,
      field: 'is_favourite',
      value: props.novel.is_favourite ? 0 : 1,
    })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '收藏操作失败'))
  }
}

const handleToggleFollow = async (e: Event) => {
  e.stopPropagation()
  if (!props.novel.author_id) return
  try {
    await novelApi.toggleSpecialFollow(props.novel.author_id)
    emit('state-changed', {
      id: props.novel.id,
      field: 'is_special_follow',
      value: props.novel.is_special_follow ? 0 : 1,
    })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '追更操作失败'))
  }
}

const handleDownload = async (e: Event) => {
  e.stopPropagation()
  if (downloading.value) return
  downloading.value = true
  try {
    const response = await novelApi.downloadNovel(
      props.novel.id,
      hasEpubReady.value ? 'epub' : 'txt',
    )
    const blob = response.data as Blob
    const filename = filenameFromContentDisposition(
      response.headers['content-disposition'] as string | undefined,
      `${props.novel.id}.txt`,
    )
    downloadBlob(blob, filename)
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '下载失败'))
  } finally {
    downloading.value = false
  }
}

const likeBorderClass = computed(() => {
  const likes = props.novel.like || 0
  if (likes >= 5000) return 'ring-2 ring-red-200 border-transparent shadow-[0_0_15px_rgba(239,68,68,0.15)]'
  if (likes >= 2500) return 'ring-2 ring-yellow-200 border-transparent shadow-[0_0_15px_rgba(234,179,8,0.15)]'
  if (likes >= 500) return 'ring-2 ring-green-200 border-transparent shadow-[0_0_15px_rgba(59,130,246,0.15)]'
  return 'border-gray-100'
})

const getTagClass = (tag: string) => {
  const preference = props.tagPreferences.find(p => p.tag === tag)?.preference
  if (preference === 'favourite') return 'bg-blue-100 text-blue-800 font-bold'
  if (preference === 'blocked') return 'line-through text-red-400 bg-gray-50'
  return 'bg-gray-100 text-gray-800'
}
</script>

<template>
  <div
    class="relative group bg-white rounded-xl shadow-sm hover:shadow-xl transition-all duration-300 overflow-hidden flex flex-col border cursor-pointer"
    :class="likeBorderClass"
    @click="toggleActions"
  >
    <button
      type="button"
      class="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-10 focus:px-3 focus:py-1.5 focus:rounded-md focus:bg-white focus:border focus:border-gray-300 focus:text-sm"
      :aria-expanded="isActive"
      @click="toggleActions"
    >
      {{ isActive ? '收起操作' : '展开操作' }}
    </button>
    <div class="p-5 grow flex flex-col">
      <div class="mb-1">
        <a
          :href="`https://www.pixiv.net/novel/show.php?id=${novel.id}`"
          target="_blank"
          rel="noopener noreferrer"
          class="text-lg font-bold text-gray-900 hover:text-blue-600 inline-block"
          :title="novel.title"
          @click="stopPropagation"
        >
          {{ novel.title }}
        </a>
      </div>
      <div v-if="novel.series_name" class="text-sm text-gray-500 mb-1 flex items-start">
        <span class="shrink-0 whitespace-nowrap">系列：</span>
        <span
          :class="[{ 'cursor-pointer hover:text-blue-600': novel.series_id }, 'wrap-break-word']"
          @click="handleSeriesClick"
        >
          {{ novel.series_name }}<template v-if="novel.series_index"> #{{ novel.series_index }}</template>
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
        <span v-for="(tag, idx) in novel.tags || []" :key="idx"
          class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium cursor-pointer hover:bg-gray-200 hover:text-blue-600 transition-colors"
          :class="getTagClass(tag)"
          @click="stopPropagation($event); emit('search', 'tag', tag)"
        >
          {{ tag }}
        </span>
      </div>
    </div>

    <div class="px-5 py-4 bg-gray-50 border-t border-gray-100 flex flex-col gap-3 relative overflow-hidden">
      <div class="flex justify-between items-center text-xs text-gray-500 transition-opacity duration-300"
           :class="[showMobileActions ? 'opacity-0' : 'opacity-100 group-hover:opacity-0']">
        <div class="flex items-center gap-3">
          <span class="flex items-center gap-1" title="喜爱">❤️ {{ formatNumber(novel.like) }}</span>
          <span class="flex items-center gap-1" title="字数">📝 {{ formatNumber(novel.text) }}</span>
          <span class="flex items-center gap-1" title="日期">📝 {{ (novel.create_time || '2016-01-01').substring(0, 10) }}</span>
        </div>
        <span v-if="hasEpubReady" class="px-1.5 py-0.5 rounded bg-green-100 text-green-800 text-[10px] font-bold">EPUB</span>
      </div>

      <div
        class="absolute inset-0 bg-white/95 backdrop-blur-sm border-t border-gray-100 flex items-center px-3 py-2 gap-2 transition-transform duration-300"
        :class="['translate-y-full', 'group-hover:translate-y-0', showMobileActions ? 'translate-y-0!' : '']"
        @click="stopPropagation"
      >
        <div class="flex-1 flex gap-2 h-full">
          <button
            class="flex-1 flex items-center justify-center text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-wait"
            :disabled="downloading"
            @click="handleDownload"
          >
            {{ downloading ? '下载中…' : '下载' }}
          </button>
          <button
            class="flex-1 flex items-center justify-center text-xs font-medium rounded-lg transition-colors cursor-pointer"
            :class="novel.is_favourite ? 'text-white bg-red-500 hover:bg-red-600' : 'text-gray-700 bg-gray-100 hover:bg-gray-200'"
            @click="handleToggleFavourite"
          >
            {{ novel.is_favourite ? '已收藏' : '收藏' }}
          </button>
          <button
            class="flex-1 flex items-center justify-center text-xs font-medium rounded-lg transition-colors cursor-pointer"
            :class="novel.is_special_follow ? 'text-white bg-purple-500 hover:bg-purple-600' : 'text-gray-700 bg-gray-100 hover:bg-gray-200'"
            @click="handleToggleFollow"
          >
            {{ novel.is_special_follow ? '已追更' : '追更' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
