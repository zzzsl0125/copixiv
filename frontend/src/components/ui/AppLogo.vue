<script setup lang="ts">
defineEmits<{ (e: 'click'): void }>()
</script>

<template>
  <button
    type="button"
    class="text-2xl font-bold text-gray-900 cursor-pointer hover:text-blue-600 transition-colors m-0 bg-transparent border-0 p-0"
    aria-label="Copixiv 首页"
    @click="$emit('click')"
  >
    Copixiv
  </button>
</template>
