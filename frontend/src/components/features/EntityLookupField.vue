<script setup lang="ts">
import { computed, onUnmounted, ref, useId, watch } from 'vue'
import { useDebounceFn } from '@vueuse/core'
import FormField from '../ui/FormField.vue'
import {
  formatIdLookupHint,
  ID_LOOKUP_DEBOUNCE_MS,
  useIdLookup,
} from '../../composables'

export interface LookupResult {
  name?: string | null
  author_name?: string | null
  series_name?: string | null
  novel_count: number
}

export interface EntitySearchOption {
  id: number
  name: string | null
  source: string
  novelCount: number
}

const props = withDefaults(
  defineProps<{
    modelValue: string
    entity: '作者' | '系列'
    label: string
    fetch: (id: number, signal?: AbortSignal) => Promise<LookupResult>
    search?: (q: string, signal?: AbortSignal) => Promise<EntitySearchOption[]>
    currentId?: number | null
    currentName?: string
    disabled?: boolean
    readonly?: boolean
    disabledNote?: string
    showHint?: boolean
    placeholder?: string
    emptyNote: string
    unlinkNote: string
    allowUnlink?: boolean
    unlinked?: boolean
    unlinkLabel?: string
    inputTestId?: string
    hintTestId?: string
    unlinkTestId?: string
  }>(),
  {
    currentId: null,
    currentName: '',
    disabled: false,
    readonly: false,
    showHint: true,
    allowUnlink: false,
    unlinked: false,
    unlinkLabel: '',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'update:unlinked': [value: boolean]
  'update:idNumber': [value: number | null | undefined]
}>()

const listboxId = useId()

const inputProxy = computed({
  get: () => props.modelValue,
  set: (val: string) => emit('update:modelValue', val),
})

const isInputDisabled = computed(() => props.disabled || (props.allowUnlink && props.unlinked))
const isLookupDisabled = computed(() => isInputDisabled.value || props.readonly)

const pickedOption = ref<EntitySearchOption | null>(null)
const searchResults = ref<EntitySearchOption[]>([])
const searchLoading = ref(false)
const searchFailed = ref(false)
const searchCompleted = ref(false)
const dropdownOpen = ref(false)
const activeIndex = ref(-1)

let searchSeq = 0
let searchController: AbortController | null = null

function abortSearchInFlight() {
  if (searchController) {
    searchController.abort()
    searchController = null
  }
}

const lookup = useIdLookup<LookupResult>({
  input: inputProxy,
  currentId: () => props.currentId ?? null,
  fetch: props.fetch,
  enabled: () =>
    !isLookupDisabled.value &&
    !(
      pickedOption.value !== null &&
      pickedOption.value.name !== null &&
      props.modelValue.trim() === String(pickedOption.value.id)
    ),
})

const debouncedSearch = useDebounceFn((queryText: string) => {
  void runSearch(queryText)
}, ID_LOOKUP_DEBOUNCE_MS)

function resetSearchState() {
  debouncedSearch.cancel()
  abortSearchInFlight()
  searchSeq += 1
  searchResults.value = []
  searchLoading.value = false
  searchFailed.value = false
  searchCompleted.value = false
  dropdownOpen.value = false
  activeIndex.value = -1
}

function scheduleSearch() {
  debouncedSearch.cancel()
  abortSearchInFlight()
  searchSeq += 1
  searchFailed.value = false
  searchCompleted.value = false

  if (isLookupDisabled.value || !props.search) {
    searchResults.value = []
    searchLoading.value = false
    dropdownOpen.value = false
    activeIndex.value = -1
    return
  }

  const trimmed = props.modelValue.trim()
  if (!trimmed || lookup.idNumber.value !== undefined) {
    searchResults.value = []
    searchLoading.value = false
    dropdownOpen.value = false
    activeIndex.value = -1
    return
  }

  searchResults.value = []
  dropdownOpen.value = false
  activeIndex.value = -1
  searchLoading.value = true
  void debouncedSearch(trimmed)
}

async function runSearch(queryText: string) {
  const searchFn = props.search
  if (!searchFn || isLookupDisabled.value) return
  abortSearchInFlight()
  const mine = ++searchSeq
  const nextController = new AbortController()
  searchController = nextController
  try {
    const items = await searchFn(queryText, nextController.signal)
    if (mine !== searchSeq || nextController.signal.aborted) return
    searchResults.value = items
    searchCompleted.value = true
    dropdownOpen.value = items.length > 0
    activeIndex.value = -1
  } catch {
    if (mine !== searchSeq || nextController.signal.aborted) return
    searchFailed.value = true
    searchResults.value = []
    dropdownOpen.value = false
    activeIndex.value = -1
  } finally {
    if (searchController === nextController) searchController = null
    if (mine === searchSeq) searchLoading.value = false
  }
}

function schedule() {
  pickedOption.value = null
  resetSearchState()
  lookup.schedule()
  scheduleSearch()
}

watch(
  lookup.idNumber,
  (val) => {
    emit('update:idNumber', val)
  },
  { immediate: true },
)

watch(
  () => props.modelValue,
  (val) => {
    if (pickedOption.value && val.trim() !== String(pickedOption.value.id)) {
      pickedOption.value = null
    }
    scheduleSearch()
  },
)

watch([isLookupDisabled, () => props.search], () => {
  if (isLookupDisabled.value) {
    resetSearchState()
  } else {
    scheduleSearch()
  }
  lookup.schedule()
})

onUnmounted(() => {
  debouncedSearch.cancel()
  abortSearchInFlight()
})

const hint = computed(() => {
  if ((props.disabled || props.readonly) && props.disabledNote) {
    return { tone: 'muted' as const, text: props.disabledNote }
  }

  const unlinking = props.allowUnlink && props.unlinked
  if (unlinking) {
    return formatIdLookupHint({
      entity: props.entity,
      idNumber: lookup.idNumber.value,
      currentId: props.currentId ?? null,
      currentName: props.currentName || '未知',
      unlinking: true,
      loading: false,
      failed: false,
      found: null,
      emptyNote: props.emptyNote,
      unlinkNote: props.unlinkNote,
    })
  }

  if (props.search && lookup.idNumber.value === undefined) {
    if (searchLoading.value) {
      return { tone: 'muted' as const, text: '搜索中…' }
    }
    if (searchFailed.value) {
      return { tone: 'warn' as const, text: '搜索失败；可直接输入数字 ID' }
    }
    if (searchResults.value.length > 0) {
      return {
        tone: 'muted' as const,
        text: `请从下拉列表选择${props.entity}，或输入数字 ID`,
      }
    }
    if (searchCompleted.value) {
      return {
        tone: 'warn' as const,
        text: `未找到匹配的${props.entity}，可尝试其他关键词或直接输入数字 ID`,
      }
    }
    return {
      tone: 'muted' as const,
      text: `输入${props.entity}名称搜索，或直接填写数字 ID`,
    }
  }

  const activePicked =
    pickedOption.value !== null && pickedOption.value.id === lookup.idNumber.value
      ? pickedOption.value
      : null

  const found = lookup.result.value
    ? {
        name:
          lookup.result.value.name ??
          lookup.result.value.author_name ??
          lookup.result.value.series_name,
        novelCount: lookup.result.value.novel_count,
      }
    : activePicked && activePicked.name !== null
      ? {
          name: activePicked.name,
          novelCount: activePicked.novelCount,
        }
      : null

  return formatIdLookupHint({
    entity: props.entity,
    idNumber: lookup.idNumber.value,
    currentId: props.currentId ?? null,
    currentName: props.currentName || '未知',
    unlinking: false,
    loading: lookup.loading.value,
    failed: lookup.failed.value,
    found,
    emptyNote: props.emptyNote,
    unlinkNote: props.unlinkNote,
  })
})

function formatSourceBadge(source: string): string {
  return source === 'local' ? '本地' : source
}

function selectOption(item: EntitySearchOption) {
  resetSearchState()
  pickedOption.value = item
  emit('update:modelValue', String(item.id))
}

function closeDropdown() {
  dropdownOpen.value = false
  activeIndex.value = -1
}

function onInput(event: Event) {
  pickedOption.value = null
  emit('update:modelValue', (event.target as HTMLInputElement).value)
}

function onBlur() {
  closeDropdown()
  debouncedSearch.cancel()
  abortSearchInFlight()
  searchSeq += 1
  searchLoading.value = false
}

function onKeydown(event: KeyboardEvent) {
  if (!props.search || isLookupDisabled.value) return

  if (event.key === 'Escape') {
    if (!dropdownOpen.value) return
    event.stopPropagation()
    closeDropdown()
    return
  }

  if (event.key === 'ArrowDown') {
    if (searchResults.value.length === 0) return
    event.preventDefault()
    if (!dropdownOpen.value) {
      dropdownOpen.value = true
      activeIndex.value = 0
      return
    }
    activeIndex.value = (activeIndex.value + 1) % searchResults.value.length
    return
  }

  if (event.key === 'ArrowUp') {
    if (!dropdownOpen.value || searchResults.value.length === 0) return
    event.preventDefault()
    activeIndex.value =
      activeIndex.value <= 0 ? searchResults.value.length - 1 : activeIndex.value - 1
    return
  }

  if (event.key === 'Enter' && dropdownOpen.value && activeIndex.value >= 0) {
    const selected = searchResults.value[activeIndex.value]
    if (!selected) return
    event.preventDefault()
    selectOption(selected)
  }
}

function onUnlinkChange(event: Event) {
  emit('update:unlinked', (event.target as HTMLInputElement).checked)
}

defineExpose({
  idNumber: lookup.idNumber,
  hint,
  schedule,
})
</script>

<template>
  <FormField
    :label="label"
    :disabled="disabled || readonly"
    :hint="showHint ? hint?.text : undefined"
    :hint-tone="showHint ? hint?.tone : undefined"
    :hint-test-id="hintTestId"
  >
    <template #default="{ id }">
      <div class="relative">
        <input
          :id="id"
          :value="modelValue"
          type="text"
          :inputmode="search ? undefined : 'numeric'"
          autocomplete="off"
          :role="search ? 'combobox' : undefined"
          :aria-expanded="search ? dropdownOpen : undefined"
          :aria-controls="search && dropdownOpen ? listboxId : undefined"
          :aria-autocomplete="search ? 'list' : undefined"
          :aria-activedescendant="
            search && dropdownOpen && activeIndex >= 0
              ? `${listboxId}-opt-${activeIndex}`
              : undefined
          "
          :placeholder="placeholder"
          :disabled="isInputDisabled"
          :readonly="readonly"
          :data-testid="inputTestId"
          class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed read-only:bg-gray-100 read-only:text-gray-500"
          @input="onInput"
          @blur="onBlur"
          @keydown="onKeydown"
        />
        <ul
          v-if="search && dropdownOpen && searchResults.length > 0"
          :id="listboxId"
          role="listbox"
          :aria-label="`${entity}搜索结果`"
          :data-testid="inputTestId ? `${inputTestId}-dropdown` : undefined"
          class="absolute z-30 mt-1 w-full max-h-48 overflow-y-auto bg-white border border-gray-200 rounded-md shadow-lg py-1 text-sm"
          @mousedown.prevent
        >
          <li
            v-for="(item, index) in searchResults"
            :id="`${listboxId}-opt-${index}`"
            :key="`${item.source}:${item.id}`"
            role="option"
            :aria-selected="index === activeIndex"
            :data-testid="inputTestId ? `${inputTestId}-option-${item.id}` : undefined"
            class="px-3 py-1.5 cursor-pointer flex items-center justify-between gap-2 transition-colors"
            :class="index === activeIndex ? 'bg-blue-50 text-blue-900' : 'text-gray-800 hover:bg-gray-50'"
            @mouseenter="activeIndex = index"
            @click="selectOption(item)"
          >
            <div class="min-w-0 flex-1">
              <div class="truncate font-medium">
                {{ item.name || `${entity} #${item.id}` }}
              </div>
              <div class="text-[11px] text-gray-400">
                #{{ item.id }}
              </div>
            </div>
            <div class="shrink-0 flex items-center gap-1.5 text-xs">
              <span
                class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium"
                :class="
                  item.source === 'local'
                    ? 'bg-amber-100 text-amber-800'
                    : 'bg-gray-100 text-gray-700'
                "
              >
                {{ formatSourceBadge(item.source) }}
              </span>
              <span class="text-gray-400 tabular-nums">{{ item.novelCount }} 篇</span>
            </div>
          </li>
        </ul>
      </div>
      <label
        v-if="allowUnlink"
        class="mt-1.5 inline-flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer"
      >
        <input
          type="checkbox"
          :checked="unlinked"
          :disabled="disabled || readonly"
          :data-testid="unlinkTestId"
          class="h-3.5 w-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
          @change="onUnlinkChange"
        />
        {{ unlinkLabel || `解除${entity}关联` }}
      </label>
    </template>
  </FormField>
</template>
