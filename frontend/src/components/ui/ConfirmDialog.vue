<script setup lang="ts">
import {
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogOverlay,
  AlertDialogRoot,
  AlertDialogTitle,
} from 'reka-ui'
import { useConfirm } from '../../composables/useConfirm'

const { state, resolveConfirm } = useConfirm()

function onOpenChange(open: boolean) {
  if (!open) resolveConfirm(false)
}
</script>

<template>
  <div v-if="state.open" class="fixed inset-0 z-[60] flex items-center justify-center">
    <AlertDialogRoot :open="state.open" @update:open="onOpenChange">
      <AlertDialogOverlay
        data-testid="confirm-dialog-overlay"
        aria-hidden="true"
        class="absolute inset-0 bg-gray-500/75 transition-opacity"
      />
      <AlertDialogContent
        data-testid="confirm-dialog"
        class="relative bg-white rounded-lg text-left overflow-hidden shadow-xl transform transition-all max-w-md w-full mx-4 focus:outline-none"
      >
        <div class="bg-white px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
          <AlertDialogTitle class="text-lg leading-6 font-medium text-gray-900">
            {{ state.title }}
          </AlertDialogTitle>
          <AlertDialogDescription class="mt-2 text-sm text-gray-600 whitespace-pre-line">
            {{ state.description }}
          </AlertDialogDescription>
        </div>
        <div class="bg-gray-50 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse gap-2">
          <button
            type="button"
            data-testid="confirm-dialog-accept"
            class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 text-base font-medium text-white focus:outline-none focus:ring-2 focus:ring-offset-2 sm:w-auto sm:text-sm transition-colors cursor-pointer"
            :class="
              state.variant === 'danger'
                ? 'bg-red-600 hover:bg-red-700 focus:ring-red-500'
                : 'bg-blue-600 hover:bg-blue-700 focus:ring-blue-500'
            "
            @click="resolveConfirm(true)"
          >
            {{ state.confirmText }}
          </button>
          <AlertDialogCancel
            data-testid="confirm-dialog-cancel"
            class="mt-2 sm:mt-0 w-full inline-flex justify-center rounded-md border border-gray-300 shadow-sm px-4 py-2 bg-white text-base font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 sm:w-auto sm:text-sm transition-colors cursor-pointer"
          >
            {{ state.cancelText }}
          </AlertDialogCancel>
        </div>
      </AlertDialogContent>
    </AlertDialogRoot>
  </div>
</template>
