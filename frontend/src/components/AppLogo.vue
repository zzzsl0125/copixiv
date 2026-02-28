<script setup lang="ts">
defineProps<{
  className?: string;
}>();

defineEmits<{
  (e: 'click'): void;
}>();
</script>

<template>
  <h1
    :class="[
      'text-2xl font-bold text-gray-900 cursor-pointer hover:text-blue-600 transition-colors m-0',
      className || ''
    ]"
    @click="$emit('click')"
  >
    Copixiv
  </h1>
</template>
