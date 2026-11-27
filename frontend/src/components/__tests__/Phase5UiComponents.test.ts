import { describe, expect, it, vi, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, nextTick, ref } from 'vue'
import ConfirmDialog from '../ui/ConfirmDialog.vue'
import CronInput from '../features/CronInput.vue'
import TagChipInput from '../features/TagChipInput.vue'
import EntityLookupField from '../features/EntityLookupField.vue'
import type { EntitySearchOption } from '../features/EntityLookupField.vue'
import EditNovelModal from '../features/EditNovelModal.vue'
import NovelCard from '../features/NovelCard.vue'
import LogViewer from '../features/LogViewer.vue'
import LoadMore from '../features/LoadMore.vue'
import { useConfirm } from '../../composables/useConfirm'
import { formatCronZh } from '../../lib/cron'
import type { Novel } from '../../types/novels'

function makeSampleNovel(overrides: Partial<Novel> = {}): Novel {
  return {
    id: 42,
    title: '测试轻小说标题',
    author_id: 1001,
    author_name: '测试作者',
    series_id: 2002,
    series_name: '测试系列',
    series_index: 1,
    tags: ['奇幻', '日常'],
    text: 12000,
    like: 300,
    view: 4500,
    create_time: '2026-01-15T10:00:00Z',
    has_epub: 1,
    is_favourite: false,
    is_special_follow: false,
    can_sync: true,
    source: 'pixiv',
    editable_fields: ['tags'],
    ...overrides,
  }
}

