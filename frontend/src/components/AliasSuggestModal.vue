<script setup lang="ts">
import { ref, watch, computed } from 'vue';
import BaseModal from './BaseModal.vue';
import AppInput from './AppInput.vue';
import AppCheckbox from './AppCheckbox.vue';
import { tagAliasApi, type TagAliasSuggest } from '../api';

const props = defineProps<{
  isOpen: boolean;
  targetTag?: string;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'updated'): void;
}>();

const loading = ref(false);
const saving = ref(false);
const suggestions = ref<TagAliasSuggest[]>([]);
const currentOffset = ref(0);

const currentSuggestion = computed(() => suggestions.value[0] || null);

const selectedCandidates = ref<Set<number>>(new Set());
const customTarget = ref('');

const fetchSuggestions = async () => {
  if (loading.value) return;
  loading.value = true;
  try {
    const data = await tagAliasApi.suggestTagAliases(5, currentOffset.value, props.targetTag);
    suggestions.value = data.items;
    currentOffset.value = data.next_offset;
    resetForm();
  } catch (error) {
    console.error('Failed to fetch suggestions:', error);
  } finally {
    loading.value = false;
  }
};

const resetForm = () => {
  selectedCandidates.value.clear();
  customTarget.value = '';
};

watch(() => props.isOpen, (newVal) => {
  if (newVal) {
    suggestions.value = [];
    currentOffset.value = 0;
    fetchSuggestions();
  }
});

const nextSuggestion = () => {
  suggestions.value.shift();
  resetForm();
  if (suggestions.value.length === 0) {
    if (props.targetTag) {
      emit('close');
    } else {
      fetchSuggestions();
    }
  }
};

const handleSkip = () => {
  nextSuggestion();
};

const handleSave = async () => {
  if (!currentSuggestion.value) return;
  
  saving.value = true;
  try {
    const targetTag = currentSuggestion.value.target.name;
    
    if (customTarget.value.trim()) {
      // 当前标签 -> 自定义目标
      await tagAliasApi.createTagAlias(targetTag, customTarget.value.trim());
      emit('updated');
    } else {
      // 选中的候选 -> 当前标签
      const selectedNames = currentSuggestion.value.candidates
        .filter(c => selectedCandidates.value.has(c.id))
        .map(c => c.name);
        
      if (selectedNames.length > 0) {
        for (const source of selectedNames) {
          await tagAliasApi.createTagAlias(source, targetTag);
        }
        emit('updated');
      }
    }
    nextSuggestion();
  } catch (error) {
    console.error('Failed to save aliases:', error);
    alert('保存失败');
  } finally {
    saving.value = false;
  }
};

const toggleCandidate = (id: number) => {
  const newSet = new Set(selectedCandidates.value);
  if (newSet.has(id)) {
    newSet.delete(id);
  } else {
    newSet.add(id);
  }
  selectedCandidates.value = newSet;
};
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    title="别名映射建议"
    :loading="saving"
    @close="$emit('close')"
    @confirm="handleSave"
    confirm-text="保存并继续"
  >
    <div v-if="loading && suggestions.length === 0" class="py-8 text-center text-gray-500">
      正在加载建议...
    </div>
    
    <div v-else-if="!currentSuggestion" class="py-8 text-center text-gray-500">
      没有更多建议了。
    </div>
    
    <div v-else class="space-y-6">
      <div class="bg-blue-50 p-4 rounded-md">
        <div class="text-lg font-bold text-blue-900">
          {{ currentSuggestion.target.name }}
          <span class="text-sm font-normal text-blue-700 ml-2">
            (引用次数: {{ currentSuggestion.target.reference_count }})
          </span>
        </div>
      </div>

      <div class="space-y-3">
        <h3 class="text-sm font-medium text-red-800 mb-1">注意，一旦设置别名，候选的标签将被修改且将无法回溯。</h3>
        <h4 class="text-sm font-medium text-gray-900">选项 1: 将以下候选标签作为当前标签的别名</h4>
        <div class="max-h-60 overflow-y-auto border border-gray-200 rounded-md p-2 space-y-2">
          <label 
            v-for="cand in currentSuggestion.candidates" 
            :key="cand.id"
            class="flex items-center space-x-3 p-2 hover:bg-gray-50 rounded cursor-pointer"
          >
            <AppCheckbox
              :model-value="selectedCandidates.has(cand.id)"
              @update:model-value="toggleCandidate(cand.id)"
            />
            <span class="text-sm text-gray-900 flex-1">{{ cand.name }}</span>
            <span class="text-xs text-gray-500">引用: {{ cand.reference_count }}</span>
          </label>
        </div>
      </div>

      <div class="relative">
        <div class="absolute inset-0 flex items-center" aria-hidden="true">
          <div class="w-full border-t border-gray-300"></div>
        </div>
        <div class="relative flex justify-center">
          <span class="px-2 bg-white text-sm text-gray-500">或者</span>
        </div>
      </div>

      <div class="space-y-3">
        <h4 class="text-sm font-medium text-gray-900">选项 2: 将当前标签设为其他标签的别名</h4>
        <AppInput
          v-model="customTarget"
          label="目标标签名称"
          placeholder="留空则使用选项1"
        />
        <p class="text-xs text-gray-500 mt-1">
          注：如果填写了此项，选项1的选择将被忽略。
        </p>
      </div>
    </div>
    
    <template #extra-buttons>
      <button 
        v-if="currentSuggestion"
        type="button" 
        @click="handleSkip" 
        class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 shadow-sm px-4 py-2 bg-white text-base font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm transition-colors"
      >
        跳过
      </button>
    </template>
  </BaseModal>
</template>
