<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { type InfiniteData, useInfiniteQuery, useQueryClient } from '@tanstack/vue-query'
import { AlertTriangle, ExternalLink, RefreshCw, RotateCcw } from '@lucide/vue'
import type { FailedNovel, FailedNovelListResponse } from '../types'
import { FAILED_NOVELS_PAGE_SIZE, failedNovelApi, getApiErrorMessage, queryKeys } from '../api'
import { useConfirm, useTaskStatus, useToast } from '../composables'
import PageHeader from '../components/features/PageHeader.vue'
import DraggableTable, { type TableColumn } from '../components/features/DraggableTable.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import ErrorAlert from '../components/ui/ErrorAlert.vue'

defineOptions({ inheritAttrs: false })
defineEmits<{ (e: 'toggle-sidebar'): void }>()

const toast = useToast()
const { confirm } = useConfirm()
const queryClient = useQueryClient()
const { isBusy, notifyTaskEnqueued } = useTaskStatus()

const retryingIds = ref<Set<number>>(new Set())
const retryingAll = ref(false)
const resettingAll = ref(false)

const listQuery = useInfiniteQuery({
  queryKey: queryKeys.failedNovels(),
  queryFn: ({ pageParam, signal }) =>
    failedNovelApi.list(pageParam, FAILED_NOVELS_PAGE_SIZE, signal),
  initialPageParam: 0,
  getNextPageParam: (lastPage, allPages) => {
    const loaded = allPages.reduce((sum, p) => sum + p.items.length, 0)
    return loaded < lastPage.total ? loaded : undefined
  },
})

watch(
  () => listQuery.error.value,
  (err) => {
    if (err) toast.error(getApiErrorMessage(err, '加载失败记录失败'))
  },
)

const items = computed<FailedNovel[]>(
  () => listQuery.data.value?.pages.flatMap((p) => p.items) ?? [],
)
const total = computed(() => {
  const pages = listQuery.data.value?.pages
  return pages && pages.length > 0 ? pages[pages.length - 1]!.total : 0
})
const hasMore = computed(() => listQuery.hasNextPage.value)
const loading = computed(() => listQuery.isPending.value)
const loadingMore = computed(() => listQuery.isFetchingNextPage.value)
const loadError = computed(() =>
  listQuery.error.value ? getApiErrorMessage(listQuery.error.value, '加载失败记录失败') : null,
)

const failedTimesCutoff = 3 // 与后端 _MAX_RETRIES 一致：达到后自动跳过
const pixivNovelUrl = (id: number) => `https://www.pixiv.net/novel/show.php?id=${id}`
const anyRetrying = computed(() => retryingAll.value || retryingIds.value.size > 0)

const columns: TableColumn[] = [
  { key: 'novel', label: '小说', tdClass: '' },
  { key: 'failed_times', label: '失败次数' },
  { key: 'last_failed_at', label: '最近失败时间', tdClass: 'text-sm text-gray-500 whitespace-nowrap' },
  { key: 'error_message', label: '错误原因', tdClass: 'text-sm text-gray-500 max-w-md' },
  { key: 'actions', label: '操作', align: 'right' },
]

function loadMore() {
  if (loading.value || loadingMore.value || !hasMore.value) return
  void listQuery.fetchNextPage()
}

async function resetOne(novel: FailedNovel) {
  const confirmed = await confirm({
    title: '重置失败计数',
    description: `重置 #${novel.novel_id} 的失败计数？记录会保留，下次批量任务会自动重试。`,
    confirmText: '确认重置',
    variant: 'default',
  })
  if (!confirmed) return
  try {
    await failedNovelApi.resetCount(novel.novel_id)
    toast.success(`已重置 #${novel.novel_id} 的失败计数`)
    queryClient.setQueryData<InfiniteData<FailedNovelListResponse>>(
      queryKeys.failedNovels(),
      (old) => {
        if (!old) return old
        return {
          ...old,
          pages: old.pages.map((p) => ({
            ...p,
            items: p.items.map((row) =>
              row.novel_id === novel.novel_id ? { ...row, failed_times: 0 } : row,
            ),
          })),
        }
      },
    )
  } catch (err) {
    toast.error(getApiErrorMessage(err, '重置失败'))
  }
}

async function retryOne(novel: FailedNovel) {
  if (anyRetrying.value || isBusy('failed_retry')) return
  retryingIds.value = new Set([novel.novel_id])
  try {
    const { task_id } = await failedNovelApi.retry([novel.novel_id])
    notifyTaskEnqueued('failed_retry')
    toast.success(`已入队重试 #${novel.novel_id}（任务 #${task_id}，可在任务管理查看进度）`)
  } catch (err) {
    toast.error(getApiErrorMessage(err, '入队重试失败'))
  } finally {
    retryingIds.value = new Set()
  }
}

