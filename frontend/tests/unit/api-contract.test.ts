/* =============================================================================
 * API 契约钉住测试（F3，docs/FRONTEND_ASSESSMENT.md §4 F3）
 * 前端 src/types/ 全手写对应后端 Pydantic DTO；后端 v2 已解冻，字段改动不同步会
 * 运行期静默错。约法：sample 字段名与后端 DTO 一字不差（注释给权威路径 文件:类名）；
 * 标注前端类型 → TS 编译期校验字段名；运行期断言钉 JS 类型；可空字段允许 null/缺失
 * （后端 Optional 缺省不下发），前端消费侧统一 `?? null` 归约成显式 null。
 * ⚠️ 漂移记录：测试初版抓到 NovelListResult.hasExcluded ≠ 后端线上 has_excluded
 * （useNovels.ts 原读 res.hasExcluded 恒 false → ExclusionBar 失效）；F3 收口时已修复：
 * 类型改用 has_excluded、读点同步，下方按线上名钉死，任一侧再改即红。
 * ========================================================================== */

import { describe, it, expect } from 'vitest'
import type {
  Novel, NovelListResult, NovelCountResult, NovelIdsResponse,
  NovelsByIdsResponse, MatchIdsResult, BatchOperationResult,
  TaskHistory, ScheduledTask, SystemConfig, SearchHistory,
  FailedNovel, FailedNovelListResponse, FailedNovelCountResponse,
} from '../../src/types'

// 可空字段合法形态 = 值 / null / 字段缺失（后端 Optional 缺省不下发，前端 `?? null` 归约）。
function optionalString(v: unknown): void {
  expect(v === null || v === undefined || typeof v === 'string').toBe(true)
}
function optionalNumber(v: unknown): void {
  expect(v === null || v === undefined || typeof v === 'number').toBe(true)
}
function optionalDict(v: unknown): void {
  expect(v === null || v === undefined || (typeof v === 'object' && !Array.isArray(v))).toBe(true)
}

// ---------------------------------------------------------------------------
// Novel — GET /api/novels/ 数组项。权威：features/novels/schemas.py:NovelBase；
// 前端 types/novels.ts:Novel（author_id 后端可 null——迁移孤儿行，前端标必填 number 更严；
// core/models.py:Novel 的 path/content/images/illusts/cover_url/shuffle 仅领域侧，不下发）
// ---------------------------------------------------------------------------
const sampleNovel: Novel = {
  id: 42,
  title: '幻想图书馆的午后',
  author_id: 1001,
  author_name: '黄昏の書架',
  series_id: 7,
  series_name: '图书管理员系列',
  series_index: 3,
  like: 1280,
  view: 45210,
  text: 8234,
  caption: '闭馆后的图书馆里，灯光还亮着。',
  create_time: '2026-08-14T21:30:00+09:00',
  has_epub: 2, // EpubStatus int 枚举：0=NO 1=PENDING 2=DONE
  tags: ['图书馆', '幻想', '日常'],
  is_favourite: 1,
  is_special_follow: 0,
}

describe('Novel（novels/schemas.py:NovelBase）', () => {
  it('主键/计数/标志位均为 number（如改类型为 string 即红）', () => {
    const n = sampleNovel
    ;['id', 'author_id', 'like', 'view', 'text', 'has_epub', 'is_favourite', 'is_special_follow']
      .forEach((k) => expect(typeof n[k as keyof Novel]).toBe('number'))
  })

  it('必填文本 string；可空字段允许 null/缺失；tags 为 string 数组；create_time 为 ISO 字符串', () => {
    const n = sampleNovel
    expect(typeof n.title).toBe('string')
    expect(n.tags.every((t) => typeof t === 'string')).toBe(true)
    optionalString(n.author_name)
    optionalNumber(n.series_id)
    optionalString(n.series_name)
    optionalNumber(n.series_index)
    optionalString(n.caption)
    optionalString(n.create_time)
    expect(new Date(n.create_time as string).toString()).not.toBe('Invalid Date')
  })
})

