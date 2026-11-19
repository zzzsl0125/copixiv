<script setup lang="ts">
import { X } from '@lucide/vue'
import {
  DialogClose,
  DialogContent,
  DialogOverlay,
  DialogRoot,
  DialogTitle,
} from 'reka-ui'

defineProps<{
  open: boolean
  title: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

function onOpenChange(nextOpen: boolean) {
  if (!nextOpen) emit('close')
}
</script>

<template>
  <div v-if="open" class="fixed inset-0 z-50 overflow-hidden">
    <DialogRoot :open="open" @update:open="onOpenChange">
      <DialogOverlay
        data-testid="slide-over-overlay"
        aria-hidden="true"
        class="absolute inset-0 bg-gray-500/75 transition-opacity"
        @click="emit('close')"
      />
      <div class="pointer-events-none fixed inset-y-0 right-0 flex max-w-full pl-0 sm:pl-10">
        <DialogContent
          data-testid="slide-over-drawer"
          :aria-describedby="undefined"
          class="pointer-events-auto w-screen max-w-4xl bg-white shadow-xl flex h-full flex-col focus:outline-none"
          @escape-key-down="emit('close')"
        >
          <div class="px-4 py-3 sm:px-6 bg-gray-50 border-b border-gray-200 flex justify-between items-center shrink-0">
            <DialogTitle as="h3" class="text-lg font-medium text-gray-900">
              {{ title }}
            </DialogTitle>
            <DialogClose
              type="button"
              class="rounded-md p-1 text-gray-400 hover:text-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer"
              aria-label="关闭面板"
              @click="emit('close')"
            >
              <X class="h-5 w-5" aria-hidden="true" />
            </DialogClose>
          </div>
          <div class="relative flex-1 overflow-hidden">
            <slot></slot>
          </div>
        </DialogContent>
      </div>
    </DialogRoot>
  </div>
</template>