async function retryAll() {
  if (anyRetrying.value || isBusy('failed_retry') || total.value === 0) return
  const confirmed = await confirm({
    title: '全部重试',
    description: `入队重试全部 ${total.value} 条失败记录？（所有失败项，不只当前页）`,
    confirmText: '确认重试',
    variant: 'default',
  })
  if (!confirmed) return
  retryingAll.value = true
  try {
    const { task_id, matched } = await failedNovelApi.retryAll()
    notifyTaskEnqueued('failed_retry')
    toast.success(`已入队重试 ${matched} 条记录（任务 #${task_id}，可在任务管理查看进度）`)
  } catch (err) {
    toast.error(getApiErrorMessage(err, '入队重试失败'))
  } finally {
    retryingAll.value = false
  }
}

async function resetAll() {
  if (resettingAll.value) return
  const confirmed = await confirm({
    title: '全部重置计数',
    description: `重置全部 ${total.value} 条失败记录的计数？（记录保留，全部解封，下次批量任务自动重试）`,
    confirmText: '确认重置',
    variant: 'danger',
  })
  if (!confirmed) return
  resettingAll.value = true
  try {
    await failedNovelApi.resetAll()
    toast.success('已重置全部失败计数')
    queryClient.setQueryData<InfiniteData<FailedNovelListResponse>>(
      queryKeys.failedNovels(),
      (old) => {
        if (!old) return old
        return {
          ...old,
          pages: old.pages.map((p) => ({
            ...p,
            items: p.items.map((row) => ({ ...row, failed_times: 0 })),
          })),
        }
      },
    )
  } catch (err) {
    toast.error(getApiErrorMessage(err, '重置失败'))
  } finally {
    resettingAll.value = false
  }
}
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
            class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
            :disabled="anyRetrying || isBusy('failed_retry') || total === 0"
            @click="retryAll"
          >
            <RefreshCw class="mr-1.5 h-4 w-4" />
            {{ retryingAll ? '入队中…' : '全部重试' }}
          </button>
          <button
            class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
            :disabled="resettingAll || total === 0"
            @click="resetAll"
          >
            <RotateCcw class="mr-1.5 h-4 w-4" />
            {{ resettingAll ? '重置中…' : '全部重置计数' }}
          </button>
        </div>
      </div>

      <ErrorAlert :message="loadError" :retryable="true" @retry="void listQuery.refetch()" />

      <EmptyState
        v-if="!loading && items.length === 0"
        :loading="false"
        title="暂无失败记录"
        description="当前没有下载失败的小说记录"
      />

      <DraggableTable
        v-else
        :items="items"
        :columns="columns"
        item-key="novel_id"
        :disabled="true"
        :loading="loading"
        empty-text="暂无失败记录"
      >
        <template #novel="{ item }">
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
        </template>
        <template #failed_times="{ item }">
          <span
            class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium"
            :class="item.failed_times >= failedTimesCutoff ? 'bg-red-100 text-red-700' : 'bg-gray-100 text-gray-600'"
          >
            <AlertTriangle v-if="item.failed_times >= failedTimesCutoff" class="mr-1 h-3 w-3" />
            {{ item.failed_times }}
          </span>
        </template>
        <template #last_failed_at="{ item }">
          {{ item.last_failed_at || '—' }}
        </template>
        <template #error_message="{ item }">
          <span class="block truncate" :title="item.error_message ?? undefined">
            {{ item.error_message || '—' }}
          </span>
        </template>
        <template #actions="{ item }">
          <div class="flex justify-end gap-2">
            <button
              class="inline-flex items-center px-2.5 py-1 text-xs font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
              :disabled="anyRetrying || isBusy('failed_retry')"
              @click="retryOne(item)"
            >
              <RefreshCw
                class="mr-1 h-3.5 w-3.5"
                :class="{ 'animate-spin': retryingIds.has(item.novel_id) }"
              />
              重试
            </button>
            <button
              class="inline-flex items-center px-2.5 py-1 text-xs font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 cursor-pointer"
              @click="resetOne(item)"
            >
              <RotateCcw class="mr-1 h-3.5 w-3.5" />
              重置计数
            </button>
          </div>
        </template>
        <template v-if="hasMore" #footer>
          <div class="border-t border-gray-200 px-4 py-3 text-center">
            <button
              class="inline-flex items-center px-4 py-2 text-sm font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 cursor-pointer"
              :disabled="loadingMore"
              @click="loadMore"
            >
              <RefreshCw class="mr-1.5 h-4 w-4" :class="{ 'animate-spin': loadingMore }" />
              {{ loadingMore ? '加载中…' : `加载更多（已显示 ${items.length} / ${total}）` }}
            </button>
          </div>
        </template>
      </DraggableTable>
    </div>
  </div>
</template>
