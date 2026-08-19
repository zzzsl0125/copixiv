<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { RefreshCw, RotateCcw, ExternalLink, AlertTriangle } from '@lucide/vue'
import type { FailedNovel } from '../types'
import { failedNovelApi } from '../api/failedNovels'
import { getApiErrorMessage } from '../api/errors'
import { useToast } from '../composables'
import PageHeader from '../components/features/PageHeader.vue'
import EmptyState from '../components/ui/EmptyState.vue'

defineOptions({ inheritAttrs: false })
defineEmits<{ (e: 'toggle-sidebar'): void }>()

const toast = useToast()

const items = ref<FailedNovel[]>([])
const total = ref(0)
const loading = ref(false)
const loadingMore = ref(false)
const retryingIds = ref<Set<number>>(new Set())
const retryingAll = ref(false)
const resettingAll = ref(false)
const loadSeq = ref(0)

const hasMore = computed(() => items.value.length < total.value)
const failedTimesCutoff = 3 // 与后端 _MAX_RETRIES 一致：达到后自动跳过

const pixivNovelUrl = (id: number) => `https://www.pixiv.net/novel/show.php?id=${id}`

/** 重试中（含「全部重试」）时禁用所有重试按钮。 */
const anyRetrying = computed(() => retryingAll.value || retryingIds.value.size > 0)

async function loadMore() {
  if (loading.value || loadingMore.value || !hasMore.value) return
  loadingMore.value = true
  const seq = ++loadSeq.value
  try {
    const data = await failedNovelApi.list(items.value.length)
    if (seq !== loadSeq.value) return
    items.value = [...items.value, ...data.items]
    total.value = data.total
  } catch (err) {
    toast.error(getApiErrorMessage(err, '加载失败记录失败'))
  } finally {
    if (seq === loadSeq.value) loadingMore.value = false
  }
}

async function load() {
  loading.value = true
  const seq = ++loadSeq.value
  try {
    const data = await failedNovelApi.list(0)
    if (seq !== loadSeq.value) return
    items.value = data.items
    total.value = data.total
  } catch (err) {
    toast.error(getApiErrorMessage(err, '加载失败记录失败'))
  } finally {
    if (seq === loadSeq.value) loading.value = false
  }
}

async function resetOne(novel: FailedNovel) {
  if (!window.confirm(`重置 #${novel.novel_id} 的失败计数？记录会保留，下次批量任务会自动重试。`)) return
  try {
    await failedNovelApi.resetCount(novel.novel_id)
    toast.success(`已重置 #${novel.novel_id} 的失败计数`)
    novel.failed_times = 0
  } catch (err) {
    toast.error(getApiErrorMessage(err, '重置失败'))
  }
}

async function retryOne(novel: FailedNovel) {
  if (anyRetrying.value) return
  retryingIds.value = new Set([novel.novel_id])
  try {
    const { task_id } = await failedNovelApi.retry([novel.novel_id])
    toast.success(`已入队重试 #${novel.novel_id}（任务 #${task_id}，可在任务管理查看进度）`)
  } catch (err) {
    toast.error(getApiErrorMessage(err, '入队重试失败'))
  } finally {
    retryingIds.value = new Set()
  }
}

async function retryAll() {
  if (anyRetrying.value || items.value.length === 0) return
  const ids = items.value.map((i) => i.novel_id)
  if (!window.confirm(`入队重试当前全部 ${ids.length} 条失败记录？（所有失败项，不只当前页）`)) return
  retryingAll.value = true
  try {
    const { task_id } = await failedNovelApi.retry(ids)
    toast.success(`已入队重试 ${ids.length} 条记录（任务 #${task_id}，可在任务管理查看进度）`)
  } catch (err) {
    toast.error(getApiErrorMessage(err, '入队重试失败'))
  } finally {
    retryingAll.value = false
  }
}

