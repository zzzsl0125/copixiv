import { describe, expect, it, vi, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, nextTick, ref } from 'vue'
import ConfirmDialog from '../ui/ConfirmDialog.vue'
import CronInput from '../features/CronInput.vue'
import TagChipInput from '../features/TagChipInput.vue'
import PixivIdLookupField from '../features/PixivIdLookupField.vue'
import NovelCard from '../features/NovelCard.vue'
import LogViewer from '../features/LogViewer.vue'
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
    is_favourite: 0,
    is_special_follow: 0,
    source: 'pixiv',
    editable_fields: ['tags'],
    ...overrides,
  }
}

describe('Phase 5 UI components', () => {
  afterEach(() => {
    vi.useRealTimers()
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

  it('PixivIdLookupField debounces valid lookups, rejects non-numeric IDs, and supports unlink disable state', async () => {
    vi.useFakeTimers()

    const fetchMock = vi.fn(async (id: number) => ({
      name: `作者-${id}`,
      novel_count: 3,
    }))

    const Host = defineComponent({
      components: { PixivIdLookupField },
      setup() {
        const idText = ref('')
        const unlinked = ref(false)
        return { idText, unlinked, fetchMock }
      },
      template: `
        <PixivIdLookupField
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
    expect(fetchMock).toHaveBeenCalledWith(12345)
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
})
