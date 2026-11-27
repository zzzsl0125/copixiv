<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { TabsList, TabsRoot, TabsTrigger } from 'reka-ui'
import {
  CheckCircle2,
  KeyRound,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
  Wifi,
  XCircle,
} from '@lucide/vue'
import { getApiErrorMessage } from '../api'
import {
  isMaskedSecret,
  useConfirm,
  useSourceCredentials,
  useSources,
  useToast,
} from '../composables'
import type {
  CredentialFieldSpec,
  CredentialKindSpec,
  SettingFieldSpec,
  SourceCredential,
  SourceItem,
  SourceTestResult,
} from '../types'
import DraggableTable, { type TableColumn } from '../components/features/DraggableTable.vue'
import PageHeader from '../components/features/PageHeader.vue'
import SectionHeader from '../components/features/SectionHeader.vue'
import AppInput from '../components/ui/AppInput.vue'
import AppSwitch from '../components/ui/AppSwitch.vue'
import BaseModal from '../components/ui/BaseModal.vue'
import ErrorAlert from '../components/ui/ErrorAlert.vue'
import FormField from '../components/ui/FormField.vue'
import StatusBadgeButton from '../components/ui/StatusBadgeButton.vue'

defineOptions({ inheritAttrs: false })
defineEmits<{ (e: 'toggle-sidebar'): void }>()

const CAPABILITY_LABELS: Record<string, string> = {
  fetch_by_ref: '引用抓取',
  file_upload: '文件上传',
  sync_novel: '单篇同步',
  retry_failure: '失败重试',
  auth_pool: '凭据池',
  author_feed: '作者追更',
  lookup_entity: '实体查询',
  test_connection: '连接测试',
}

interface KeyValueRow {
  rowId: number
  key: string
  value: string
}

interface CredentialFormState {
  id: number | null
  kind: string
  name: string
  valid: boolean
  secret: Record<string, string | boolean>
  maskedHints: Record<string, string>
  meta: Record<string, string | boolean>
}

const route = useRoute()
const toast = useToast()
const { confirm } = useConfirm()
const {
  sources,
  loading,
  error,
  reload,
  updateConfig,
  testConnection,
  getBadge,
} = useSources()

const activeSourceKey = ref('')
const togglingEnabled = ref(false)
const savingConfig = ref(false)
const testingSource = ref<string | null>(null)
const testResults = reactive<Record<string, SourceTestResult>>({})

let nextKvRowId = 1
const configDraft = ref<Record<string, unknown>>({})
const kvRowsByField = ref<Record<string, KeyValueRow[]>>({})

const activeSource = computed<SourceItem | undefined>(() =>
  sources.value.find((item) => item.source === activeSourceKey.value),
)

function syncDraftFromSource(item: SourceItem | undefined) {
  if (!item) {
    configDraft.value = {}
    kvRowsByField.value = {}
    return
  }
  const nextDraft: Record<string, unknown> = {}
  const nextKvRows: Record<string, KeyValueRow[]> = {}
  const schema = item.manifest.settings_schema ?? []

  for (const field of schema) {
    const rawVal = field.key in item.config ? item.config[field.key] : field.default
    if (field.field_type === 'key_value') {
      const record =
        rawVal && typeof rawVal === 'object' && !Array.isArray(rawVal)
          ? (rawVal as Record<string, unknown>)
          : {}
      const rows = Object.entries(record).map(([k, v]) => ({
        rowId: nextKvRowId++,
        key: k,
        value: typeof v === 'string' ? v : String(v ?? ''),
      }))
      nextKvRows[field.key] = rows
      nextDraft[field.key] = Object.fromEntries(rows.map((r) => [r.key, r.value]))
    } else if (field.field_type === 'boolean') {
      nextDraft[field.key] = Boolean(rawVal)
    } else if (field.field_type === 'number') {
      nextDraft[field.key] = typeof rawVal === 'number' ? rawVal : (rawVal ?? '')
    } else {
      nextDraft[field.key] = typeof rawVal === 'string' ? rawVal : String(rawVal ?? '')
    }
  }

  configDraft.value = nextDraft
  kvRowsByField.value = nextKvRows
}

watch(
  sources,
  (list) => {
    if (list.length === 0) {
      activeSourceKey.value = ''
      return
    }
    const querySource = typeof route.query.source === 'string' ? route.query.source : ''
    if (!activeSourceKey.value && querySource && list.some((s) => s.source === querySource)) {
      activeSourceKey.value = querySource
      return
    }
    if (!list.some((s) => s.source === activeSourceKey.value)) {
      activeSourceKey.value = list[0]!.source
    }
  },
  { immediate: true },
)

watch(
  () => [activeSource.value?.source, activeSource.value?.config] as const,
  ([nextSource, nextConfig], prev) => {
    const [prevSource, prevConfig] = prev ?? [undefined, undefined]
    if (
      nextSource !== prevSource ||
      JSON.stringify(nextConfig) !== JSON.stringify(prevConfig)
    ) {
      syncDraftFromSource(activeSource.value)
    }
  },
  { immediate: true },
)

function selectSource(source: string) {
  activeSourceKey.value = source
}