async function resetAll() {
  if (resettingAll.value) return
  if (!window.confirm(`重置全部 ${total.value} 条失败记录的计数？（记录保留，全部解封，下次批量任务自动重试）`)) return
  resettingAll.value = true
  try {
    await failedNovelApi.resetAll()
    toast.success('已重置全部失败计数')
    items.value.forEach((i) => { i.failed_times = 0 })
  } catch (err) {
    toast.error(getApiErrorMessage(err, '重置失败'))
  } finally {
    resettingAll.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="min-h-screen bg-gray-50">
    <PageHeader title="失败记录" @toggle-sidebar="$emit('toggle-sidebar')" />

    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
      <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
        <p class="text-sm text-gray-600">
          共 <span class="font-medium">{{ total }}</span> 条失败记录 ·
          失败 <span class="font-medium">{{ failedTimesCutoff }}</span> 次以上的记录会被自动跳过，重置计数后解封（记录保留）
        </p>
        <div class="flex gap-2">
          <button
            class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            :disabled="anyRetrying || items.length === 0"
            @click="retryAll"
          >
            <RefreshCw class="mr-1.5 h-4 w-4" />
            {{ retryingAll ? '入队中…' : '全部重试' }}
          </button>
          <button
            class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            :disabled="resettingAll || total === 0"
            @click="resetAll"
          >
            <RotateCcw class="mr-1.5 h-4 w-4" />
            {{ resettingAll ? '重置中…' : '全部重置计数' }}
          </button>
        </div>
      </div>

      <EmptyState v-if="!loading && items.length === 0" :loading="false" />

      <div v-else-if="items.length > 0" class="bg-white shadow-sm rounded-lg overflow-hidden">
        <div class="overflow-x-auto">
          <table class="min-w-full divide-y divide-gray-200">
            <thead class="bg-gray-50">
              <tr>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">小说</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">失败次数</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">最近失败时间</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">错误原因</th>
                <th class="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">操作</th>
              </tr>
            </thead>
            <tbody class="bg-white divide-y divide-gray-200">
              <tr v-for="item in items" :key="item.novel_id" class="hover:bg-gray-50">
                <td class="px-4 py-3">
                  <div class="flex items-start gap-2">
                    <a
                      :href="pixivNovelUrl(item.novel_id)"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="inline-flex items-center text-sm font-medium text-blue-600 hover:text-blue-800 shrink-0"
                      :title="`#${item.novel_id} · 在 Pixiv 打开`"
                    >
                      #{{ item.novel_id }}
                      <ExternalLink class="ml-1 h-3.5 w-3.5" />
                    </a>
                    <span class="text-sm text-gray-800 break-all" :title="item.title ?? undefined">
                      {{ item.title || '（无标题记录）' }}
                    </span>
                  </div>
                </td>
                <td class="px-4 py-3">
                  <span
                    class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium"
                    :class="item.failed_times >= failedTimesCutoff ? 'bg-red-100 text-red-700' : 'bg-gray-100 text-gray-600'"
                  >
                    <AlertTriangle v-if="item.failed_times >= failedTimesCutoff" class="mr-1 h-3 w-3" />
                    {{ item.failed_times }}
                  </span>
                </td>
                <td class="px-4 py-3 text-sm text-gray-500 whitespace-nowrap">{{ item.last_failed_at || '—' }}</td>
                <td class="px-4 py-3 text-sm text-gray-500 max-w-md">
                  <span class="block truncate" :title="item.error_message ?? undefined">{{ item.error_message || '—' }}</span>
                </td>
                <td class="px-4 py-3">
                  <div class="flex justify-end gap-2">
                    <button
                      class="inline-flex items-center px-2.5 py-1 text-xs font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                      :disabled="anyRetrying"
                      @click="retryOne(item)"
                    >
                      <RefreshCw class="mr-1 h-3.5 w-3.5" :class="{ 'animate-spin': retryingIds.has(item.novel_id) }" />
                      重试
                    </button>
                    <button
                      class="inline-flex items-center px-2.5 py-1 text-xs font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50"
                      @click="resetOne(item)"
                    >
                      <RotateCcw class="mr-1 h-3.5 w-3.5" />
                      重置计数
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="hasMore" class="border-t border-gray-200 px-4 py-3 text-center">
          <button
            class="inline-flex items-center px-4 py-2 text-sm font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            :disabled="loadingMore"
            @click="loadMore"
          >
            <RefreshCw class="mr-1.5 h-4 w-4" :class="{ 'animate-spin': loadingMore }" />
            {{ loadingMore ? '加载中…' : `加载更多（已显示 ${items.length} / ${total}）` }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