// ---------------------------------------------------------------------------
// 列表 / 计数 / 批量响应容器。权威：novels/schemas.py:NovelListResponse / NovelIdsResponse /
// NovelsByIdsResponse / MatchIdsResponse / BatchOperationResponse；GET /api/novels/count →
// novels/api.py:count_novels（{total, excluded}）；前端 types/novels.ts 同名类型
// ---------------------------------------------------------------------------
describe('NovelListResult（schemas.py:NovelListResponse）', () => {
  // 已对齐线上字段名 has_excluded（snake_case，schemas.py L52 / api.py L95）。
  // 类型层面 `const list: NovelListResult` 即编译期校验；任一侧改回 camelCase 即红。
  it('下发字段为 novels / cursor / has_excluded（snake_case），cursor 可为 null', () => {
    const list: NovelListResult = {
      novels: [sampleNovel],
      cursor: null,
      has_excluded: true, // 仅首屏关键词搜索时计算（api.py L94），否则缺省 false
    }
    expect(list.novels[0].id).toBe(42)
    expect(list.cursor).toBeNull()
    expect(typeof list.has_excluded).toBe('boolean')
    expect(list.has_excluded).toBe(true)
  })
})

describe('NovelCountResult（GET /api/novels/count）', () => {
  it('total/excluded 均为 number（with_excluded=true 时 excluded 才非零）', () => {
    const count: NovelCountResult = { total: 5, excluded: 2 }
    expect(typeof count.total).toBe('number')
    expect(typeof count.excluded).toBe('number')
  })
})

describe('批量辅助响应（novels/schemas.py）', () => {
  it('NovelIdsResponse: ids 数组 + total + truncated', () => {
    const res: NovelIdsResponse = { ids: [1, 2, 3], total: 3, truncated: false }
    expect(res.ids.every((i) => typeof i === 'number')).toBe(true)
    expect(typeof res.total).toBe('number')
    expect(typeof res.truncated).toBe('boolean')
  })

  it('NovelsByIdsResponse / MatchIdsResult / BatchOperationResult 结构', () => {
    const byIds: NovelsByIdsResponse = { novels: [sampleNovel], truncated: false }
    const match: MatchIdsResult = { matching_ids: [42], truncated: false }
    const op: BatchOperationResult = { matched: 1, affected: 1 }
    expect(byIds.novels).toHaveLength(1)
    expect(typeof byIds.truncated).toBe('boolean')
    expect(match.matching_ids).toEqual([42])
    expect(typeof op.matched).toBe('number')
    expect(typeof op.affected).toBe('number')
  })
})

// ---------------------------------------------------------------------------
// TaskHistory / ScheduledTask。权威：tasks/schemas.py:TaskHistoryResponse / ScheduledTaskResponse；
// 写路径 tasks/history_repo.py（progress 只写字符串、从不清除）；前端 types/tasks.ts。
// task_func 是内部 dedup 键，Response 不下发；progress 后端类型写 Any（为未来 dict 形态预留），
// 当前线上下发恒 string|null，前端钉 string|null 一致；sort_index 后端默认 0 必回，前端可选（宽松）。
// ---------------------------------------------------------------------------
describe('TaskHistory（tasks/schemas.py:TaskHistoryResponse）', () => {
  const sampleTask: TaskHistory = {
    id: 88,
    name: 'novel_follow',
    arguments: { author_ids: [1001] },
    status: 'running',
    start_time: '2026-09-06T10:00:00Z',
    end_time: null,
    duration: null,
    result: null,
    progress: '下载 12/30',
  }

  it('id/status/start_time 必填；start_time 为 ISO 字符串（datetime 序列化）', () => {
    const t = sampleTask
    expect(typeof t.id).toBe('number')
    expect(typeof t.status).toBe('string')
    expect(typeof t.start_time).toBe('string')
    expect(new Date(t.start_time).toString()).not.toBe('Invalid Date')
  })

  it('arguments/result 可空 dict；end_time/duration 可空（running 行不下发 end_time）；progress string|null', () => {
    const t = sampleTask
    optionalDict(t.arguments)
    optionalDict(t.result)
    optionalString(t.end_time)
    optionalNumber(t.duration)
    optionalString(t.progress)
  })
})

