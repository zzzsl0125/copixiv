import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import TagManagement from '../../src/views/TagManagement.vue'

const tagPreferenceApiMock = vi.hoisted(() => ({
  getTagPreferences: vi.fn(),
  setTagPreference: vi.fn(),
  deleteTagPreference: vi.fn(),
  reorderTagPreferences: vi.fn(),
}))

const tagAliasApiMock = vi.hoisted(() => ({
  getTagAliases: vi.fn(),
  suggestTagAliases: vi.fn(),
  createTagAlias: vi.fn(),
  deleteTagAlias: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  tagPreferenceApi: tagPreferenceApiMock,
  tagAliasApi: tagAliasApiMock,
}))

const DraggableTableStub = {
  name: 'DraggableTable',
  props: ['items', 'columns', 'loading', 'emptyText'],
  template: `
    <div>
      <div v-for="(item, index) in items" :key="index">
        <slot name="tag" :item="item" />
        <slot name="actions" :item="item" />
      </div>
    </div>
  `,
}

describe('TagManagement (delete-preference contract regression)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    tagPreferenceApiMock.getTagPreferences.mockResolvedValue([
      { id: 7, tag: 'NTR', preference: 'favourite', sort_index: 0 },
    ])
    tagAliasApiMock.getTagAliases.mockResolvedValue([])
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  it('deletes a tag preference by its numeric id, not by tag name', async () => {
    const wrapper = mount(TagManagement, {
      global: {
        stubs: {
          PageHeader: true,
          SectionHeader: true,
          DraggableTable: DraggableTableStub,
          BaseModal: true,
          AliasSuggestModal: true,
        },
      },
    })
    await flushPromises()

    const deleteButton = wrapper
      .findAll('button')
      .find((button) => button.text().trim() === '删除')
    expect(deleteButton).toBeTruthy()
    await deleteButton!.trigger('click')
    await flushPromises()

    expect(tagPreferenceApiMock.deleteTagPreference).toHaveBeenCalledTimes(1)
    expect(tagPreferenceApiMock.deleteTagPreference).toHaveBeenCalledWith(7)
  })
})