describe('Phase 5 UI components', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    const { resolveConfirm } = useConfirm()
    resolveConfirm(false)
  })

  it('useConfirm + ConfirmDialog resolves true on accept and false on cancel', async () => {
    const wrapper = mount(ConfirmDialog)
    const { confirm } = useConfirm()

    expect(wrapper.find('[data-testid="confirm-dialog"]').exists()).toBe(false)

    const acceptedPromise = confirm({
      title: '删除确认',
      description: '确定要删除这条记录吗？',
      confirmText: '确认删除',
      cancelText: '再想想',
      variant: 'danger',
    })
    await nextTick()

    expect(wrapper.find('[data-testid="confirm-dialog"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('删除确认')
    expect(wrapper.text()).toContain('确定要删除这条记录吗？')

    await wrapper.get('[data-testid="confirm-dialog-accept"]').trigger('click')
    await expect(acceptedPromise).resolves.toBe(true)
    await nextTick()
    expect(wrapper.find('[data-testid="confirm-dialog"]').exists()).toBe(false)

    const cancelledPromise = confirm('确定取消该操作吗？')
    await nextTick()
    expect(wrapper.find('[data-testid="confirm-dialog"]').exists()).toBe(true)

    await wrapper.get('[data-testid="confirm-dialog-cancel"]').trigger('click')
    await expect(cancelledPromise).resolves.toBe(false)
    await nextTick()
    expect(wrapper.find('[data-testid="confirm-dialog"]').exists()).toBe(false)
  })

  it('formatCronZh + CronInput keeps step cron in custom mode without NaN and emits daily preset changes', async () => {
    const stepText = formatCronZh('*/5 * * * *')
    expect(stepText).toBeTruthy()
    expect(stepText).not.toContain('NaN')

    const Host = defineComponent({
      components: { CronInput },
      setup() {
        const cron = ref('*/5 * * * *')
        return { cron }
      },
      template: '<CronInput v-model="cron" />',
    })

    const wrapper = mount(Host)
    const modeSelect = wrapper.get('select[aria-label="执行频率模式"]')
    expect((modeSelect.element as HTMLSelectElement).value).toBe('custom')

    const preview = wrapper.get('[data-testid="cron-preview"]')
    expect(preview.text()).not.toContain('NaN')
    expect(preview.text()).toContain('调度预览：')

    await modeSelect.setValue('daily')
    await wrapper.get('input[aria-label="小时"]').setValue('8')
    await wrapper.get('input[aria-label="分钟"]').setValue('30')
    await nextTick()

    expect(wrapper.vm.cron).toBe('30 8 * * *')
    expect(preview.text()).not.toContain('NaN')
  })

  it('TagChipInput supports chips add/dedup/remove on Enter and live tag splitting', async () => {
    const ChipsHost = defineComponent({
      components: { TagChipInput },
      setup() {
        const tags = ref(['冒险'])
        return { tags }
      },
      template: `
        <TagChipInput
          v-model="tags"
          mode="chips"
          label="标签"
          input-test-id="chip-input"
          tags-test-id="chip-list"
          tag-remove-test-id-prefix="remove-tag-"
        />
      `,
    })

    const chipsWrapper = mount(ChipsHost)
    const chipInput = chipsWrapper.get('[data-testid="chip-input"]')
    await chipInput.setValue('奇幻/冒险/魔法')
    await chipInput.trigger('keydown.enter')
    await nextTick()

    expect(chipsWrapper.vm.tags).toEqual(['冒险', '奇幻', '魔法'])

    await chipsWrapper.get('[data-testid="remove-tag-冒险"]').trigger('click')
    await nextTick()
    expect(chipsWrapper.vm.tags).toEqual(['奇幻', '魔法'])

    const LiveHost = defineComponent({
      components: { TagChipInput },
      setup() {
        const tags = ref<string[]>([])
        return { tags }
      },
      template: `
        <TagChipInput
          v-model="tags"
          mode="live"
          label="批量标签"
          input-test-id="live-tag-input"
        />
      `,
    })

    const liveWrapper = mount(LiveHost)
    await liveWrapper.get('[data-testid="live-tag-input"]').setValue('foo, bar baz foo')
    await nextTick()

    expect(liveWrapper.vm.tags).toEqual(['foo', 'bar', 'baz'])
    expect(liveWrapper.text()).toContain('共 3 个')
  })

  it('EntityLookupField debounces valid lookups, rejects non-numeric IDs without search, and supports unlink disable state', async () => {
    vi.useFakeTimers()

    const fetchMock = vi.fn(async (id: number) => ({
      name: `作者-${id}`,
      novel_count: 3,
    }))

    const Host = defineComponent({
      components: { EntityLookupField },
      setup() {
        const idText = ref('')
        const unlinked = ref(false)
        return { idText, unlinked, fetchMock }
      },
      template: `
        <EntityLookupField
          v-model="idText"
          entity="作者"
          label="作者 ID"
          :fetch="fetchMock"
          empty-note="留空保持不变"
          unlink-note="将清空作者绑定"
          :allow-unlink="true"
          :unlinked="unlinked"
          input-test-id="author-id-input"
          hint-test-id="author-id-hint"
        />
      `,
    })

    const wrapper = mount(Host)
    const input = wrapper.get('[data-testid="author-id-input"]')

    // Invalid non-numeric input shows inline validation without hitting fetch
    await input.setValue('abc')
    await nextTick()
    expect(wrapper.get('[data-testid="author-id-hint"]').text()).toContain('只能填数字 ID')
    expect(fetchMock).not.toHaveBeenCalled()

    // Valid numeric ID resolves after debounce window
    await input.setValue('12345')
    await vi.advanceTimersByTimeAsync(350)
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith(12345, expect.any(AbortSignal))
    expect(wrapper.get('[data-testid="author-id-hint"]').text()).toContain('✓ 作者-12345 · 库内已有 3 本')

    // Unlink flag disables input and displays unlink note
    wrapper.vm.unlinked = true
    await nextTick()
    expect((input.element as HTMLInputElement).disabled).toBe(true)
    expect(wrapper.get('[data-testid="author-id-hint"]').text()).toContain('将清空作者绑定')
  })

  it('NovelCard emits pure presentation events for action buttons without internal API calls', async () => {
    const novel = makeSampleNovel()
    const wrapper = mount(NovelCard, {
      props: {
        novel,
        isActive: false,
        tagPreferenceMap: new Map([['奇幻', 'favourite' as const]]),
      },
    })

    await wrapper.get('[data-testid="novel-download"]').trigger('click')
    await wrapper.get('[data-testid="novel-favourite"]').trigger('click')
    await wrapper.get('[data-testid="novel-follow"]').trigger('click')
    await wrapper.get('[data-testid="novel-edit"]').trigger('click')

    expect(wrapper.emitted('download')).toEqual([[novel]])
    expect(wrapper.emitted('toggle-favourite')).toEqual([[novel]])
    expect(wrapper.emitted('toggle-follow')).toEqual([[novel]])
    expect(wrapper.emitted('edit')).toEqual([[novel]])
  })

  it('LogViewer virtualizes 2000 log lines while reporting full total line count', async () => {
    const log = Array.from({ length: 2000 }, (_, i) => `log entry #${i + 1}`).join('\n')
    const wrapper = mount(LogViewer, {
      props: {
        log,
        title: 'Task Execution Log',
        maxHeight: '400px',
      },
    })
    await nextTick()

    expect(wrapper.text()).toContain('2000 lines')

    const scrollContainer = wrapper.get('[data-testid="log-viewer-scroll"]')
    const renderedRows = scrollContainer.findAll('div.min-w-full > div')
    expect(renderedRows.length).toBeGreaterThan(0)
    expect(renderedRows.length).toBeLessThan(200)
  })

  it('LoadMore emits once per intersection visit and does not infinite-loop after loading finishes', async () => {
    vi.useFakeTimers()

    let observerCallback: IntersectionObserverCallback | null = null
    class MockIntersectionObserver {
      constructor(cb: IntersectionObserverCallback) {
        observerCallback = cb
      }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords(): IntersectionObserverEntry[] {
        return []
      }
    }
    vi.stubGlobal('IntersectionObserver', MockIntersectionObserver)

    const wrapper = mount(LoadMore, {
      props: {
        loading: false,
        noMoreData: false,
        hasData: true,
      },
    })
    await nextTick()

    const triggerIntersection = (isIntersecting: boolean) => {
      observerCallback?.(
        [{ isIntersecting } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      )
    }

    // 1) First scroll to bottom triggers load-more once
    triggerIntersection(true)
    expect(wrapper.emitted('load-more')).toHaveLength(1)

    // 2) Duplicate intersection while still in the same visit is ignored
    triggerIntersection(true)
    expect(wrapper.emitted('load-more')).toHaveLength(1)

    // 3) Loading starts and finishes while sentinel stays in viewport -> does NOT loop
    await wrapper.setProps({ loading: true })
    await wrapper.setProps({ loading: false })
    await vi.advanceTimersByTimeAsync(350)
    expect(wrapper.emitted('load-more')).toHaveLength(1)

    // 4) Once new content pushes sentinel out of viewport and user scrolls to bottom again, it fires
    triggerIntersection(false)
    triggerIntersection(true)
    expect(wrapper.emitted('load-more')).toHaveLength(2)
  })

  it('NovelCard renders unnamed series fallback, hides follow button when author_id is missing, and matches tag preferences case-insensitively', async () => {
    const novel = makeSampleNovel({
      author_id: null as unknown as number,
      series_id: 4242,
      series_name: null,
      series_index: 3,
      tags: ['GAY', 'BlockedTag'],
    })

    const wrapper = mount(NovelCard, {
      props: {
        novel,
        isActive: false,
        tagPreferenceMap: new Map([
          ['gay', 'favourite' as const],
          ['blockedtag', 'blocked' as const],
        ]),
      },
    })

    expect(wrapper.text()).toContain('系列 #4242 #3')
    expect(wrapper.find('[data-testid="novel-follow"]').exists()).toBe(false)

    const gayTag = wrapper.findAll('span').find((s) => s.text() === 'GAY')
    expect(gayTag?.classes()).toContain('bg-blue-100')
    const blockedTag = wrapper.findAll('span').find((s) => s.text() === 'BlockedTag')
    expect(blockedTag?.classes()).toContain('line-through')
  })

  it('TagChipInput flushes draft input on blur and via exposed flush()', async () => {
    const Host = defineComponent({
      components: { TagChipInput },
      setup() {
        const tags = ref(['现有'])
        const chipRef = ref<InstanceType<typeof TagChipInput> | null>(null)
        return { tags, chipRef }
      },
      template: `
        <TagChipInput
          ref="chipRef"
          v-model="tags"
          mode="chips"
          label="标签"
          input-test-id="flush-chip-input"
        />
      `,
    })

    const wrapper = mount(Host)
    const input = wrapper.get('[data-testid="flush-chip-input"]')

    await input.setValue('失焦提交')
    await input.trigger('blur')
    await nextTick()
    expect(wrapper.vm.tags).toEqual(['现有', '失焦提交'])

    await input.setValue('保存前提交')
    expect(wrapper.vm.chipRef?.hasDraft).toBe(true)
    wrapper.vm.chipRef?.flush()
    await nextTick()
    expect(wrapper.vm.tags).toEqual(['现有', '失焦提交', '保存前提交'])
    expect(wrapper.vm.chipRef?.hasDraft).toBe(false)
  })

  it('EntityLookupField computes and exposes hint even when showHint is false', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async (id: number) => ({
      name: `作者-${id}`,
      novel_count: 5,
    }))

    const Host = defineComponent({
      components: { EntityLookupField },
      setup() {
        const idText = ref('')
        const fieldRef = ref<InstanceType<typeof EntityLookupField> | null>(null)
        return { idText, fieldRef, fetchMock }
      },
      template: `
        <EntityLookupField
          ref="fieldRef"
          v-model="idText"
          entity="作者"
          label="作者 ID"
          :fetch="fetchMock"
          :show-hint="false"
          empty-note="留空 = 不改作者"
          unlink-note="解除作者关联"
          input-test-id="hidden-hint-input"
          hint-test-id="hidden-hint-el"
        />
      `,
    })

    const wrapper = mount(Host)
    await nextTick()
    expect(wrapper.find('[data-testid="hidden-hint-el"]').exists()).toBe(false)
    expect(wrapper.vm.fieldRef?.hint?.text).toBe('留空 = 不改作者')

    await wrapper.get('[data-testid="hidden-hint-input"]').setValue('777')
    await vi.advanceTimersByTimeAsync(350)
    await flushPromises()

    expect(wrapper.find('[data-testid="hidden-hint-el"]').exists()).toBe(false)
    expect(wrapper.vm.fieldRef?.hint?.text).toContain('✓ 作者-777 · 库内已有 5 本')
  })

  it('EntityLookupField supports name search dropdown selection, Escape/blur dismissal, and negative ID lookup', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async (id: number) => ({
      name: `外部作者-${id}`,
      novel_count: 2,
    }))
    const searchMock = vi.fn(async (_q: string): Promise<EntitySearchOption[]> => [
      { id: -101, name: 'Alice', source: 'syosetu', novelCount: 4 },
      { id: 202, name: 'Alice-Local', source: 'local', novelCount: 1 },
    ])

    const Host = defineComponent({
      components: { EntityLookupField },
      setup() {
        const idText = ref('')
        return { idText, fetchMock, searchMock }
      },
      template: `
        <EntityLookupField
          v-model="idText"
          entity="作者"
          label="作者 ID"
          :fetch="fetchMock"
          :search="searchMock"
          empty-note="留空保持不变"
          unlink-note="解除作者关联"
          input-test-id="search-author-input"
          hint-test-id="search-author-hint"
        />
      `,
    })

    const wrapper = mount(Host)
    const input = wrapper.get('[data-testid="search-author-input"]')

    // Typing a name triggers debounced search instead of showing ID-only error
    await input.setValue('Alice')
    await nextTick()
    expect(wrapper.get('[data-testid="search-author-hint"]').text()).toBe('搜索中…')
    expect(fetchMock).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(350)
    await flushPromises()

    expect(searchMock).toHaveBeenCalledWith('Alice', expect.any(AbortSignal))
    const dropdown = wrapper.get('[data-testid="search-author-input-dropdown"]')
    expect(dropdown.attributes('role')).toBe('listbox')
    expect(dropdown.text()).toContain('Alice')
    expect(dropdown.text()).toContain('syosetu')
    expect(dropdown.text()).toContain('4 篇')
    expect(dropdown.text()).toContain('本地')
    expect(dropdown.text()).toContain('1 篇')

    // Escape closes dropdown without altering input
    await input.trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('[data-testid="search-author-input-dropdown"]').exists()).toBe(false)

    // ArrowDown re-opens dropdown and clicking an option selects its ID
    await input.trigger('keydown', { key: 'ArrowDown' })
    await nextTick()
    expect(wrapper.find('[data-testid="search-author-input-dropdown"]').exists()).toBe(true)

    await wrapper.get('[data-testid="search-author-input-option--101"]').trigger('click')
    await nextTick()

    expect(wrapper.vm.idText).toBe('-101')
    expect(wrapper.find('[data-testid="search-author-input-dropdown"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="search-author-hint"]').text()).toBe('✓ Alice · 库内已有 4 本')

    // Direct negative ID entry triggers fetch(id) after debounce
    await input.setValue('-99')
    await vi.advanceTimersByTimeAsync(350)
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith(-99, expect.any(AbortSignal))
    expect(wrapper.get('[data-testid="search-author-hint"]').text()).toBe('✓ 外部作者--99 · 库内已有 2 本')

    // Blur aborts pending search and closes dropdown
    await input.setValue('Bob')
    await vi.advanceTimersByTimeAsync(350)
    await flushPromises()
    expect(wrapper.find('[data-testid="search-author-input-dropdown"]').exists()).toBe(true)
    await input.trigger('blur')
    await nextTick()
    expect(wrapper.find('[data-testid="search-author-input-dropdown"]').exists()).toBe(false)
  })

  it('EntityLookupField aborts superseded search requests and discards stale responses', async () => {
    vi.useFakeTimers()
    const resolvers = new Map<string, (items: EntitySearchOption[]) => void>()
    const signals: AbortSignal[] = []
    const searchMock = vi.fn((q: string, signal?: AbortSignal) => {
      if (signal) signals.push(signal)
      return new Promise<EntitySearchOption[]>((resolve) => {
        resolvers.set(q, resolve)
      })
    })
    const fetchMock = vi.fn(async (id: number) => ({ name: `Author-${id}`, novel_count: 0 }))

    const Host = defineComponent({
      components: { EntityLookupField },
      setup() {
        const idText = ref('')
        return { idText, fetchMock, searchMock }
      },
      template: `
        <EntityLookupField
          v-model="idText"
          entity="作者"
          label="作者 ID"
          :fetch="fetchMock"
          :search="searchMock"
          empty-note="留空保持不变"
          unlink-note="解除作者关联"
          input-test-id="race-author-input"
          hint-test-id="race-author-hint"
        />
      `,
    })

    const wrapper = mount(Host)
    const input = wrapper.get('[data-testid="race-author-input"]')

    await input.setValue('First')
    await vi.advanceTimersByTimeAsync(350)
    expect(signals).toHaveLength(1)
    expect(signals[0]!.aborted).toBe(false)

    // Typing a new query aborts the first in-flight search
    await input.setValue('Second')
    await nextTick()
    expect(signals[0]!.aborted).toBe(true)

    await vi.advanceTimersByTimeAsync(350)
    expect(signals).toHaveLength(2)
    expect(signals[1]!.aborted).toBe(false)

    // Newer search resolves, then stale first search resolves
    resolvers.get('Second')!([{ id: 88, name: 'FreshMatch', source: 'pixiv', novelCount: 2 }])
    await flushPromises()
    resolvers.get('First')!([{ id: 11, name: 'StaleMatch', source: 'local', novelCount: 1 }])
    await flushPromises()

    const dropdown = wrapper.get('[data-testid="race-author-input-dropdown"]')
    expect(dropdown.text()).toContain('FreshMatch')
    expect(dropdown.text()).not.toContain('StaleMatch')
  })

  it('EditNovelModal sync button visibility honors novel.can_sync', async () => {
    const syncableNovel = makeSampleNovel({ source: 'syosetu', can_sync: true })
    const wrapper = mount(EditNovelModal, {
      props: {
        isOpen: true,
        novel: syncableNovel,
      },
    })
    await nextTick()
    expect(wrapper.find('[data-testid="edit-sync"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="edit-source-badge"]').text()).toBe('syosetu')

    await wrapper.setProps({
      novel: makeSampleNovel({ source: 'pixiv', can_sync: false }),
    })
    await nextTick()
    expect(wrapper.find('[data-testid="edit-sync"]').exists()).toBe(false)

    await wrapper.setProps({
      novel: makeSampleNovel({ source: 'local', can_sync: false }),
    })
    await nextTick()
    expect(wrapper.find('[data-testid="edit-sync"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="edit-source-badge"]').text()).toBe('本地')

    wrapper.unmount()
  })
})