const activeCapabilities = computed(() => activeSource.value?.manifest.capabilities ?? [])
const supportsTestConnection = computed(() =>
  activeCapabilities.value.includes('test_connection'),
)
const activeBadge = computed(() =>
  activeSource.value ? getBadge(activeSource.value.source) : null,
)
const activeTestResult = computed<SourceTestResult | undefined>(() =>
  activeSource.value ? testResults[activeSource.value.source] : undefined,
)

function capabilityLabel(cap: string): string {
  return CAPABILITY_LABELS[cap] ?? cap
}

function isFieldVisible(field: SettingFieldSpec, draft: Record<string, unknown>): boolean {
  if (!field.visible_when) return true
  return Object.entries(field.visible_when).every(([depKey, expected]) => {
    const actual = draft[depKey]
    return Array.isArray(expected) ? expected.includes(actual) : actual === expected
  })
}

const settingsSchema = computed<SettingFieldSpec[]>(
  () => activeSource.value?.manifest.settings_schema ?? [],
)
const visibleSettingsFields = computed<SettingFieldSpec[]>(() =>
  settingsSchema.value.filter((field) => isFieldVisible(field, configDraft.value)),
)

function setConfigValue(key: string, value: unknown) {
  configDraft.value = { ...configDraft.value, [key]: value }
}

function setNumberConfigValue(key: string, raw: string) {
  if (raw === '') {
    setConfigValue(key, '')
    return
  }
  const parsed = Number(raw)
  setConfigValue(key, Number.isFinite(parsed) ? parsed : raw)
}

function addKvRow(fieldKey: string) {
  const existing = kvRowsByField.value[fieldKey] ?? []
  kvRowsByField.value = {
    ...kvRowsByField.value,
    [fieldKey]: [...existing, { rowId: nextKvRowId++, key: '', value: '' }],
  }
}

function removeKvRow(fieldKey: string, rowId: number) {
  const existing = kvRowsByField.value[fieldKey] ?? []
  kvRowsByField.value = {
    ...kvRowsByField.value,
    [fieldKey]: existing.filter((row) => row.rowId !== rowId),
  }
}

function updateKvRow(fieldKey: string, rowId: number, part: 'key' | 'value', val: string) {
  const existing = kvRowsByField.value[fieldKey] ?? []
  kvRowsByField.value = {
    ...kvRowsByField.value,
    [fieldKey]: existing.map((row) =>
      row.rowId === rowId ? { ...row, [part]: val } : row,
    ),
  }
}

function numberFieldHint(field: SettingFieldSpec): string | undefined {
  const parts: string[] = []
  if (field.description) parts.push(field.description)
  if (field.min_value != null && field.max_value != null) {
    parts.push(`取值范围：${field.min_value} ~ ${field.max_value}`)
  } else if (field.min_value != null) {
    parts.push(`最小值：${field.min_value}`)
  } else if (field.max_value != null) {
    parts.push(`最大值：${field.max_value}`)
  }
  return parts.length > 0 ? parts.join(' · ') : undefined
}

function buildValidatedConfigPayload(): { payload: Record<string, unknown> } | { error: string } {
  const payload: Record<string, unknown> = {}
  for (const field of settingsSchema.value) {
    if (field.field_type === 'key_value') {
      const rows = kvRowsByField.value[field.key] ?? []
      const map: Record<string, string> = {}
      for (const row of rows) {
        const cleanedKey = row.key.trim()
        if (!cleanedKey && !row.value) continue
        if (!cleanedKey) {
          return { error: `${field.label} 中存在空键名` }
        }
        map[cleanedKey] = row.value
      }
      payload[field.key] = map
      continue
    }

    const val = configDraft.value[field.key]
    if (field.field_type === 'number') {
      const num = typeof val === 'number' ? val : Number(String(val ?? '').trim())
      if (val === '' || val == null || !Number.isFinite(num)) {
        return { error: `${field.label} 必须是有效数字` }
      }
      if (field.min_value != null && num < field.min_value) {
        return { error: `${field.label} 不能小于 ${field.min_value}` }
      }
      if (field.max_value != null && num > field.max_value) {
        return { error: `${field.label} 不能大于 ${field.max_value}` }
      }
      payload[field.key] = num
      continue
    }

    if (field.field_type === 'boolean') {
      payload[field.key] = Boolean(val)
      continue
    }

    payload[field.key] = typeof val === 'string' ? val : String(val ?? '')
  }
  return { payload }
}

const hasConfigChanges = computed(() => {
  const item = activeSource.value
  if (!item || settingsSchema.value.length === 0) return false
  for (const field of settingsSchema.value) {
    const serverVal = field.key in item.config ? item.config[field.key] : field.default
    if (field.field_type === 'key_value') {
      const rows = kvRowsByField.value[field.key] ?? []
      const draftObj = Object.fromEntries(
        rows
          .filter((r) => r.key.trim() !== '' || r.value !== '')
          .map((r) => [r.key.trim(), r.value]),
      )
      const serverObj =
        serverVal && typeof serverVal === 'object' && !Array.isArray(serverVal)
          ? serverVal
          : {}
      if (JSON.stringify(draftObj) !== JSON.stringify(serverObj)) return true
    } else if (field.field_type === 'boolean') {
      if (Boolean(configDraft.value[field.key]) !== Boolean(serverVal)) return true
    } else if (field.field_type === 'number') {
      if (configDraft.value[field.key] !== serverVal) return true
    } else {
      if (String(configDraft.value[field.key] ?? '') !== String(serverVal ?? '')) return true
    }
  }
  return false
})

