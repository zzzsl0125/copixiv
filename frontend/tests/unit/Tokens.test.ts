import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import Tokens from '../../src/views/Tokens.vue'

const tokenApiMock = vi.hoisted(() => ({
  getTokens: vi.fn(),
  createToken: vi.fn(),
  updateToken: vi.fn(),
  deleteToken: vi.fn(),
  reorderTokens: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  tokenApi: tokenApiMock,
}))

const DraggableTableStub = {
  name: 'DraggableTable',
  props: ['items', 'columns', 'loading', 'emptyText'],
  template: `
    <div>
      <div v-for="(item, index) in items" :key="index">
        <slot name="name" :item="item" />
        <slot name="token" :item="item" />
        <slot name="actions" :item="item" />
      </div>
    </div>
  `,
}

const maskedToken = { id: 1, name: '旧名称', token: '****abcd', premium: false, valid: true }

function mountTokens() {
  return mount(Tokens, {
    global: {
      stubs: {
        PageHeader: true,
        SectionHeader: true,
        DraggableTable: DraggableTableStub,
        StatusBadgeButton: true,
      },
    },
  })
}

describe('Tokens (masked-token regression)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    tokenApiMock.getTokens.mockResolvedValue([maskedToken])
    tokenApiMock.updateToken.mockResolvedValue(maskedToken)
    tokenApiMock.createToken.mockResolvedValue(maskedToken)
    tokenApiMock.deleteToken.mockResolvedValue({ ok: true })
    tokenApiMock.reorderTokens.mockResolvedValue({ ok: true })
  })

  it('renders the masked token as-is instead of duplicating it', async () => {
    const wrapper = mountTokens()
    await flushPromises()

    expect(wrapper.text()).toContain('****abcd')
    expect(wrapper.text()).not.toContain('****abcd...****abcd')
  })

  it('does not send the masked token back when only the name changes', async () => {
    const wrapper = mountTokens()
    await flushPromises()

    const editButton = wrapper
      .findAll('button')
      .find((button) => button.text().trim() === '编辑')
    expect(editButton).toBeTruthy()
    await editButton!.trigger('click')

    // The masked value must stay out of the editable form.
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('')

    await wrapper.find('input[type="text"]').setValue('新名称')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(tokenApiMock.updateToken).toHaveBeenCalledTimes(1)
    const [, payload] = tokenApiMock.updateToken.mock.calls[0] as [
      number,
      Record<string, unknown>,
    ]
    expect(payload).toEqual({
      name: '新名称',
      premium: false,
      valid: true,
    })
    expect(payload).not.toHaveProperty('token')
  })
})
