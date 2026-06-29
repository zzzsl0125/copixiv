<script setup lang="ts">
import { ref } from 'vue'
import type { Token } from '../types'
import { useTokens } from '../composables'
import PageHeader from '../components/features/PageHeader.vue'
import SectionHeader from '../components/features/SectionHeader.vue'
import DraggableTable, { type TableColumn } from '../components/features/DraggableTable.vue'
import BaseModal from '../components/ui/BaseModal.vue'
import StatusBadgeButton from '../components/ui/StatusBadgeButton.vue'
import AppInput from '../components/ui/AppInput.vue'
import AppCheckbox from '../components/ui/AppCheckbox.vue'

defineEmits<{ (e: 'toggle-sidebar'): void }>()

const { tokens, loading, error, loadTokens, toggleField, reorder, save, remove } = useTokens()

const showModal = ref(false)
const saving = ref(false)
const currentToken = ref<Partial<Token>>({ name: '', token: '', premium: false, valid: true })

const openModal = (token?: Token) => {
  currentToken.value = token ? { ...token } : { name: '', token: '', premium: false, valid: true }
  showModal.value = true
}

const closeModal = () => { showModal.value = false }

const handleToggle = async (token: Token, field: 'premium' | 'valid') => {
  try { await toggleField(token, field) }
  catch (err: unknown) {
    const msg = err && typeof err === 'object' && 'response' in err
      ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      : '更新状态失败'
    alert(msg || '更新状态失败')
  }
}

const handleReorder = async (newTokens: unknown[]) => {
  try { await reorder(newTokens as Token[]) }
  catch { alert('排序保存失败，即将重新加载数据'); await loadTokens() }
}

const handleSave = async () => {
  saving.value = true
  try {
    await save({
      id: currentToken.value.id,
      name: currentToken.value.name!,
      token: currentToken.value.token!,
      premium: currentToken.value.premium || false,
      valid: currentToken.value.valid !== false,
    })
    closeModal()
  } catch (err: unknown) {
    const msg = err && typeof err === 'object' && 'response' in err
      ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      : err instanceof Error ? err.message : '保存失败'
    alert(msg || '保存失败')
  } finally { saving.value = false }
}

const handleDelete = async (id: number) => {
  if (!confirm('确定要删除这个 Token 吗？')) return
  try { await remove(id) }
  catch (err: unknown) {
    const msg = err && typeof err === 'object' && 'response' in err
      ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      : '删除失败'
    alert(msg || '删除失败')
  }
}

const columns: TableColumn[] = [
  { key: 'name', label: '名称' },
  { key: 'token', label: 'Token', tdClass: 'text-sm text-gray-500 max-w-xs truncate' },
  { key: 'premium', label: '高级会员' },
  { key: 'valid', label: '状态' },
  { key: 'actions', label: '操作', align: 'right' },
]
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full bg-gray-50">
    <PageHeader title="账号管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow overflow-auto">
      <SectionHeader :tabs="[{ name: 'list', label: '账号列表' }]" active-tab="list" add-button-text="添加 Token" :show-refresh="true" :loading="loading" @add="openModal()" @refresh="loadTokens()" />

      <div v-if="error" class="mb-6 bg-red-50 text-red-600 p-4 rounded-lg flex items-center gap-2">
        <svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
        {{ error }}
      </div>

      <DraggableTable :items="tokens" :columns="columns" :loading="loading" empty-text="暂无 Token" @reorder="handleReorder">
        <template #name="{ item: token }">
          <span class="text-sm font-medium text-gray-900">{{ (token as Token).name }}</span>
        </template>
        <template #token="{ item: token }">
          <span :title="(token as Token).token">{{ (token as Token).token.substring(0, 10) }}...{{ (token as Token).token.substring((token as Token).token.length - 10) }}</span>
        </template>
        <template #premium="{ item: token }">
          <StatusBadgeButton :active="(token as Token).premium" activeTheme="yellow" inactiveTheme="gray" @click="handleToggle(token as Token, 'premium')">
            {{ (token as Token).premium ? '是' : '否' }}
          </StatusBadgeButton>
        </template>
        <template #valid="{ item: token }">
          <StatusBadgeButton :active="(token as Token).valid" activeTheme="green" inactiveTheme="red" @click="handleToggle(token as Token, 'valid')">
            {{ (token as Token).valid ? '有效' : '无效' }}
          </StatusBadgeButton>
        </template>
        <template #actions="{ item: token }">
          <button @click="openModal(token as Token)" class="text-indigo-600 hover:text-indigo-900 mr-3 text-sm font-medium">编辑</button>
          <button @click="handleDelete((token as Token).id)" class="text-red-600 hover:text-red-900 text-sm font-medium">删除</button>
        </template>
      </DraggableTable>
    </main>

    <BaseModal :is-open="showModal" :title="currentToken.id ? '编辑 Token' : '添加 Token'" :loading="saving" @close="closeModal" @confirm="handleSave">
      <AppInput :model-value="currentToken.name || ''" @update:model-value="currentToken.name = $event" label="名称" required />
      <AppInput :model-value="currentToken.token || ''" @update:model-value="currentToken.token = $event" type="textarea" label="Token" required />
      <div class="flex items-center space-x-6">
        <AppCheckbox :model-value="!!currentToken.premium" @update:model-value="currentToken.premium = $event" label="高级会员" />
        <AppCheckbox :model-value="currentToken.valid !== false" @update:model-value="currentToken.valid = $event" label="有效" />
      </div>
    </BaseModal>
  </div>
</template>
