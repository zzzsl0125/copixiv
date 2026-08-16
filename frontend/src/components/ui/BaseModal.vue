<template>
  <div v-if="isOpen" class="fixed inset-0 z-50 flex items-center justify-center" :aria-labelledby="titleId" role="dialog" aria-modal="true" data-testid="base-modal">
    <!-- backdrop -->
    <div class="absolute inset-0 bg-gray-500/75 transition-opacity" aria-hidden="true" @click="close"></div>
    <!-- dialog panel -->
    <div ref="panelRef" tabindex="-1" class="relative bg-white rounded-lg text-left overflow-hidden shadow-xl transform transition-all max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto focus:outline-none">
      <form @submit.prevent="confirm">
        <div class="bg-white px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
          <h3 class="text-lg leading-6 font-medium text-gray-900 mb-4" :id="titleId">
            {{ title }}
          </h3>
          <div class="space-y-4">
            <slot></slot>
          </div>
        </div>
        <div class="bg-gray-50 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
          <button type="submit" :disabled="loading || confirmDisabled" class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-blue-600 text-base font-medium text-white hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
            {{ loading ? confirmText.replace(/保存/g, '保存中').replace(/确认/g, '确认中') : confirmText }}
          </button>
          <slot name="extra-buttons"></slot>
          <button type="button" @click="close" class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 shadow-sm px-4 py-2 bg-white text-base font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm transition-colors">
            {{ cancelText }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, onUnmounted, useId } from 'vue'

const props = withDefaults(defineProps<{
  isOpen: boolean
  title: string
  loading?: boolean
  confirmText?: string
  cancelText?: string
  /** Extra disable condition for the confirm button (e.g. validation). */
  confirmDisabled?: boolean
}>(), {
  loading: false,
  confirmText: '保存',
  cancelText: '取消',
  confirmDisabled: false,
})

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'confirm'): void
}>()

const titleId = useId()
const panelRef = ref<HTMLElement | null>(null)

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

const handleKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') {
    close()
    return
  }
  if (event.key !== 'Tab' || !panelRef.value) return
  const focusable = Array.from(
    panelRef.value.querySelectorAll<HTMLElement>(FOCUSABLE),
  ).filter((el) => el.offsetParent !== null)
  if (focusable.length === 0) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

watch(() => props.isOpen, (open) => {
  if (open) {
    document.addEventListener('keydown', handleKeydown)
    nextTick(() => panelRef.value?.focus())
  } else {
    document.removeEventListener('keydown', handleKeydown)
  }
}, { immediate: true })

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
})

const close = () => {
  if (props.loading) return
  emit('close')
}

const confirm = () => {
  if (props.loading) return
  emit('confirm')
}
</script>
