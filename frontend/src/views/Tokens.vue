<template>
  <div class="flex-1 flex flex-col min-w-0 h-full bg-gray-50">
    <PageHeader title="账号管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow overflow-auto">
      <SectionHeader 
        :tabs="[
          { name: 'list', label: '账号列表' }
        ]"
        active-tab="list"
        add-button-text="添加 Token"
        :show-refresh="true"
        :loading="loading"
        @add="openModal()"
        @refresh="loadTokens()"
      >
      </SectionHeader>

      <!-- Error state -->
      <div v-if="error" class="mb-6 bg-red-50 text-red-600 p-4 rounded-lg flex items-center gap-2">
        <svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
        </svg>
        {{ error }}
      </div>

      <DraggableTable 
        :items="tokens" 
        :columns="columns"
        :loading="loading"
        empty-text="暂无 Token"
        @reorder="onDragEnd"
      >
        <template #name="{ item: token }">
          <span class="text-sm font-medium text-gray-900">{{ token.name }}</span>
        </template>
        
        <template #token="{ item: token }">
          <span :title="token.token">{{ token.token.substring(0, 10) }}...{{ token.token.substring(token.token.length - 10) }}</span>
        </template>
        
        <template #premium="{ item: token }">
          <StatusBadgeButton 
            :active="token.premium" 
            activeTheme="yellow" 
            inactiveTheme="gray" 
            @click="togglePremium(token)"
          >
            {{ token.premium ? '是' : '否' }}
          </StatusBadgeButton>
        </template>
        
        <template #special="{ item: token }">
          <StatusBadgeButton 
            :active="token.special" 
            activeTheme="purple" 
            inactiveTheme="gray" 
            @click="toggleSpecial(token)"
          >
            {{ token.special ? '是' : '否' }}
          </StatusBadgeButton>
        </template>
        
        <template #valid="{ item: token }">
          <StatusBadgeButton 
            :active="token.valid" 
            activeTheme="green" 
            inactiveTheme="red" 
            @click="toggleValid(token)"
          >
            {{ token.valid ? '有效' : '无效' }}
          </StatusBadgeButton>
        </template>
        
        <template #actions="{ item: token }">
          <button @click="openModal(token)" class="text-indigo-600 hover:text-indigo-900 mr-3 text-sm font-medium">编辑</button>
          <button @click="deleteToken(token.id)" class="text-red-600 hover:text-red-900 text-sm font-medium">删除</button>
        </template>
      </DraggableTable>
    </main>

    <!-- Edit Modal -->
    <BaseModal
      :is-open="showModal"
      :title="currentToken.id ? '编辑 Token' : '添加 Token'"
      :loading="saving"
      @close="closeModal"
      @confirm="saveToken"
    >
      <AppInput 
        :model-value="currentToken.name || ''"
        @update:model-value="currentToken.name = $event"
        label="名称" 
        required 
      />
      
      <AppInput 
        :model-value="currentToken.token || ''"
        @update:model-value="currentToken.token = $event"
        type="textarea" 
        label="Token" 
        required 
      />
      
      <div class="flex items-center space-x-6">
        <AppCheckbox :model-value="!!currentToken.premium" @update:model-value="currentToken.premium = $event" label="高级会员" />
        <AppCheckbox :model-value="!!currentToken.special" @update:model-value="currentToken.special = $event" label="特殊账号" />
        <AppCheckbox :model-value="currentToken.valid !== false" @update:model-value="currentToken.valid = $event" label="有效" />
      </div>
    </BaseModal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { tokenApi, type Token } from '../api';
import PageHeader from '../components/PageHeader.vue';
import SectionHeader from '../components/SectionHeader.vue';
import DraggableTable, { type TableColumn } from '../components/DraggableTable.vue';
import BaseModal from '../components/BaseModal.vue';
import StatusBadgeButton from '../components/StatusBadgeButton.vue';
import AppInput from '../components/AppInput.vue';
import AppCheckbox from '../components/AppCheckbox.vue';

defineEmits<{
  (e: 'toggle-sidebar'): void;
}>();

const tokens = ref<Token[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

const showModal = ref(false);
const saving = ref(false);
const currentToken = ref<Partial<Token>>({
  name: '',
  token: '',
  premium: false,
  special: false,
  valid: true
});

const loadTokens = async () => {
  loading.value = true;
  error.value = null;
  try {
    tokens.value = await tokenApi.getTokens();
  } catch (err: any) {
    error.value = err.response?.data?.detail || err.message || '获取 Token 列表失败';
  } finally {
    loading.value = false;
  }
};

const onDragEnd = async (newTokens: Token[]) => {
  try {
    tokens.value = newTokens;
    const tokenIds = newTokens.map(t => t.id);
    await tokenApi.reorderTokens(tokenIds);
  } catch (err: any) {
    console.error('Failed to reorder tokens:', err);
    alert('排序保存失败，即将重新加载数据');
    await loadTokens();
  }
};

const openModal = (token?: Token) => {
  if (token) {
    currentToken.value = { ...token };
  } else {
    currentToken.value = {
      name: '',
      token: '',
      premium: false,
      special: false,
      valid: true
    };
  }
  showModal.value = true;
};

const closeModal = () => {
  showModal.value = false;
};

const togglePremium = async (token: Token) => {
  try {
    await tokenApi.updateToken(token.id, { premium: !token.premium });
    await loadTokens();
  } catch (err: any) {
    alert(err.response?.data?.detail || err.message || '更新状态失败');
  }
};

const toggleSpecial = async (token: Token) => {
  try {
    await tokenApi.updateToken(token.id, { special: !token.special });
    await loadTokens();
  } catch (err: any) {
    alert(err.response?.data?.detail || err.message || '更新状态失败');
  }
};

const toggleValid = async (token: Token) => {
  try {
    await tokenApi.updateToken(token.id, { valid: !token.valid });
    await loadTokens();
  } catch (err: any) {
    alert(err.response?.data?.detail || err.message || '更新状态失败');
  }
};

const saveToken = async () => {
  saving.value = true;
  error.value = null;
  try {
    if (currentToken.value.id) {
      await tokenApi.updateToken(currentToken.value.id, {
        name: currentToken.value.name,
        token: currentToken.value.token,
        premium: currentToken.value.premium,
        special: currentToken.value.special,
        valid: currentToken.value.valid
      });
    } else {
      await tokenApi.createToken({
        name: currentToken.value.name!,
        token: currentToken.value.token!,
        premium: currentToken.value.premium || false,
        special: currentToken.value.special || false,
        valid: currentToken.value.valid !== false
      });
    }
    closeModal();
    await loadTokens();
  } catch (err: any) {
    alert(err.response?.data?.detail || err.message || '保存失败');
  } finally {
    saving.value = false;
  }
};

const deleteToken = async (id: number) => {
  if (!confirm('确定要删除这个 Token 吗？')) {
    return;
  }
  
  try {
    await tokenApi.deleteToken(id);
    await loadTokens();
  } catch (err: any) {
    alert(err.response?.data?.detail || err.message || '删除失败');
  }
};

onMounted(() => {
  loadTokens();
});

const columns: TableColumn[] = [
  { key: 'name', label: '名称' },
  { key: 'token', label: 'Token', tdClass: 'text-sm text-gray-500 max-w-xs truncate' },
  { key: 'premium', label: '高级会员' },
  { key: 'special', label: '特殊账号' },
  { key: 'valid', label: '状态' },
  { key: 'actions', label: '操作', align: 'right' }
];
</script>