async function handleToggleEnabled(nextEnabled: boolean) {
  const item = activeSource.value
  if (!item || togglingEnabled.value) return
  togglingEnabled.value = true
  try {
    const updated = await updateConfig(item.source, { enabled: nextEnabled })
    toast.success(
      `${updated.manifest.display_name} 已${updated.enabled ? '启用' : '禁用'}`,
    )
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '切换启用状态失败'))
  } finally {
    togglingEnabled.value = false
  }
}

async function handleTestConnection() {
  const item = activeSource.value
  if (!item || testingSource.value) return
  testingSource.value = item.source
  try {
    const res = await testConnection(item.source)
    testResults[item.source] = res
    if (res.ok) {
      toast.success(res.message || `${item.manifest.display_name} 连接正常`)
    } else {
      toast.error(res.message || `${item.manifest.display_name} 连接失败`)
    }
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '测试连接失败'))
  } finally {
    testingSource.value = null
  }
}

async function handleSaveConfig() {
  const item = activeSource.value
  if (!item || savingConfig.value) return
  const built = buildValidatedConfigPayload()
  if ('error' in built) {
    toast.error(built.error)
    return
  }
  savingConfig.value = true
  try {
    const updated = await updateConfig(item.source, { config: built.payload })
    syncDraftFromSource(updated)
    toast.success(`${updated.manifest.display_name} 配置已保存`)
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '保存配置失败'))
  } finally {
    savingConfig.value = false
  }
}

function resetConfigDraft() {
  syncDraftFromSource(activeSource.value)
}

// ---------------------------------------------------------------------------
// Credentials Pool
// ---------------------------------------------------------------------------

const activeKinds = computed<CredentialKindSpec[]>(
  () => activeSource.value?.manifest.credential_kinds ?? [],
)
const hasCredentialPool = computed(() => activeKinds.value.length > 0)
const supportsReorder = computed(() => activeKinds.value.some((k) => k.supports_reorder))

const {
  credentials,
  loading: credsLoading,
  error: credsError,
  reload: reloadCreds,
  create: createCred,
  update: updateCred,
  remove: removeCred,
  reorder: reorderCreds,
} = useSourceCredentials(activeSourceKey, { enabled: hasCredentialPool })

const validCredCount = computed(() => credentials.value.filter((c) => c.valid).length)

function kindSpecFor(kind: string): CredentialKindSpec | undefined {
  return activeKinds.value.find((k) => k.kind === kind) ?? activeKinds.value[0]
}

function kindLabel(kind: string): string {
  return kindSpecFor(kind)?.label ?? kind
}

function effectiveMetaFieldsFor(spec: CredentialKindSpec | undefined): CredentialFieldSpec[] {
  if (!spec) return []
  const fields = [...(spec.meta_fields ?? [])]
  const seenKeys = new Set(fields.map((f) => f.key))
  for (const flag of spec.unique_meta_flags ?? []) {
    if (!seenKeys.has(flag)) {
      seenKeys.add(flag)
      fields.push({
        key: flag,
        label: flag,
        field_type: 'boolean',
        required: false,
        default: false,
        placeholder: '',
        description: '',
      })
    }
  }
  return fields
}

function booleanMetaFieldsFor(kind: string): CredentialFieldSpec[] {
  return effectiveMetaFieldsFor(kindSpecFor(kind)).filter(
    (f) => f.field_type === 'boolean',
  )
}

function textMetaFieldsFor(kind: string): CredentialFieldSpec[] {
  return effectiveMetaFieldsFor(kindSpecFor(kind)).filter(
    (f) => f.field_type !== 'boolean',
  )
}

function isUniqueMetaFlag(kind: string, key: string): boolean {
  return (kindSpecFor(kind)?.unique_meta_flags ?? []).includes(key)
}

const hasAnyMetaColumns = computed(() =>
  activeKinds.value.some((k) => effectiveMetaFieldsFor(k).length > 0),
)

const credColumns = computed<TableColumn[]>(() => {
  const cols: TableColumn[] = [{ key: 'name', label: '名称' }]
  if (activeKinds.value.length > 1) {
    cols.push({ key: 'kind', label: '类型' })
  }
  cols.push({
    key: 'secret',
    label: '凭据',
    tdClass: 'text-sm text-gray-500 max-w-xs truncate',
  })
  if (hasAnyMetaColumns.value) {
    cols.push({ key: 'meta', label: '属性标记' })
  }
  cols.push({ key: 'valid', label: '状态' })
  cols.push({ key: 'actions', label: '操作', align: 'right' })
  return cols
})

