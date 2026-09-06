<script setup lang="ts">
import { CheckCircle, XCircle, Info, AlertTriangle } from '@lucide/vue'
import type { Toast } from '../../composables'

defineProps<{ toasts: Toast[] }>()
</script>

<template>
  <div class="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm w-[calc(100vw-2rem)]" role="status" aria-live="polite">
    <div
      v-for="toast in toasts"
      :key="toast.id"
      :class="[
        'flex items-center gap-3 px-4 py-3 rounded-lg shadow-lg border text-sm transition-all',
        toast.type === 'success' && 'bg-green-50 border-green-200 text-green-800',
        toast.type === 'error' && 'bg-red-50 border-red-200 text-red-800',
        toast.type === 'warning' && 'bg-yellow-50 border-yellow-200 text-yellow-800',
        toast.type === 'info' && 'bg-blue-50 border-blue-200 text-blue-800',
      ]"
    >
      <CheckCircle v-if="toast.type === 'success'" class="w-5 h-5 shrink-0 text-green-500" />
      <XCircle v-if="toast.type === 'error'" class="w-5 h-5 shrink-0 text-red-500" />
      <AlertTriangle v-if="toast.type === 'warning'" class="w-5 h-5 shrink-0 text-yellow-500" />
      <Info v-if="toast.type === 'info'" class="w-5 h-5 shrink-0 text-blue-500" />
      <span class="flex-1">{{ toast.message }}</span>
    </div>
  </div>
</template>
