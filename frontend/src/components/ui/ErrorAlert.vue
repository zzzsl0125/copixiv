<script setup lang="ts">
import { AlertCircle } from '@lucide/vue'

defineProps<{
  message: string | null | undefined
  retryable?: boolean
}>()

defineEmits<{
  (e: 'retry'): void
}>()
</script>

<template>
  <div
    v-if="message"
    class="bg-red-50 border-l-4 border-red-400 p-4 mb-6 rounded-md flex items-center justify-between gap-4"
    role="alert"
  >
    <div class="flex items-center gap-2 min-w-0">
      <AlertCircle class="w-5 h-5 text-red-500 shrink-0" aria-hidden="true" />
      <p class="text-sm text-red-700 break-words">{{ message }}</p>
    </div>
    <button
      v-if="retryable"
      type="button"
      class="shrink-0 text-xs font-medium text-red-700 hover:text-red-900 underline cursor-pointer"
      @click="$emit('retry')"
    >
      重试
    </button>
  </div>
</template>