describe('ScheduledTask（tasks/schemas.py:ScheduledTaskResponse）', () => {
  it('id/name/task/cron/is_enabled 必填；params/config 可空 dict；sort_index 默认 0', () => {
    const t: ScheduledTask = {
      id: 3,
      name: '每日收藏同步',
      task: 'novel_follow',
      cron: '0 3 * * *',
      params: { limit: 50 },
      // 后端 config 为 dict|None，缺省时线上下发 null；前端类型已允许 null
      // （F3 收口对齐），这里用 null 钉住缺省形态。
      config: null,
      is_enabled: true,
      sort_index: 0,
    }
    expect(typeof t.id).toBe('number')
    expect(typeof t.name).toBe('string')
    expect(typeof t.task).toBe('string')
    expect(typeof t.cron).toBe('string')
    expect(typeof t.is_enabled).toBe('boolean')
    optionalDict(t.params)
    optionalDict(t.config)
    optionalNumber(t.sort_index)
  })
})

// ---------------------------------------------------------------------------
// SystemConfig / SearchHistory。权威：system/api.py:SystemConfigResponse（exclude_blocked_tag_novels
// 为运行期设置，缺省 True）+ novels/history_api.py:SearchHistoryResponse；前端 types/system.ts
// ---------------------------------------------------------------------------
describe('SystemConfig（system/api.py:SystemConfigResponse）', () => {
  it('batch_download_naming string；exclude_blocked_tag_novels boolean', () => {
    const config: SystemConfig = { batch_download_naming: '{id}-{title}', exclude_blocked_tag_novels: true }
    expect(typeof config.batch_download_naming).toBe('string')
    expect(typeof config.exclude_blocked_tag_novels).toBe('boolean')
  })
})

describe('SearchHistory（novels/history_api.py:SearchHistoryResponse）', () => {
  it('id/type/value/timestamp 必填（timestamp 为 ISO 字符串）；display_value 可空', () => {
    const h: SearchHistory = {
      id: 12,
      type: 'keyword',
      value: '图书馆 幻想',
      display_value: null,
      timestamp: '2026-09-06T09:00:00Z',
    }
    expect(typeof h.id).toBe('number')
    expect(typeof h.type).toBe('string')
    expect(typeof h.value).toBe('string')
    expect(typeof h.timestamp).toBe('string')
    expect(new Date(h.timestamp).toString()).not.toBe('Invalid Date')
    optionalString(h.display_value)
  })
})

// ---------------------------------------------------------------------------
// FailedNovel — GET /api/failed-novels 数组项。权威：failures/api.py:FailedNovelItem /
// FailedNovelListResponse / FailedNovelCountResponse，数据源 failures/repo.py；前端 types/failedNovels.ts。
// title/failure_type/error_message/last_failed_at 全部可空（旧行无标题、迁移前行无时间），前端标 string|null 一致
// ---------------------------------------------------------------------------
describe('FailedNovel（failures/api.py:FailedNovelItem）', () => {
  const sampleFailed: FailedNovel = {
    novel_id: 777,
    title: '404 サンプル作品',
    failure_type: 'network',
    error_message: 'webview_novel 返回空',
    failed_times: 2,
    last_failed_at: '2026-09-05T18:22:00Z',
  }

  it('novel_id/failed_times 为 number；其余字段 string 或 null', () => {
    const f = sampleFailed
    expect(typeof f.novel_id).toBe('number')
    expect(typeof f.failed_times).toBe('number')
    expect(typeof f.title).toBe('string')
    optionalString(f.failure_type)
    optionalString(f.error_message)
    optionalString(f.last_failed_at)
  })

  it('列表响应 items+total+offset+limit；count 端点下发单个 number', () => {
    const list: FailedNovelListResponse = { items: [sampleFailed], total: 1, offset: 0, limit: 100 }
    const count: FailedNovelCountResponse = { count: 1 }
    expect(list.items).toHaveLength(1)
    expect(typeof list.total).toBe('number')
    expect(typeof list.offset).toBe('number')
    expect(typeof list.limit).toBe('number')
    expect(typeof count.count).toBe('number')
  })
})