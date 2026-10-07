import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import BatchBar from '../../src/components/features/BatchBar.vue'

/**
 * 批量栏「编辑」（批量编辑作者 / 系列）的入口契约。
 *
 * 与其他操作按钮同款同排：有选择才可用（没选择时点进去只会看到一片改不了的书）。
 */

function mountBar(props: Partial<{
  matchedCount: number
  countLoading: boolean
  selectedCount: number
  hasSelection: boolean
  hasFilter: boolean
  selectAllLoading: boolean
  isViewingSelected: boolean
}> = {}) {
  return mount(BatchBar, {
    props: {
      matchedCount: 10,
      countLoading: false,
      selectedCount: 3,
      hasSelection: true,
      hasFilter: true,
      selectAllLoading: false,
      isViewingSelected: false,
      ...props,
    },
  })
}

describe('BatchBar「编辑」按钮', () => {
  it('存在，且点击 emit edit', async () => {
    const wrapper = mountBar()

    const button = wrapper.get('[data-testid="batch-edit"]')
    expect(button.text()).toContain('编辑')

    await button.trigger('click')

    expect(wrapper.emitted('edit')).toHaveLength(1)
  })

  it('没有选择时禁用（与其他操作按钮同一套规则）', async () => {
    const wrapper = mountBar({ hasSelection: false, selectedCount: 0 })

    expect(wrapper.get('[data-testid="batch-edit"]').attributes('disabled')).toBeDefined()
  })
})
