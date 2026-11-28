import { describe, expect, it, vi, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import NovelCard from '../features/NovelCard.vue'
import EditNovelModal from '../features/EditNovelModal.vue'
import ConfirmDialog from '../ui/ConfirmDialog.vue'
import type { Novel } from '../../types/novels'

const seriesMocks = vi.hoisted(() => ({
  updateSeries: vi.fn(),
  syncSeries: vi.fn(),
  deleteSeries: vi.fn(),
  updateNovel: vi.fn(),
  syncNovel: vi.fn(),
  deleteNovel: vi.fn(),
  lookupAuthor: vi.fn(),
  lookupSeries: vi.fn(),
  searchAuthors: vi.fn(),
  searchSeries: vi.fn(),
}))

vi.mock('../../api', () => ({
  novelApi: {
    updateSeries: seriesMocks.updateSeries,
    syncSeries: seriesMocks.syncSeries,
    deleteSeries: seriesMocks.deleteSeries,
    updateNovel: seriesMocks.updateNovel,
    syncNovel: seriesMocks.syncNovel,
    deleteNovel: seriesMocks.deleteNovel,
    lookupAuthor: seriesMocks.lookupAuthor,
    lookupSeries: seriesMocks.lookupSeries,
    searchAuthors: seriesMocks.searchAuthors,
    searchSeries: seriesMocks.searchSeries,
  },
  getApiErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

vi.mock('../../composables/useSources', () => ({
  useSources: () => ({
    getBadge: (source: string) => {
      if (source === 'esj') {
        return { label: 'ESJ', class: 'bg-sky-100 text-sky-800', title: 'ESJ Zone' }
      }
      if (source === 'local') {
        return { label: '本地', class: 'bg-gray-100 text-gray-800', title: '本地上传' }
      }
      return null
    },
  }),
}))

function makeNovel(overrides: Partial<Novel> = {}): Novel {
  return {
    id: 903,
    title: '魔法物语',
    author_id: 77,
    author_name: '作者A',
    series_id: 900,
    series_name: '魔法物语',
    series_index: null,
    like: 800,
    view: 9000,
    text: 3500,
    create_time: '2026-03-01T12:00:00Z',
    has_epub: 2,
    tags: ['奇幻', '主线'],
    is_favourite: false,
    is_special_follow: false,
    source: 'pixiv',
    external_url: 'https://www.pixiv.net/novel/series/900',
    can_sync: true,
    is_series: true,
    chapter_count: 3,
    editable_fields: ['series_index', 'tags', 'create_time'],
    ...overrides,
  }
}

describe('Series folded cards & modal behavior', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('NovelCard renders static series badge and clickable chapter-count line', async () => {
    const seriesWrapper = mount(NovelCard, {
      props: {
        novel: makeNovel(),
        isActive: false,
      },
    })

    expect(seriesWrapper.get('[data-testid="badge-series"]').text()).toBe('系列')
    expect(seriesWrapper.get('[data-testid="badge-epub"]').text()).toBe('EPUB')
    expect(seriesWrapper.text()).toContain('系列：')
    expect(seriesWrapper.text()).toContain('魔法物语')
    expect(seriesWrapper.text()).not.toContain('共 3 章')

    const clickableSeries = seriesWrapper
      .findAll('span')
      .find((s) => s.text() === '魔法物语')
    expect(clickableSeries).toBeTruthy()
    await clickableSeries!.trigger('click')
    expect(seriesWrapper.emitted('search')).toEqual([['series', 900]])

    // Drilldown chapter card omits badge-series and displays #series_index
    const chapterWrapper = mount(NovelCard, {
      props: {
        novel: makeNovel({
          id: 901,
          title: '序章',
          series_index: 1,
          is_series: false,
          chapter_count: 1,
        }),
        isActive: false,
      },
    })
    expect(chapterWrapper.find('[data-testid="badge-series"]').exists()).toBe(false)
    expect(chapterWrapper.text()).toContain('魔法物语 #1')
    seriesWrapper.unmount()
    chapterWrapper.unmount()
  })

  it('EditNovelModal hides chapter/date inputs in series mode and invokes series endpoints', async () => {
    const localSeries = makeNovel({
      id: -12,
      series_id: -90,
      source: 'local',
      can_sync: false,
      external_url: null,
      chapter_count: 2,
      editable_fields: ['title', 'author_id', 'series_id', 'series_index', 'tags', 'create_time'],
    })
    const updatedSeries = makeNovel({
      ...localSeries,
      title: '改名系列',
      series_name: '改名系列',
    })
    seriesMocks.updateSeries.mockResolvedValue(updatedSeries)
    seriesMocks.deleteSeries.mockResolvedValue(undefined)

    const confirmWrapper = mount(ConfirmDialog)
    const wrapper = mount(EditNovelModal, {
      props: {
        isOpen: true,
        novel: localSeries,
      },
      global: {
        stubs: { teleport: true },
      },
    })

    await nextTick()
    expect(wrapper.text()).toContain('编辑系列信息')
    expect(wrapper.text()).toContain('系列 #-90')
    expect(wrapper.text()).toContain('共 2 章')
    expect(wrapper.find('[data-testid="edit-series-id"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="edit-series-index"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="edit-date"]').exists()).toBe(false)

    await wrapper.get('[data-testid="edit-title"]').setValue('改名系列')
    await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
    await flushPromises()

    expect(seriesMocks.updateSeries).toHaveBeenCalledWith(-90, { title: '改名系列' })
    expect(seriesMocks.updateNovel).not.toHaveBeenCalled()
    expect(wrapper.emitted('updated')).toEqual([[updatedSeries]])

    // Delete calls deleteSeries after confirmation showing chapter count
    await wrapper.get('[data-testid="edit-delete"]').trigger('click')
    await nextTick()
    expect(confirmWrapper.text()).toContain('共 2 章')
    const acceptBtn = confirmWrapper.findAll('button').find((b) => b.text().includes('确认删除'))
    await acceptBtn!.trigger('click')
    await flushPromises()

    expect(seriesMocks.deleteSeries).toHaveBeenCalledWith(-90)
    expect(seriesMocks.deleteNovel).not.toHaveBeenCalled()
    expect(wrapper.emitted('deleted')).toEqual([[-12]])

    wrapper.unmount()
    confirmWrapper.unmount()
  })
})
