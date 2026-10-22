import { describe, it, expect, afterEach } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import BaseModal from '../../src/components/ui/BaseModal.vue'

enableAutoUnmount(afterEach)

type BaseModalProps = Partial<{
  isOpen: boolean
  title: string
  loading: boolean
  confirmText: string
  cancelText: string
  confirmDisabled: boolean
}>

/** attachTo body：reka 的 focus trap / autofocus / initial focus 需要真实文档树。 */
function mountModal(props: BaseModalProps = {}, extraButtons = '') {
  return mount(BaseModal, {
    props: { isOpen: true, title: '编辑小说', ...props },
    slots: {
      default: '<p data-testid="body">正文</p>',
      ...(extraButtons ? { 'extra-buttons': extraButtons } : {}),
    },
    attachTo: document.body,
  })
}

type Wrapper = ReturnType<typeof mountModal>

function panel(wrapper: Wrapper) {
  return wrapper.get('[data-testid="base-modal"]')
}

function confirmButton(wrapper: Wrapper) {
  return wrapper.get('[data-testid="base-modal"] button[type="submit"]')
}

function cancelButton(wrapper: Wrapper) {
  return wrapper.get('[data-testid="base-modal"] button[type="button"]')
}

async function pressEscape() {
  // keydown 冒泡到 window，reka 的 DismissableLayer 在 window 上监听。
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  await nextTick()
  await flushPromises()
}

describe('BaseModal', () => {
  it('open 时渲染面板、标题、正文与 footer；close 后面板同步消失', async () => {
    const wrapper = mountModal({}, '<button data-testid="extra">删除</button>')

    expect(panel(wrapper).attributes('data-testid')).toBe('base-modal')
    expect(wrapper.find('[data-testid="base-modal"] form').exists()).toBe(true)
    expect(wrapper.get('[data-testid="base-modal"] h3').text()).toBe('编辑小说')
    expect(wrapper.get('[data-testid="body"]').text()).toBe('正文')
    expect(confirmButton(wrapper).text()).toBe('保存')
    expect(cancelButton(wrapper).text()).toBe('取消')
    expect(wrapper.get('[data-testid="extra"]').text()).toBe('删除')

    await wrapper.setProps({ isOpen: false })
    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(false)
  })

  it('关闭状态不渲染任何面板（含 overlay）', () => {
    const wrapper = mountModal({ isOpen: false })
    expect(wrapper.find('[data-testid="base-modal"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="base-modal-overlay"]').exists()).toBe(false)
  })

  it('提交表单 emit confirm；confirmDisabled 时按钮禁用且不 emit', async () => {
    const wrapper = mountModal()
    await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
    expect(wrapper.emitted('confirm')).toHaveLength(1)

    await wrapper.setProps({ confirmDisabled: true })
    expect(confirmButton(wrapper).attributes('disabled')).toBeDefined()
    // 直接提交表单（绕过 disabled 按钮）也不应 emit：confirm() 自己守一道。
    await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
    expect(wrapper.emitted('confirm')).toHaveLength(1)
  })

  it('点「取消」emit close，且该按钮是 type=button（不会提交表单）', async () => {
    const wrapper = mountModal()
    expect(cancelButton(wrapper).attributes('type')).toBe('button')
    await cancelButton(wrapper).trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('不 loading 时 ESC 关闭（只 emit 一次）', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await pressEscape()

    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('不 loading 时点遮罩关闭', async () => {
    const wrapper = mountModal()
    await flushPromises()

    await wrapper.get('[data-testid="base-modal-overlay"]').trigger('click')

    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('loading 时 ESC / 遮罩 / 提交都不生效，confirm 按钮禁用且文案变「保存中」', async () => {
    const wrapper = mountModal({ loading: true, confirmText: '保存' })
    await flushPromises()

    expect(confirmButton(wrapper).attributes('disabled')).toBeDefined()
    expect(confirmButton(wrapper).text()).toBe('保存中')

    await pressEscape()
    expect(wrapper.emitted('close')).toBeUndefined()

    await wrapper.get('[data-testid="base-modal-overlay"]').trigger('click')
    expect(wrapper.emitted('close')).toBeUndefined()

    await wrapper.get('[data-testid="base-modal"] form').trigger('submit')
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('loading 时「确认」也按既有规则替换为「确认中」', () => {
    const wrapper = mountModal({ loading: true, confirmText: '确认下载' })
    expect(confirmButton(wrapper).text()).toBe('确认中下载')
  })

  it('footer 的 DOM 顺序是 确认 / 取消 / #extra-buttons（slot 在末位）', () => {
    const wrapper = mountModal({}, '<button data-testid="extra">删除</button>')

    const submit = confirmButton(wrapper).element
    const footer = submit.parentElement
    expect(footer).not.toBeNull()

    const children = Array.from(footer!.children)
    expect(children).toHaveLength(3)
    expect(children[0]).toBe(submit)
    expect((children[1] as HTMLElement).textContent?.trim()).toBe('取消')
    expect(children[2]).toBe(wrapper.get('[data-testid="extra"]').element)
  })

  it('role=dialog / aria-modal / aria-labelledby 指向可见标题', async () => {
    const wrapper = mountModal()
    await flushPromises()

    const dialog = panel(wrapper)
    expect(dialog.attributes('role')).toBe('dialog')
    expect(dialog.attributes('aria-modal')).toBe('true')
    const labelledBy = dialog.attributes('aria-labelledby')
    expect(labelledBy).toBeTruthy()
    expect(wrapper.get(`[id="${labelledBy}"]`).text()).toBe('编辑小说')
    // reka 默认会接 aria-describedby；显式关掉，避免指向不存在的描述节点。
    expect(dialog.attributes('aria-describedby')).toBeUndefined()
  })

  it('焦点进入面板内部，关闭后还给打开它的元素', async () => {
    const trigger = document.createElement('button')
    trigger.textContent = '打开'
    document.body.appendChild(trigger)
    trigger.focus()
    expect(document.activeElement).toBe(trigger)

    const wrapper = mountModal()
    await flushPromises()

    const dialog = panel(wrapper).element as HTMLElement
    expect(dialog.contains(document.activeElement)).toBe(true)

    await wrapper.setProps({ isOpen: false })
    await nextTick()
    // reka 在 unmount 清理里用 setTimeout(0) 派发 closeAutoFocus。
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(document.activeElement).toBe(trigger)

    trigger.remove()
  })
})