function formatSecretSummary(cred: SourceCredential): string {
  const entries = Object.entries(cred.secret ?? {})
  if (entries.length === 0) return '—'
  const spec = kindSpecFor(cred.kind)
  const labelMap = new Map((spec?.secret_fields ?? []).map((f) => [f.key, f.label]))
  return entries
    .map(([k, v]) => {
      const label = labelMap.get(k) ?? k
      return entries.length === 1 ? String(v ?? '****') : `${label}: ${String(v ?? '')}`
    })
    .join(' · ')
}

const mutatingCredId = ref<number | null>(null)

async function handleToggleCredValid(cred: SourceCredential) {
  if (mutatingCredId.value !== null) return
  mutatingCredId.value = cred.id
  try {
    await updateCred(cred.id, { valid: !cred.valid })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '更新状态失败'))
  } finally {
    mutatingCredId.value = null
  }
}

async function handleToggleCredMeta(cred: SourceCredential, metaKey: string) {
  if (mutatingCredId.value !== null) return
  mutatingCredId.value = cred.id
  try {
    const nextMeta = {
      ...cred.meta,
      [metaKey]: !cred.meta?.[metaKey],
    }
    await updateCred(cred.id, { meta: nextMeta })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '更新属性失败'))
  } finally {
    mutatingCredId.value = null
  }
}

async function handleReorderCreds(newCreds: SourceCredential[]) {
  try {
    await reorderCreds(newCreds)
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '排序保存失败'))
  }
}

async function handleDeleteCred(cred: SourceCredential) {
  const confirmed = await confirm({
    title: '删除凭据',
    description: `确定要删除凭据「${cred.name}」吗？`,
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (!confirmed) return
  try {
    await removeCred(cred.id)
    toast.success('凭据已删除')
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '删除凭据失败'))
  }
}

// Credential Create / Edit Modal
const showCredModal = ref(false)
const savingCred = ref(false)
const credForm = ref<CredentialFormState>({
  id: null,
  kind: '',
  name: '',
  valid: true,
  secret: {},
  maskedHints: {},
  meta: {},
})

const selectedKindSpec = computed<CredentialKindSpec | undefined>(() =>
  kindSpecFor(credForm.value.kind),
)
const modalMetaFields = computed<CredentialFieldSpec[]>(() =>
  effectiveMetaFieldsFor(selectedKindSpec.value),
)

function buildDefaultCredFields(
  spec: CredentialKindSpec | undefined,
  existing?: SourceCredential,
): Pick<CredentialFormState, 'secret' | 'maskedHints' | 'meta'> {
  const secret: Record<string, string | boolean> = {}
  const maskedHints: Record<string, string> = {}
  const meta: Record<string, string | boolean> = {}

  for (const sf of spec?.secret_fields ?? []) {
    const existingVal = existing?.secret?.[sf.key]
    if (sf.field_type === 'boolean') {
      secret[sf.key] = Boolean(existingVal ?? sf.default ?? false)
    } else if (sf.field_type === 'secret') {
      secret[sf.key] = ''
      if (existing && existingVal != null) {
        maskedHints[sf.key] = String(existingVal)
      }
    } else {
      secret[sf.key] =
        existing && existingVal != null
          ? String(existingVal)
          : String(sf.default ?? '')
    }
  }

  for (const mf of effectiveMetaFieldsFor(spec)) {
    const existingVal = existing?.meta?.[mf.key]
    if (mf.field_type === 'boolean') {
      meta[mf.key] = Boolean(existingVal ?? mf.default ?? false)
    } else {
      meta[mf.key] =
        existing && existingVal != null
          ? String(existingVal)
          : String(mf.default ?? '')
    }
  }

  return { secret, maskedHints, meta }
}

function openCredModal(cred?: SourceCredential) {
  if (cred) {
    const spec = kindSpecFor(cred.kind)
    const defaults = buildDefaultCredFields(spec, cred)
    credForm.value = {
      id: cred.id,
      kind: cred.kind,
      name: cred.name,
      valid: cred.valid,
      ...defaults,
    }
  } else {
    const defaultKind = activeKinds.value[0]
    const defaults = buildDefaultCredFields(defaultKind)
    credForm.value = {
      id: null,
      kind: defaultKind?.kind ?? '',
      name: '',
      valid: true,
      ...defaults,
    }
  }
  showCredModal.value = true
}

function closeCredModal() {
  showCredModal.value = false
}

function handleCredKindChange(nextKind: string) {
  if (credForm.value.id !== null) return
  const spec = kindSpecFor(nextKind)
  const defaults = buildDefaultCredFields(spec)
  credForm.value = {
    ...credForm.value,
    kind: nextKind,
    ...defaults,
  }
}

function secretFieldHint(sf: CredentialFieldSpec): string | undefined {
  if (credForm.value.id !== null && sf.field_type === 'secret') {
    return sf.description ? `${sf.description} · 留空则保留原值` : '留空则保留原值'
  }
  return sf.description || undefined
}

