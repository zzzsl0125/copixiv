<script setup lang="ts">
import { ref } from 'vue'
import type { Token } from '../types'
import { useTokens, useToast } from '../composables'
import { getApiErrorMessage } from '../api'
import PageHeader from '../components/features/PageHeader.vue'
import SectionHeader from '../components/features/SectionHeader.vue'
import DraggableTable, { type TableColumn } from '../components/features/DraggableTable.vue'
import BaseModal from '../components/ui/BaseModal.vue'
import StatusBadgeButton from '../components/ui/StatusBadgeButton.vue'
import AppInput from '../components/ui/AppInput.vue'
import AppCheckbox from '../components/ui/AppCheckbox.vue'

defineOptions({ inheritAttrs: false })
defineEmits<{ (e: 'toggle-sidebar'): void }>()

const { tokens, loading, error, loadTokens, toggleField, reorder, save, remove } = useTokens()
const toast = useToast()

const showModal = ref(false)
const saving = ref(false)
const currentToken = ref<Partial<Token>>({ name: '', token: '', premium: false, valid: true })
const maskedTokenHint = ref('')

const openModal = (token?: Token) => {
  if (token) {
    currentToken.value = { ...token, token: '' }
    maskedTokenHint.value = token.token
  } else {
    currentToken.value = { name: '', token: '', premium: false, valid: true }
    maskedTokenHint.value = ''
  }
  showModal.value = true
}

const closeModal = () => { showModal.value = false }

const handleToggle = async (token: Token, field: 'premium' | 'valid' | 'is_follow') => {
  try { await toggleField(token, field) }
  catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '更新状态失败'))
  }
}

const handleReorder = async (newTokens: unknown[]) => {
  try { await reorder(newTokens as Token[]) }
  catch {
    toast.error('排序保存失败，即将重新加载数据')
    await loadTokens()
  }
}

const handleSave = async () => {
  if (saving.value) return
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
    toast.error(getApiErrorMessage(err, '保存失败'))
  } finally { saving.value = false }
}

const handleDelete = async (id: number) => {
  if (!confirm('确定要删除这个 Token 吗？')) return
  try { await remove(id) }
  catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '删除失败'))
  }
}

const columns: TableColumn[] = [
  { key: 'name', label: '名称' },
  { key: 'token', label: 'Token', tdClass: 'text-sm text-gray-500 max-w-xs truncate' },
  { key: 'is_follow', label: '追更账号' },
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
          <span :title="(token as Token).token" class="font-mono">{{ (token as Token).token }}</span>
        </template>
        <template #is_follow="{ item: token }">
          <StatusBadgeButton :active="(token as Token).is_follow" activeTheme="purple" inactiveTheme="gray" @click="handleToggle(token as Token, 'is_follow')">
            {{ (token as Token).is_follow ? '是' : '否' }}
          </StatusBadgeButton>
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
      <div>
        <AppInput
          :model-value="currentToken.token || ''"
          @update:model-value="currentToken.token = $event"
          type="textarea"
          :label="currentToken.id ? 'Token（填写新 Token；留空保持不变）' : 'Token'"
          :required="!currentToken.id"
          :placeholder="currentToken.id ? '仅当需要更换 Token 时填写' : ''"
        />
        <p v-if="maskedTokenHint" class="mt-1 text-xs text-gray-500 font-mono">当前：{{ maskedTokenHint }}</p>
      </div>
      <div class="flex items-center space-x-6">
        <AppCheckbox :model-value="!!currentToken.premium" @update:model-value="currentToken.premium = $event" label="高级会员" />
        <AppCheckbox :model-value="currentToken.valid !== false" @update:model-value="currentToken.valid = $event" label="有效" />
      </div>
    </BaseModal>
  </div>
</template>
