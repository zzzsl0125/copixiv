/**
 * Frontend-bundled config defaults.
 *
 * The backend no longer serves ``default_min_like`` / ``default_min_text``
 * (the ``frontend`` config section was removed in the config cleanup); the
 * default filter values live here in the client instead.
 */

export const DEFAULT_MIN_LIKE = 500
export const DEFAULT_MIN_TEXT = 3000

/**
 * 书架「section」单源表（docs/LIBRARY_DESIGN.md §5）。
 *
 * 一个 section = 一次特殊搜索：侧边栏按钮 emit 的 keyword / section 名、
 * `syncActiveSection()` 从 keyword 的反推、以及该 section 的阈值 / 默认排序，
 * 三处共用这一张表——任何一处再出现写死的 `'is_favourite:true;'` 之类的字面量，
 * 就意味着表被绕过了。
 *
 * 阈值为什么必须随 section 走——**书架态不设门槛，浏览态才设**：
 * 服务端按 `like >= min_like` / `text >= min_text` 过滤（features/novels/repo.py:512-520），
 * 而「我的收藏 / 特别关注 / 我的上传」是「我的东西」的完整清单——首屏带门槛就是
 * 静默丢内容（本地书 `like=0`，不归零则「我的上传」恒空；特别关注曾因 500/3000
 * 藏掉 1395/4371 篇）。`novels`（小说列表）是浏览态，门槛才有筛选意义。
 *
 * 回归警示：`32a13bb` 之前，点任何书架都经 `useNovels.applySearchOrdering()` 把两个
 * 阈值归零；收成这张表时只给 uploads 写了 0，收藏/特别关注照抄了 `DEFAULT_MIN_*`，
 * 于是"点开书架 = 500/3000"这个回归悄悄活了半年。新增 section 请先问：
 * 它是「我的清单」还是「浏览」。
 */
export interface SectionDef {
  /** 触发本次搜索的 DSL 关键词（`''` = 小说列表/无关键词）。 */
  keyword: string
  min_like: number
  min_text: number
  /** 该 section 的默认排序（点击侧边栏进入时套用）。 */
  order_by: string
  order_direction: string
}

export const SECTIONS = {
  novels: { keyword: '', min_like: DEFAULT_MIN_LIKE, min_text: DEFAULT_MIN_TEXT, order_by: 'random', order_direction: 'DESC' },
  // 书架态（我的清单）：阈值 0/0，服务端 `min_like > 0` 不成立即不加条件——
  // 点开即完整清单，门槛交给用户自己点筛选条（applySection 只是把表里的值抄进 filters）。
  favourites: { keyword: 'is_favourite:true;', min_like: 0, min_text: 0, order_by: 'id', order_direction: 'DESC' },
  special_follow: { keyword: 'is_special_follow:true;', min_like: 0, min_text: 0, order_by: 'id', order_direction: 'DESC' },
  // 本地书：like/view 一律 0（LIBRARY_DESIGN.md §3.3），阈值必须为 0；
  // id 是**负的时间坐标**（§3.1），时间在模上：服务端按 `abs(id)` 排序，
  // 所以 `order_by: 'id'` + DESC 仍是「最新上传在前」。
  uploads: { keyword: 'source:local;', min_like: 0, min_text: 0, order_by: 'id', order_direction: 'DESC' },
} satisfies Record<string, SectionDef>

export type SectionId = keyof typeof SECTIONS

/**
 * keyword → section 的反推（`syncActiveSection()` 的唯一判据）。
 * 非空且不匹配任何 section 的 keyword（自由搜索 / 卡片下钻）返回 null；
 * 空 keyword 即 `novels`。
 */
export function resolveSection(keyword: string): SectionId | null {
  // 无关键词 = 小说列表（SECTIONS.novels.keyword 就是 ''）。
  if (!keyword) return 'novels'
  const found = (Object.keys(SECTIONS) as SectionId[]).find(
    (id) => SECTIONS[id].keyword !== '' && SECTIONS[id].keyword === keyword,
  )
  return found ?? null
}

/**
 * 该 keyword 生效的 section 定义：命中某 section 用它，自由搜索回落到
 * `novels`（阈值/排序的缺省语义）。
 *
 * 注意与 `resolveSection()` 的区别：后者返回 null 表示「侧边栏无高亮」，
 * 但阈值仍要有确定取值——自由搜索不能继承上一个 section 的 0/0 阈值，
 * 否则「我的上传」之后再搜别的关键词会悄悄失去 min_like 过滤。
 */
export function sectionFor(keyword: string): SectionDef {
  return resolveSection(keyword) ? SECTIONS[resolveSection(keyword) as SectionId] : SECTIONS.novels
}

/** 分页页大小：浏览列表（useNovels per_page）与「查看已选/被排除」集合视图
 * （usePagedNovelIdView 每页切片）共用，改页大小只改这一处。 */
export const DEFAULT_PAGE_SIZE = 30
