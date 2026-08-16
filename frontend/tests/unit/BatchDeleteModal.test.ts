import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import BatchDeleteModal from '../../src/components/features/BatchDeleteModal.vue'
import type { BatchScope } from '../../src/types'

const apiMock = vi.hoisted(() => ({
  batchOperation: vi.fn(),
  submitBatchTask: vi.fn(),
}))

vi.mock('../../src/api', () => ({
  novelApi: {
    batchOperation: apiMock.batchOperation,
    submitBatchTask: apiMock.submitBatchTask,
  },
  BATCH_MAX_NOVELS: 5000,
}))

const SCOPE: BatchScope = { mode: 'ids', novel_ids: [1, 2, 3], excluded_ids: [] }

function mountModal(props: Partial<{ isOpen: boolean; scope: BatchScope | null; scopeLabel: string }> = {}) {
  return mount(BatchDeleteModal, {
    props: {
      isOpen: true,
      scope: SCOPE,
      scopeLabel: '已勾选的 3 篇',
      ...props,
    },
  })
}

describe('BatchDeleteModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMock.batchOperation.mockResolvedValue({ matched: 3, affected: 3 })
  })

  it('requires the confirmation word before the button enables', async () => {
    const wrapper = mountModal()

    const input = wrapper.get('[data-testid="delete-confirm-input"]')
    const submit = wrapper.get('button[type="submit"]')
    expect(submit.attributes('disabled')).toBeDefined()

    await input.setValue('delete')
    expect(submit.attributes('disabled')).toBeDefined()

    await input.setValue('DELETE')
    expect(submit.attributes('disabled')).toBeUndefined()
  })

  it('submits the delete operation with the given scope and reports success', async () => {
    const wrapper = mountModal()

    await wrapper.get('[data-testid="delete-confirm-input"]').setValue('DELETE')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(apiMock.batchOperation).toHaveBeenCalledWith({
      operation: 'delete',
      scope: SCOPE,
    })
    expect(wrapper.emitted('success')?.[0]).toEqual([
      { matched: 3, affected: 3 },
    ])
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('does nothing when the confirm word has not been typed', async () => {
    const wrapper = mountModal()

    await wrapper.get('[data-testid="delete-confirm-input"]').setValue('DELETE')
    // Reset input via prop-less reopen simulation: type wrong word again
    await wrapper.get('[data-testid="delete-confirm-input"]').setValue('nope')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(apiMock.batchOperation).not.toHaveBeenCalled()
  })

  it('surfaces API errors instead of emitting success', async () => {
    apiMock.batchOperation.mockRejectedValue(new Error('500 boom'))
    const wrapper = mountModal()

    await wrapper.get('[data-testid="delete-confirm-input"]').setValue('DELETE')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.emitted('success')).toBeUndefined()
    expect(wrapper.emitted('error')).toBeTruthy()
  })

  it('clears the typed word each time the modal opens', async () => {
    const wrapper = mountModal()
    await wrapper.get('[data-testid="delete-confirm-input"]').setValue('DELETE')

    await wrapper.setProps({ isOpen: false })
    await wrapper.setProps({ isOpen: true })

    expect(
      (wrapper.get('[data-testid="delete-confirm-input"]').element as HTMLInputElement).value,
    ).toBe('')
  })

  it('routes selections over the sync cap to the background task system', async () => {
    apiMock.submitBatchTask.mockResolvedValue({ task_id: 42, matched: 5001 })
    const bigScope: BatchScope = {
      mode: 'ids',
      novel_ids: Array.from({ length: 5001 }, (_, i) => i + 1),
      excluded_ids: [],
    }
    const wrapper = mountModal({ scope: bigScope, scopeLabel: '已勾选的 5001 篇' })

    await wrapper.get('[data-testid="delete-confirm-input"]').setValue('DELETE')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(apiMock.batchOperation).not.toHaveBeenCalled()
    expect(apiMock.submitBatchTask).toHaveBeenCalledWith({
      operation: 'delete',
      scope: bigScope,
    })
    expect(wrapper.emitted('task-submitted')?.[0]).toEqual([
      { task_id: 42, matched: 5001 },
    ])
  })
})