async function handleSaveCredential() {
  if (savingCred.value) return
  const trimmedName = credForm.value.name.trim()
  if (!trimmedName) {
    toast.error('凭据名称不能为空')
    return
  }

  const spec = selectedKindSpec.value
  const isEdit = credForm.value.id !== null
  const secretPayload: Record<string, unknown> = {}

  for (const sf of spec?.secret_fields ?? []) {
    const rawVal = credForm.value.secret[sf.key]
    if (sf.field_type === 'boolean') {
      secretPayload[sf.key] = Boolean(rawVal)
      continue
    }
    const text = String(rawVal ?? '').trim()
    if (sf.field_type === 'secret') {
      if (!isEdit) {
        if (sf.required && !text) {
          toast.error(`${sf.label}不能为空`)
          return
        }
        if (text) secretPayload[sf.key] = text
      } else if (text && !isMaskedSecret(text)) {
        secretPayload[sf.key] = text
      }
      continue
    }
    if (sf.required && !text) {
      toast.error(`${sf.label}不能为空`)
      return
    }
    if (text || isEdit) {
      secretPayload[sf.key] = text
    }
  }

  const metaPayload: Record<string, unknown> = {}
  for (const mf of modalMetaFields.value) {
    const rawVal = credForm.value.meta[mf.key]
    metaPayload[mf.key] =
      mf.field_type === 'boolean' ? Boolean(rawVal) : String(rawVal ?? '').trim()
  }

  savingCred.value = true
  try {
    if (isEdit && credForm.value.id !== null) {
      await updateCred(credForm.value.id, {
        name: trimmedName,
        valid: credForm.value.valid,
        meta: metaPayload,
        ...(Object.keys(secretPayload).length > 0 ? { secret: secretPayload } : {}),
      })
      toast.success('凭据已更新')
    } else {
      await createCred({
        name: trimmedName,
        kind: credForm.value.kind || undefined,
        secret: secretPayload,
        valid: credForm.value.valid,
        meta: metaPayload,
      })
      toast.success('凭据已添加')
    }
    closeCredModal()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '保存凭据失败'))
  } finally {
    savingCred.value = false
  }
}
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 bg-gray-50">
    <PageHeader title="数据源管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow">
      <SectionHeader :show-refresh="true" :loading="loading" @refresh="void reload()">
        <template #left>
          <TabsRoot
            v-if="sources.length > 0"
            :model-value="activeSourceKey"
            @update:model-value="selectSource(String($event))"
          >
            <TabsList class="-mb-[1px] flex space-x-6 overflow-x-auto" aria-label="数据源列表">
              <TabsTrigger
                v-for="item in sources"
                :key="item.source"
                :value="item.source"
                :data-testid="`source-tab-${item.source}`"
                class="inline-flex items-center gap-2 whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm transition-colors cursor-pointer focus:outline-none focus-visible:text-blue-600 data-[state=active]:border-blue-500 data-[state=active]:text-blue-600 border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
              >
                <span
                  class="inline-block w-2 h-2 rounded-full shrink-0"
                  :class="item.enabled ? 'bg-green-500' : 'bg-gray-300'"
                  :title="item.enabled ? '已启用' : '已禁用'"
                />
                <span>{{ item.manifest.display_name }}</span>
                <span
                  v-if="item.manifest.credential_kinds && item.manifest.credential_kinds.length > 0"
                  class="inline-flex items-center px-1.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600"
                  :title="`${item.valid_credential_count} 个有效 / 共 ${item.credential_count} 个凭据`"
                >
                  {{ item.credential_count }}
                </span>
              </TabsTrigger>
            </TabsList>
          </TabsRoot>
        </template>
      </SectionHeader>

      <ErrorAlert :message="error" :retryable="true" @retry="void reload()" />

      <div
        v-if="loading && sources.length === 0"
        class="bg-white rounded-lg shadow border border-gray-200 p-12 flex flex-col items-center justify-center text-gray-500"
      >
        <RefreshCw class="w-6 h-6 animate-spin text-blue-500 mb-2" />
        <span>正在加载数据源...</span>
      </div>

      <div
        v-else-if="!loading && sources.length === 0 && !error"
        class="bg-white rounded-lg shadow border border-gray-200 p-12 text-center text-gray-500"
      >
        暂无已注册的数据源
      </div>

      <div v-else-if="activeSource" class="space-y-6">
        <!-- 1. Header Card -->
        <section
          class="bg-white rounded-lg shadow border border-gray-200 p-6"
          data-testid="source-header-card"
        >
          <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div class="space-y-2">
              <div class="flex flex-wrap items-center gap-2.5">
                <h2 class="text-lg font-semibold text-gray-900">
                  {{ activeSource.manifest.display_name }}
                </h2>
                <span
                  v-if="activeBadge"
                  class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium"
                  :class="activeBadge.class"
                  :title="activeBadge.title"
                >
                  {{ activeBadge.label }}
                </span>
                <span class="text-xs font-mono text-gray-400">
                  {{ activeSource.source }}
                </span>
              </div>

              <div v-if="activeCapabilities.length > 0" class="flex flex-wrap gap-1.5">
                <span
                  v-for="cap in activeCapabilities"
                  :key="cap"
                  class="inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium bg-slate-100 text-slate-700"
                >
                  {{ capabilityLabel(cap) }}
                </span>
              </div>
            </div>

            <div class="flex flex-wrap items-center gap-4 shrink-0">
              <div class="flex items-center gap-2.5">
                <span class="text-sm font-medium text-gray-700">启用该数据源</span>
                <AppSwitch
                  :model-value="activeSource.enabled"
                  :disabled="togglingEnabled"
                  aria-label="启用该数据源"
                  data-testid="source-enabled-switch"
                  @update:model-value="handleToggleEnabled"
                />
              </div>

              <button
                v-if="supportsTestConnection"
                type="button"
                :disabled="testingSource === activeSource.source"
                data-testid="source-test-btn"
                class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-md border border-gray-300 bg-white text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors cursor-pointer shadow-sm"
                @click="handleTestConnection"
              >
                <RefreshCw
                  v-if="testingSource === activeSource.source"
                  class="w-4 h-4 animate-spin text-blue-600"
                />
                <Wifi v-else class="w-4 h-4 text-gray-500" />
                <span>{{ testingSource === activeSource.source ? '测试中…' : '测试连接' }}</span>
              </button>
            </div>
          </div>

          <div
            v-if="activeTestResult"
            data-testid="source-test-result"
            class="mt-4 rounded-md border p-3 text-sm flex flex-wrap items-center justify-between gap-2"
            :class="
              activeTestResult.ok
                ? 'bg-green-50 border-green-200 text-green-800'
                : 'bg-red-50 border-red-200 text-red-800'
            "
          >
            <div class="flex items-center gap-2 min-w-0">
              <CheckCircle2 v-if="activeTestResult.ok" class="w-4 h-4 shrink-0 text-green-600" />
              <XCircle v-else class="w-4 h-4 shrink-0 text-red-600" />
              <span class="font-medium break-words">
                {{ activeTestResult.message || (activeTestResult.ok ? '连接成功' : '连接失败') }}
              </span>
              <span
                v-if="activeTestResult.account_summary"
                class="text-xs opacity-85"
              >
                （{{ activeTestResult.account_summary }}）
              </span>
            </div>
            <span
              v-if="activeTestResult.latency_ms != null"
              class="text-xs font-mono opacity-80 shrink-0"
            >
              {{ Math.round(activeTestResult.latency_ms) }} ms
            </span>
          </div>
        </section>

        <!-- 2. Schema-driven Settings Form -->
        <section
          v-if="settingsSchema.length > 0"
          class="bg-white rounded-lg shadow border border-gray-200 p-6"
          data-testid="source-settings-section"
        >
          <div class="flex items-center justify-between mb-4">
            <h3 class="text-base font-semibold text-gray-900">参数配置</h3>
          </div>

          <form class="space-y-4" @submit.prevent="handleSaveConfig">
            <div v-for="field in visibleSettingsFields" :key="field.key">
              <!-- boolean -->
              <div
                v-if="field.field_type === 'boolean'"
                class="flex items-center justify-between gap-4 py-1"
              >
                <div class="min-w-0">
                  <div class="text-sm font-medium text-gray-700">{{ field.label }}</div>
                  <p v-if="field.description" class="mt-0.5 text-xs text-gray-400">
                    {{ field.description }}
                  </p>
                </div>
                <AppSwitch
                  :model-value="Boolean(configDraft[field.key])"
                  :aria-label="field.label"
                  @update:model-value="setConfigValue(field.key, $event)"
                />
              </div>

              <!-- select -->
              <FormField
                v-else-if="field.field_type === 'select'"
                :label="field.label"
                :hint="field.description || undefined"
                v-slot="{ id }"
              >
                <select
                  :id="id"
                  :value="String(configDraft[field.key] ?? '')"
                  class="mt-1 block w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                  @change="setConfigValue(field.key, ($event.target as HTMLSelectElement).value)"
                >
                  <option
                    v-for="opt in field.options ?? []"
                    :key="opt.value"
                    :value="opt.value"
                  >
                    {{ opt.label }}
                  </option>
                </select>
              </FormField>

              <!-- number -->
              <FormField
                v-else-if="field.field_type === 'number'"
                :label="field.label"
                :hint="numberFieldHint(field)"
                v-slot="{ id }"
              >
                <input
                  :id="id"
                  type="number"
                  step="any"
                  :min="field.min_value ?? undefined"
                  :max="field.max_value ?? undefined"
                  :value="configDraft[field.key] ?? ''"
                  class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                  @input="setNumberConfigValue(field.key, ($event.target as HTMLInputElement).value)"
                />
              </FormField>

              <!-- key_value -->
              <FormField
                v-else-if="field.field_type === 'key_value'"
                :label="field.label"
                :hint="field.description || undefined"
              >
                <div class="mt-2 space-y-2">
                  <div
                    v-for="row in kvRowsByField[field.key] ?? []"
                    :key="row.rowId"
                    class="flex items-center gap-2"
                  >
                    <input
                      type="text"
                      :value="row.key"
                      placeholder="键（如 book_id/chapter_id 或 *）"
                      aria-label="键"
                      class="flex-1 min-w-0 rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                      @input="updateKvRow(field.key, row.rowId, 'key', ($event.target as HTMLInputElement).value)"
                    />
                    <input
                      type="text"
                      :value="row.value"
                      placeholder="值"
                      aria-label="值"
                      class="flex-1 min-w-0 rounded-md border border-gray-300 px-3 py-1.5 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                      @input="updateKvRow(field.key, row.rowId, 'value', ($event.target as HTMLInputElement).value)"
                    />
                    <button
                      type="button"
                      class="p-1.5 text-gray-400 hover:text-red-600 rounded hover:bg-red-50 transition-colors cursor-pointer"
                      aria-label="删除条目"
                      title="删除条目"
                      @click="removeKvRow(field.key, row.rowId)"
                    >
                      <Trash2 class="w-4 h-4" />
                    </button>
                  </div>
                  <button
                    type="button"
                    class="inline-flex items-center gap-1 px-3 py-1.5 rounded-md border border-dashed border-gray-300 text-xs font-medium text-gray-600 hover:border-blue-400 hover:text-blue-600 transition-colors cursor-pointer"
                    @click="addKvRow(field.key)"
                  >
                    <Plus class="w-3.5 h-3.5" />
                    添加条目
                  </button>
                </div>
              </FormField>

              <!-- string -->
              <FormField
                v-else
                :label="field.label"
                :hint="field.description || undefined"
                v-slot="{ id }"
              >
                <input
                  :id="id"
                  type="text"
                  :value="String(configDraft[field.key] ?? '')"
                  class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                  @input="setConfigValue(field.key, ($event.target as HTMLInputElement).value)"
                />
              </FormField>
            </div>

            <div class="pt-3 border-t border-gray-100 flex items-center justify-end gap-3">
              <button
                v-if="hasConfigChanges"
                type="button"
                :disabled="savingConfig"
                class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-md border border-gray-300 bg-white text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 transition-colors cursor-pointer"
                @click="resetConfigDraft"
              >
                <RotateCcw class="w-4 h-4" />
                恢复
              </button>
              <button
                type="submit"
                :disabled="savingConfig"
                data-testid="source-save-config-btn"
                class="inline-flex items-center gap-1.5 px-4 py-2 rounded-md bg-blue-600 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors cursor-pointer shadow-sm"
              >
                <Save class="w-4 h-4" />
                {{ savingConfig ? '保存中…' : '保存配置' }}
              </button>
            </div>
          </form>
        </section>

        <!-- 3. Credentials Pool Section -->
        <section
          v-if="hasCredentialPool"
          class="space-y-4"
          data-testid="source-credentials-section"
        >
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-2">
              <KeyRound class="w-5 h-5 text-gray-500" />
              <h3 class="text-base font-semibold text-gray-900">凭据池</h3>
              <span class="text-xs text-gray-500">
                共 {{ credentials.length }} 个，有效 {{ validCredCount }} 个
              </span>
            </div>

            <div class="flex items-center gap-2">
              <button
                type="button"
                class="p-2 text-gray-500 hover:text-blue-600 rounded-full hover:bg-gray-100 transition-colors cursor-pointer"
                title="刷新凭据"
                aria-label="刷新凭据"
                @click="void reloadCreds()"
              >
                <RefreshCw class="w-4 h-4" :class="{ 'animate-spin': credsLoading }" />
              </button>
              <button
                type="button"
                data-testid="add-credential-btn"
                class="inline-flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors shadow-sm text-sm font-medium whitespace-nowrap cursor-pointer"
                @click="openCredModal()"
              >
                <Plus class="w-4 h-4" />
                添加凭据
              </button>
            </div>
          </div>

          <ErrorAlert :message="credsError" :retryable="true" @retry="void reloadCreds()" />

          <DraggableTable
            :items="credentials"
            :columns="credColumns"
            :loading="credsLoading"
            :disabled="!supportsReorder"
            empty-text="暂无凭据，点击右上角「添加凭据」创建"
            @reorder="handleReorderCreds"
          >
            <template #name="{ item: cred }">
              <span class="text-sm font-medium text-gray-900">{{ cred.name }}</span>
            </template>
            <template #kind="{ item: cred }">
              <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-slate-100 text-slate-700">
                {{ kindLabel(cred.kind) }}
              </span>
            </template>
            <template #secret="{ item: cred }">
              <span :title="formatSecretSummary(cred)" class="font-mono text-xs text-gray-600">
                {{ formatSecretSummary(cred) }}
              </span>
            </template>
            <template #meta="{ item: cred }">
              <div class="flex flex-wrap items-center gap-1.5">
                <StatusBadgeButton
                  v-for="mf in booleanMetaFieldsFor(cred.kind)"
                  :key="mf.key"
                  :active="Boolean(cred.meta?.[mf.key])"
                  :active-theme="isUniqueMetaFlag(cred.kind, mf.key) ? 'purple' : 'yellow'"
                  inactive-theme="gray"
                  :disabled="mutatingCredId === cred.id"
                  @click="handleToggleCredMeta(cred, mf.key)"
                >
                  {{ mf.label }}：{{ cred.meta?.[mf.key] ? '是' : '否' }}
                </StatusBadgeButton>
                <span
                  v-for="sf in textMetaFieldsFor(cred.kind)"
                  :key="sf.key"
                  class="text-xs text-gray-600"
                >
                  {{ sf.label }}: {{ cred.meta?.[sf.key] ?? '—' }}
                </span>
              </div>
            </template>
            <template #valid="{ item: cred }">
              <StatusBadgeButton
                :active="cred.valid"
                active-theme="green"
                inactive-theme="red"
                :disabled="mutatingCredId === cred.id"
                @click="handleToggleCredValid(cred)"
              >
                {{ cred.valid ? '有效' : '无效' }}
              </StatusBadgeButton>
            </template>
            <template #actions="{ item: cred }">
              <button
                type="button"
                class="text-indigo-600 hover:text-indigo-900 mr-3 text-sm font-medium cursor-pointer"
                @click="openCredModal(cred)"
              >
                编辑
              </button>
              <button
                type="button"
                class="text-red-600 hover:text-red-900 text-sm font-medium cursor-pointer"
                @click="handleDeleteCred(cred)"
              >
                删除
              </button>
            </template>
          </DraggableTable>
        </section>
      </div>
    </main>

    <!-- Create / Edit Credential Modal -->
    <BaseModal
      :is-open="showCredModal"
      :title="credForm.id !== null ? '编辑凭据' : '添加凭据'"
      :loading="savingCred"
      @close="closeCredModal"
      @confirm="handleSaveCredential"
    >
      <FormField
        v-if="activeKinds.length > 1 && credForm.id === null"
        label="凭据类型"
        v-slot="{ id }"
      >
        <select
          :id="id"
          :value="credForm.kind"
          class="mt-1 block w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          @change="handleCredKindChange(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="k in activeKinds" :key="k.kind" :value="k.kind">
            {{ k.label }}
          </option>
        </select>
      </FormField>
      <div v-else-if="activeKinds.length > 1 && credForm.id !== null" class="text-sm text-gray-600">
        凭据类型：
        <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-slate-100 text-slate-700">
          {{ kindLabel(credForm.kind) }}
        </span>
      </div>

      <AppInput
        :model-value="credForm.name"
        label="名称"
        placeholder="例如 主账号 / 备用凭据"
        required
        @update:model-value="credForm.name = $event"
      />

      <template v-for="sf in selectedKindSpec?.secret_fields ?? []" :key="sf.key">
        <FormField
          v-if="sf.field_type === 'secret'"
          :label="sf.label"
          :hint="secretFieldHint(sf)"
          v-slot="{ id }"
        >
          <input
            :id="id"
            type="password"
            autocomplete="new-password"
            :required="credForm.id === null && sf.required"
            :placeholder="credForm.id !== null ? '留空保留原值' : (sf.placeholder || `请输入${sf.label}`)"
            :value="String(credForm.secret[sf.key] ?? '')"
            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            @input="credForm.secret[sf.key] = ($event.target as HTMLInputElement).value"
          />
          <p
            v-if="credForm.id !== null && credForm.maskedHints[sf.key]"
            class="mt-1 text-xs text-gray-500 font-mono"
          >
            当前：{{ credForm.maskedHints[sf.key] }}
          </p>
        </FormField>

        <div
          v-else-if="sf.field_type === 'boolean'"
          class="flex items-center justify-between py-1"
        >
          <span class="text-sm font-medium text-gray-700">{{ sf.label }}</span>
          <AppSwitch
            :model-value="Boolean(credForm.secret[sf.key])"
            :aria-label="sf.label"
            @update:model-value="credForm.secret[sf.key] = $event"
          />
        </div>

        <AppInput
          v-else
          :model-value="String(credForm.secret[sf.key] ?? '')"
          :label="sf.label"
          :required="sf.required"
          :placeholder="sf.placeholder || undefined"
          @update:model-value="credForm.secret[sf.key] = $event"
        />
      </template>

      <template v-for="mf in modalMetaFields" :key="mf.key">
        <div
          v-if="mf.field_type === 'boolean'"
          class="flex items-center justify-between py-1"
        >
          <div>
            <div class="text-sm font-medium text-gray-700">{{ mf.label }}</div>
            <p v-if="mf.description" class="text-xs text-gray-400">{{ mf.description }}</p>
          </div>
          <AppSwitch
            :model-value="Boolean(credForm.meta[mf.key])"
            :aria-label="mf.label"
            @update:model-value="credForm.meta[mf.key] = $event"
          />
        </div>

        <AppInput
          v-else
          :model-value="String(credForm.meta[mf.key] ?? '')"
          :label="mf.label"
          :required="mf.required"
          :placeholder="mf.placeholder || undefined"
          @update:model-value="credForm.meta[mf.key] = $event"
        />
      </template>

      <div class="flex items-center justify-between pt-2 border-t border-gray-100">
        <span class="text-sm font-medium text-gray-700">启用该凭据（有效）</span>
        <AppSwitch
          :model-value="credForm.valid"
          aria-label="启用该凭据"
          @update:model-value="credForm.valid = $event"
        />
      </div>
    </BaseModal>
  </div>
</template>
