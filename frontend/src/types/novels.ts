/** Novel domain types */

export interface Novel {
  id: number
  title: string
  /**
   * pixiv 作者 id —— **本地书为 null**（上传时 `author_id=NULL`，见
   * LIBRARY_DESIGN.md §3.3），迁移孤儿行同样如此。编辑器「作者」一栏读写的
   * 就是它；`author_name` 只是它的解析结果（2026-09-19 拍板）。
   */
  author_id?: number | null
  author_name?: string
  /**
   * pixiv 系列 id —— 与 `author_id` 同样可空：本地书上传时 `series_id=NULL`，
   * 单篇（不属于任何系列）的 pixiv 小说也是 NULL。编辑器「系列」一栏读写它。
   */
  series_id?: number | null
  series_name?: string
  series_index?: number
  like: number
  view: number
  text: number
  caption?: string
  create_time?: string
  has_epub: number
  tags: string[]
  is_favourite: number
  is_special_follow: number
  /** 来源：'pixiv'（缺省）/ 'local'（本地上传的 TXT / EPUB，id 为负）。见 LIBRARY_DESIGN.md §2 */
  source?: string
  /**
   * **服务端下发的字段策略**：这本书允许 PATCH 提交哪些字段（后端 `NovelBase`
   * 新增，列表项与 PATCH / POST sync 响应都带）。
   *
   * 取值只有两种形态：
   * - 本地书：`['title','author_id','series_id','series_index','tags','create_time']`
   * - pixiv 书：`['series_index','tags','create_time']`
   *
   * pixiv 书收掉那三列的理由**按字段不同**（唯一所有者是后端
   * `features/novels/field_policy.py`，这里只是转述它的口径）：
   * - `title` / `series_id` 是**"来源会覆盖"**：来源刷新无条件覆盖 `title`；
   *   而 `series_name` 在同一个覆盖白名单里——改掉 id 后名字下一轮就被顶回去
   *   （id 指向新系列、名字还是旧的），手改只是制造"改完又变回去"的假象；
   * - `author_id` **不是覆盖问题**：它不在覆盖白名单里（"非空者胜"），手改不会
   *   被打回，而是永久让"这本书的作者是谁"与来源不符——更隐蔽，所以同样只读；
   *   它的理由是**"作者身份以来源为准"**。
   *
   * 两类错配的修正入口都不是手改，而是**同步**（`POST /api/novels/{id}/sync`：
   * 按 id 从来源重抓、恢复来源值）。
   *
   * 前端**只读这个数组**决定置灰与收口，不再自己按 `source` 推一份"哪个来源能改
   * 哪些字段"——那份表在服务端执法（`update_metadata.py`），在前端复制一遍就是
   * 第二份真相，两边一漂移就会出现"能点但保存 400"（pixiv 书提交这三列）。
   */
  editable_fields: string[]
}

export interface NovelFilters {
  keyword: string
  order_by: string
  order_direction: string
  min_like?: number
  min_text?: number
}

/**
 * 单本元数据编辑的可提交字段（`PATCH /api/novels/{id}`，docs/NOVEL_EDIT_DESIGN.md §5）。
 *
 * PATCH 三态：键不出现 = 不动；显式 `null` = 清空（`series_index` /
 * `create_time` / `caption` / `author_id` / `series_id`）。
 *
 * 服务端执法（不是静默忽略）——前端据此置灰的那份策略就是响应里的
 * `Novel.editable_fields`，理由与 400 文案的唯一所有者在后端
 * `features/novels/field_policy.py`：
 * - `title` / `author_id` / `series_id` / `caption` **仅本地书**可提交，pixiv 书
 *   提交 → 400（文案由 `field_policy.rejection_reason` 按字段给）；四者进这条
 *   规则的理由并不相同：`title` / `series_id` / `caption` 是"来源会覆盖"
 *   （§5.6：`series_name` 在覆盖白名单里，id 改了名字会被顶回去；`caption`
 *   同理，所以此前干脆全局不可编辑——本地书永不进那条写路径，2026-09-22
 *   因此只对本地书放开），`author_id` 是"作者身份以来源为准"——来源压根不覆盖
 *   它（"非空者胜"），手改不会被打回，而是永久与来源不符。
 *   四类错配的修正入口都是「同步」，不是手改（见 `Novel.editable_fields`）；
 * - **`author_name` / `series_name` 同样不可提交**（2026-09-19 拍板）：作者与
 *   系列编辑的都是 id，显示名是它的解析结果（本地表 → pixiv 兜底）。
 *   `author_id` / `series_id` 给 `null` = 解除关联，但**收尾不同**（2026-09-22）：
 *   作者保留显示名（名字有独立来源＝文件名解析）；系列连 `series_name` 与
 *   `series_index` 一起清——本地书的系列名只可能由 id 解析而来，留着就是卡片上
 *   一串点不动、也删不掉的文字。
 */
export interface NovelUpdatePayload {
  title?: string
  author_id?: number | null
  series_id?: number | null
  series_index?: number | null
  tags?: string[]
  create_time?: string | null
  /** 仅本地书；空串提交 `null`（清空），服务端上限 2000 字。 */
  caption?: string | null
}

