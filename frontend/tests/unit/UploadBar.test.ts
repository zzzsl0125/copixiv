import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import UploadBar from '../../src/components/features/UploadBar.vue'
import { BAR_SHELL_CLASS, barBtnClass } from '../../src/lib/ui'

/**
 * 「我的上传」工具条（docs/LIBRARY_DESIGN.md §5）。
 *
 * 它是列表顶部栏这一族的一员，样式与 BatchBar / ExclusionBar **同源**
 * （`lib/ui.ts`）——所以这里不只钉行为，也钉"确实用的是同一套栏外壳/按钮样式"，
 * 否则三条栏各自抄一份类名，改一处漏两处。
 */
describe('UploadBar', () => {
  it('渲染栏外壳与上传按钮，并用共享的栏样式', () => {
    const wrapper = mount(UploadBar)

    const bar = wrapper.get('[data-testid="uploads-bar"]')
    expect(bar.classes()).toContain(BAR_SHELL_CLASS.split(' ')[0])
    expect(bar.classes()).toContain('border-b')

    const btn = wrapper.get('[data-testid="uploads-bar-upload"]')
    expect(btn.text()).toContain('上传 TXT')
    // 主按钮态来自同一份共享样式（含禁用态与图标间距）
    expect(btn.classes()).toEqual(expect.arrayContaining(barBtnClass('primary').split(' ')))
  })

  it('点击按钮 emit upload，且不触发任何路由跳转（弹窗，不新增路由）', async () => {
    const wrapper = mount(UploadBar)

    await wrapper.get('[data-testid="uploads-bar-upload"]').trigger('click')

    expect(wrapper.emitted('upload')).toHaveLength(1)
  })

  it('栏内只保留标识与按钮：不放大段说明文案', () => {
    const wrapper = mount(UploadBar)

    expect(wrapper.text()).toContain('我的上传')
    expect(wrapper.text()).toContain('上传 TXT')
    // 说明文案（"本地 TXT 收进书库；重复上传会被拒收…"）已按要求移除
    expect(wrapper.text()).not.toContain('拒收')
    expect(wrapper.text()).not.toContain('收进书库')
  })
})
