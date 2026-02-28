<script setup lang="ts">
import { Search } from 'lucide-vue-next';

defineProps<{
  loading: boolean;
}>();
</script>

<template>
  <div v-if="!loading" class="text-center py-20">
    <div class="inline-flex items-center justify-center w-16 h-16 rounded-full bg-gray-100 mb-4">
      <Search class="h-8 w-8 text-gray-400" />
    </div>
    <h3 class="text-lg font-medium text-gray-900">未找到相关小说</h3>
    <p class="mt-1 text-sm text-gray-500">尝试更换搜索词或调整筛选条件</p>
  </div>
</template>