/**
 * 作者 ID 预览（`GET /api/novels/author/{author_id}`）。
 *
 * 保存前告诉用户这个名字对不对；服务端用的是同一个解析函数，所以预览与
 * 实际落库一致。`source` 说明名字是哪来的：
 * - `local`：库里已知（离线可用）；
 * - `pixiv`：本次现查（说明网络通、作者存在）；
 * - `unknown`：拿不到名字 —— **不影响可保存性**，id 才是身份。
 */
export interface AuthorLookup {
  author_id: number
  author_name?: string | null
  source: 'local' | 'pixiv' | 'unknown'
  /** 库内已有多少本挂在这个 id 下（拼错 id 的直觉校验） */
  novel_count: number
}

/**
 * 系列 ID 预览（`GET /api/novels/series/{series_id}`）—— 与 `AuthorLookup` 同构。
 *
 * 名字的来源链更长：`series` 行 → 同系列的 novel 行 → pixiv `novel_series`；
 * 前两步都是本地查询（离线也能预览），第三步才打网络。
 */
export interface SeriesLookup {
  series_id: number
  series_name?: string | null
  source: 'local' | 'pixiv' | 'unknown'
  novel_count: number
}

export interface GetNovelsParams {
  keyword?: string
  order_by?: string
  order_direction?: string
  cursor?: Record<string, unknown>
  per_page?: number
  min_like?: number
  min_text?: number
  excluded_ids?: number[]
  /** 排除厌恶标签小说：true/undefined → 排除；false → 本次不排除 */
  exclude_blocked?: boolean
}

/**
 * /api/novels/count 响应：total 可见数，excluded 被厌恶标签隐藏数。
 *
 * 服务端计数**有界**（后端 `repo.COUNT_CAP`，与同步批量操作上限
 * `BATCH_MAX_NOVELS` 同值）：命中数超过上限时 `total` 只是下界，
 * `truncated` 为 true，展示必须是 `5000+` / `≥ 5000`，不能当成精确值。
 * 两个标志在旧后端上缺席（undefined）—— 缺席即「精确」，与老行为一致。
 */
export interface NovelCountResult {
  total: number
  excluded: number
  /** true = 真实匹配数 > total（total 是下界） */
  truncated?: boolean
  /** true = 被隐藏数 > excluded（excluded 是下界） */
  excluded_truncated?: boolean
}

/** /api/novels/ 响应：has_excluded 首屏附带「范围内是否存在被厌恶标签排除的小说」 */
export interface NovelListResult {
  novels: Novel[]
  cursor: Record<string, unknown> | null
  has_excluded: boolean
}

/** Which novels a batch operation applies to (mirrors backend BatchScope). */
export interface BatchScope {
  mode: 'ids' | 'all_matched'
  novel_ids: number[]
  keyword?: string
  min_like?: number
  min_text?: number
  excluded_ids: number[]
}

export type BatchOperation = 'delete' | 'add_tags' | 'remove_tags'

export interface BatchOperationResult {
  matched: number
  affected: number
}

/**
 * 批量编辑「作者」/「系列」（`POST /api/novels/batch-metadata`）。
 *
 * 三态与单本 `NovelUpdatePayload` 同源（键不出现 = 不改；显式 `null` = 解除关联），
 * 但**"留空"的含义刻意不同**：单本弹窗里留空就是解除关联（"这本书"是明确的），
 * 批量里一批书各自挂着不同的作者，空输入框不可能是它们的共同意图——所以留空
 * = 不改，解除关联必须显式勾选，由前端把那个勾翻成 `null`。
 *
 * `apply: false` 是预演（只算数不写库），语义与响应形状和真执行一致。
 */
export interface BatchMetadataRequest {
  scope: BatchScope
  /** 键不出现 = 不改作者；显式 `null` = 解除关联（收尾见 `NovelUpdatePayload`）。 */
  author_id?: number | null
  /** 键不出现 = 不改系列；显式 `null` = 解除关联（连显示名与章节号一起清）。 */
  series_id?: number | null
  /** false = 预演（只算数不写库）；true = 真执行。 */
  apply: boolean
}

/**
 * 批量编辑作者 / 系列的响应（`apply: false/true` 同一形状；预演时 `affected = 0`）。
 *
 * - `matched`：选择集总数；
 * - `applicable` / `skipped`：其中**本地书** / **pixiv 书**的数量。
 *
 * 只作用于本地书是服务端的执法：这两列在单本编辑里对 pixiv 书硬拒 400
 * （"作者身份以来源为准"），批量改成"预先报数 + 跳过"。所以前端**不自己按
 * `source` 过滤**（那是第二份真相），只把服务端给的数字摆出来。
 */
export interface BatchMetadataResult {
  matched: number
  applicable: number
  skipped: number
  affected: number
}

/** Matching IDs for the 「全选匹配」bulk-add action. */
export interface NovelIdsResponse {
  ids: number[]
  total: number
  truncated: boolean
}

/** Novels fetched by explicit ID list (「查看已选」view). */
export interface NovelsByIdsResponse {
  novels: Novel[]
  truncated: boolean
}

/** Intersection of a selection with the current search scope (scoped clear). */
export interface MatchIdsResult {
  matching_ids: number[]
  truncated: boolean
}
