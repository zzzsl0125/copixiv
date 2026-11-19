<script setup lang="ts">
import { ref } from 'vue'
import type { Token } from '../types'
import { useConfirm, useToast, useTokens } from '../composables'
import { getApiErrorMessage } from '../api'
import PageHeader from '../components/features/PageHeader.vue'
import SectionHeader from '../components/features/SectionHeader.vue'
import DraggableTable, { type TableColumn } from '../components/features/DraggableTable.vue'
import BaseModal from '../components/ui/BaseModal.vue'
import StatusBadgeButton from '../components/ui/StatusBadgeButton.vue'
import AppInput from '../components/ui/AppInput.vue'
import AppCheckbox from '../components/ui/AppCheckbox.vue'
import ErrorAlert from '../components/ui/ErrorAlert.vue'

defineOptions({ inheritAttrs: false })
defineEmits<{ (e: 'toggle-sidebar'): void }>()

const { tokens, loading, error, loadTokens, toggleField, reorder, save, remove } = useTokens()
const toast = useToast()
const { confirm } = useConfirm()

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

const closeModal = () => {
  showModal.value = false
}

const handleToggle = async (token: Token, field: 'premium' | 'valid' | 'is_follow') => {
  try {
    await toggleField(token, field)
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '更新状态失败'))
  }
}

const handleReorder = async (newTokens: Token[]) => {
  try {
    await reorder(newTokens)
  } catch {
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
  } finally {
    saving.value = false
  }
}

const handleDelete = async (id: number) => {
  const confirmed = await confirm({
    title: '删除 Token',
    description: '确定要删除这个 Token 吗？',
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (!confirmed) return
  try {
    await remove(id)
  } catch (err: unknown) {
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
  <div class="flex-1 flex flex-col min-w-0 bg-gray-50">
    <PageHeader title="账号管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow">
      <SectionHeader
        :tabs="[{ name: 'list', label: '账号列表' }]"
        active-tab="list"
        add-button-text="添加 Token"
        :show-refresh="true"
        :loading="loading"
        @add="openModal()"
        @refresh="void loadTokens()"
      />

      <ErrorAlert :message="error" :retryable="true" @retry="void loadTokens()" />

      <DraggableTable
        :items="tokens"
        :columns="columns"
        :loading="loading"
        empty-text="暂无 Token"
        @reorder="handleReorder"
      >
        <template #name="{ item: token }">
          <span class="text-sm font-medium text-gray-900">{{ token.name }}</span>
        </template>
        <template #token="{ item: token }">
          <span :title="token.token" class="font-mono">{{ token.token }}</span>
        </template>
        <template #is_follow="{ item: token }">
          <StatusBadgeButton
            :active="token.is_follow"
            activeTheme="purple"
            inactiveTheme="gray"
            @click="handleToggle(token, 'is_follow')"
          >
            {{ token.is_follow ? '是' : '否' }}
          </StatusBadgeButton>
        </template>
        <template #premium="{ item: token }">
          <StatusBadgeButton
            :active="token.premium"
            activeTheme="yellow"
            inactiveTheme="gray"
            @click="handleToggle(token, 'premium')"
          >
            {{ token.premium ? '是' : '否' }}
          </StatusBadgeButton>
        </template>
        <template #valid="{ item: token }">
          <StatusBadgeButton
            :active="token.valid"
            activeTheme="green"
            inactiveTheme="red"
            @click="handleToggle(token, 'valid')"
          >
            {{ token.valid ? '有效' : '无效' }}
          </StatusBadgeButton>
        </template>
        <template #actions="{ item: token }">
          <button
            @click="openModal(token)"
            class="text-indigo-600 hover:text-indigo-900 mr-3 text-sm font-medium cursor-pointer"
          >
            编辑
          </button>
          <button
            @click="handleDelete(token.id)"
            class="text-red-600 hover:text-red-900 text-sm font-medium cursor-pointer"
          >
            删除
          </button>
        </template>
      </DraggableTable>
    </main>

    <BaseModal
      :is-open="showModal"
      :title="currentToken.id ? '编辑 Token' : '添加 Token'"
      :loading="saving"
      @close="closeModal"
      @confirm="handleSave"
    >
      <AppInput
        :model-value="currentToken.name || ''"
        @update:model-value="currentToken.name = $event"
        label="名称"
        required
      />
      <div>
        <AppInput
          :model-value="currentToken.token || ''"
          @update:model-value="currentToken.token = $event"
          type="textarea"
          :label="currentToken.id ? 'Token（填写新 Token；留空保持不变）' : 'Token'"
          :required="!currentToken.id"
          :placeholder="currentToken.id ? '仅当需要更换 Token 时填写' : ''"
        />
        <p v-if="maskedTokenHint" class="mt-1 text-xs text-gray-500 font-mono">
          当前：{{ maskedTokenHint }}
        </p>
      </div>
      <div class="flex items-center space-x-6">
        <AppCheckbox
          :model-value="!!currentToken.premium"
          @update:model-value="currentToken.premium = $event"
          label="高级会员"
        />
        <AppCheckbox
          :model-value="currentToken.valid !== false"
          @update:model-value="currentToken.valid = $event"
          label="有效"
        />
      </div>
    </BaseModal>
  </div>
</template>
