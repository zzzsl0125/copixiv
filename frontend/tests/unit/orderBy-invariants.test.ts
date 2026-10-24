import { describe, it, expect } from 'vitest'
import { SECTIONS } from '../../src/config'
import { parseOrderBy, parseOrderDirection, isOrderBy, isOrderDirection } from '../../src/types'

/**
 * 排序字段/方向这两组「注释守着的约定」，现在改由类型 + 这个文件一起守。
 *
 * 背景：`GET /api/novels/` 的 `order_by` 在 OpenAPI 里**没有 enum**（Pydantic 侧是
 * 裸 `str`），后端 `repo.py` 对未知字段走 `getattr(model, order_by, None)`，
 * 拿到 None 就**静默不加排序** —— 拼错不会 400，只会让列表悄悄退回默认顺序。
 * 所以「值必须是那 6 个之一」这件事只能由前端自己保证。
 *
 * 静态的那一半（把 `SectionDef.order_by` 从 `string` 收窄成 `OrderBy`）让
 * `SECTIONS` 的 `satisfies` 真正生效；这个文件守动态的那一半：来自 URL 的
 * 不可信输入必须被校验，而不是原样进入 filters。
 */
describe('parseOrderBy / parseOrderDirection（不可信查询参数）', () => {
  it('六个合法排序值原样通过', () => {
    for (const v of ['id', 'like', 'view', 'text', 'create_time', 'random'] as const) {
      expect(parseOrderBy(v)).toBe(v)
    }
  })

  it('缺省（参数不存在）回落到 random', () => {
    expect(parseOrderBy(null)).toBe('random')
  })

  /**
   * 这条是本次改动的**行为**差异点。
   *
   * 旧写法是 `params.get('order_by') || 'random'`：它只兜「没有」，不兜「乱写」。
   * `?order_by=lkke` 会原样进入 filters，发到后端后静默无排序 —— 用户看到的是
   * 「排序按钮点了没反应」，而不是一个错误。
   */
  it('非法值不得原样放出：?order_by=lkke 必须回落 random', () => {
    expect(parseOrderBy('lkke')).toBe('random')
    expect(parseOrderBy('')).toBe('random')
    expect(parseOrderBy('LIKES')).toBe('random')
  })

  it('大小写敏感：LIKE 不是 like', () => {
    expect(isOrderBy('LIKE')).toBe(false)
    expect(isOrderBy('like')).toBe(true)
  })

  it('方向只认 ASC / DESC，其余回落 DESC', () => {
    expect(parseOrderDirection('ASC')).toBe('ASC')
    expect(parseOrderDirection('DESC')).toBe('DESC')
    expect(parseOrderDirection('asc')).toBe('DESC')
    expect(parseOrderDirection('')).toBe('DESC')
    expect(parseOrderDirection(null)).toBe('DESC')
    expect(isOrderDirection('ASC')).toBe(true)
  })
})

/**
 * `SECTIONS` 是「一个 section = 一次特殊搜索」的单源表，阈值/排序/排除开关都在这里。
 * 它的 `satisfies Record<string, SectionDef>` 只有在字段类型被收窄后才有校验力。
 *
 * 这里钉住的是表里每条记录的排序都落在合法集合内 —— 编译期也会拦
 * （实测把 novels 写成 'lkke' 会在 4 处报 TS2322），但类型只在你改代码时
 * 在场，这个用例在 CI 里也在场。
 */
describe('SECTIONS 的排序取值合法性', () => {
  it('每个 section 的 order_by 都在合法集合内', () => {
    for (const [id, def] of Object.entries(SECTIONS)) {
      expect(isOrderBy(def.order_by), `${id} 的 order_by=${def.order_by}`).toBe(true)
    }
  })

  it('每个 section 的 order_direction 都在合法集合内', () => {
    for (const [id, def] of Object.entries(SECTIONS)) {
      expect(isOrderDirection(def.order_direction), `${id} 的方向=${def.order_direction}`).toBe(true)
    }
  })
})
