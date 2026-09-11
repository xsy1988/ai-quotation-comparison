// 与 backend/app/compare/compare_engine.py 的 get_comparison 输出对齐
// 空值语义：供应商未报某行/抽屉时值为 null（渲染"/"），绝不当作 0

export interface Supplier {
  quote_id: number
  supplier_name: string
  supplier_code: string | null
  part_name: string | null
  scheme: string | null
  flags: string[]
  calc_check: string | null
  final_unit_price_taxed: number | null
  category_code: string | null
}

/** hierarchy[].key ∈ HIERARCHY_ROWS：六个模块合计 + 汇总行 */
export type HierarchyKey =
  | 'materials'
  | 'processing'
  | 'inspection'
  | 'packaging_transport'
  | 'sga_tax'
  | 'other'
  | 'untaxed_total'
  | 'tax_amount'
  | 'taxed_total'
  | 'discount'
  | 'final_unit_price_taxed'

export interface HierarchyRow {
  key: HierarchyKey
  label: string
  /** quote_id -> 金额（null = 未报） */
  values: Record<string, number | null>
}

export interface ProcessingItem {
  /** quote_line id，就地编辑 PATCH /api/quote_lines/{id} 用 */
  id: number
  name: string
  amount: number | null
  atom_code: string | null
  confidence: string | null
  fingerprint: string | null
  bundle_flag: boolean
  is_new_process: boolean
  /** 共享单元格去重的置零副本（derive 规则 A）：金额列显示"/"，备注 tooltip 展示 */
  is_shared: boolean
  note: string | null
}

export interface ProcessingDetail {
  quote_id: number
  module: 'processing'
  items: ProcessingItem[]
}

// ---------- 层级金额对比树（price_tree，get_comparison 新增字段） ----------

/** 每供应商基本信息（quote.basic_info 解析，字段缺失为 null） */
export interface BasicInfo {
  quote_id: number
  project_name: string | null
  part_name: string | null
  material_spec: string | null
  quote_date: string | null
  currency: string | null
  moq: number | null
}

/** price_tree 明细行的 meta（按 quote_id 各存一份，均可能缺省） */
export interface PriceTreeMeta {
  /** quote_line id：加工费明细行就地编辑用 */
  id?: number
  atom_code?: string | null
  confidence?: string | null
  match_path?: string | null
  is_new_process?: boolean
  bundle_flag?: boolean
  fingerprint?: string | null
  /** 包装运输/损管利/税费条目的类型标签（包装/运输、损耗/管理费/利润/其他、税费） */
  item_type?: string | null
  /** 税费条目税率（0.13 = 13%） */
  rate?: number | null
  /** 材料/检验等条目的备注（原 quote_line.spec 未落库，仅有 note） */
  note?: string | null
  /** 该供应商此条目的原文名称（加工费/材料明细同格展示用） */
  name?: string | null
  /** 共享单元格去重置零副本 → 金额显示"/"（derive 规则 A） */
  is_shared?: boolean
  cavity_count?: number | null
  lifespan?: number | null
}

export interface PriceTreeNode {
  key: string
  label: string
  kind: 'group' | 'amount' | 'text'
  /** quote_id -> 金额或文本（null = 未报） */
  values: Record<string, number | string | null>
  children?: PriceTreeNode[]
  meta?: Record<string, PriceTreeMeta>
  /** 加工费明细行：原子所属工艺域/阶段/类别（未匹配条目为 null），供切换分组 */
  scope_meta?: {
    domain?: { code: string; name: string } | null
    stage?: { code: string; name: string } | null
    class?: { code: string; name: string } | null
  } | null
}

export type DrawerScope = 'process_domain' | 'process_stage' | 'process_class'

export interface DrawerGroup {
  group_code: string
  group_name: string
  /** quote_id -> 该抽屉加工费合计（null = 未报） */
  values: Record<string, number | null>
  is_fallback_bucket: boolean
}

export interface Drawer {
  scope: DrawerScope
  groups: DrawerGroup[]
}

export interface FingerprintRow {
  quote_id: number
  item_name: string
  amount: number | null
}

export interface FingerprintGroup {
  fingerprint: string
  rows: FingerprintRow[]
}

export type ToolingType = 'mold' | 'fixture' | 'stencil'

export interface ToolingItem {
  quote_id: number
  item_name: string
  amount: number | null
  cavity_count: number | null
  lifespan: number | null
}

export interface ToolingBlock {
  type: ToolingType
  items: ToolingItem[]
}

export type WarningCategory = 'calc' | 'unmatched' | 'low_confidence' | 'new_process'

export interface WarningEntry {
  quote_id: number
  flags: string[]
  line_counts: {
    low_confidence: number
    unmatched: number
    new_process: number
  }
}

export interface Comparison {
  task_id: number
  suppliers: Supplier[]
  hierarchy: HierarchyRow[]
  processing_details: ProcessingDetail[]
  drawers: Drawer[]
  fingerprint_groups: FingerprintGroup[]
  tooling: ToolingBlock[]
  warnings: WarningEntry[]
  basic: BasicInfo[]
  price_tree: PriceTreeNode[]
}

// ---------- 任务 / 进度 ----------

export interface TaskListItem {
  id: number
  project_name: string
  status: string
  created_at: string
  quote_count: number
}

export interface QuoteProgress {
  quote_id: number
  supplier_name: string
  parse_status: string
  stage: string | null
  /** 该 quote 最近一条 parse_log 的 action（如 retry_round），配合 detail 展示重试轮次 */
  action?: string | null
  /** 该 quote 最近一条 parse_log 的 detail（JSON）；retry_round 时带 round/reason */
  detail?: { round?: number; reason?: string } | null
  error?: string | null
}

export interface ProgressPayload {
  task_status: string
  quotes: QuoteProgress[]
}

export interface CreateTaskResponse {
  task_id: number
}

// ---------- AI 综合建议 ----------

export interface AiSummary {
  id: number
  task_id: number
  content: string
  /** pass = 数字回检通过；mismatch = 部分数字与机械对比不一致 */
  check_status: 'pass' | 'mismatch'
  /** JSON：数字回检明细（回检轮次、抽查数、可疑数列表） */
  check_detail: {
    rounds: number
    checked: number
    suspicious: { value: number }[]
  } | null
  created_at: string
}

export interface AiSummaryResponse {
  summary: AiSummary | null
}

// ---------- 就地编辑（第 7 步） ----------

export interface AtomOption {
  code: string
  name: string
}

export interface CategoryOption {
  code: string
  name: string
}

export interface QuotePatch {
  part_name?: string
  material_spec?: string
  quote_date?: string
  currency?: string
  moq?: number
  quote_no?: string
  supplier_name?: string
  category_code?: string
  module_totals?: Record<string, number | null>
  discount?: number
}

export interface QuoteLinePatch {
  amount?: number
  atom_code?: string
  note?: string
  confirm_status?: 'confirmed' | 'corrected'
}

export interface QuotePatchResult {
  quote_id: number
  supplier_name: string | null
  category_code: string | null
  parse_status: string
  calc_check: string | null
  flags: string[]
  summary: Record<string, number | null>
}

export interface QuoteLinePatchResult extends Record<string, unknown> {
  id: number
  quote_id: number
  module: string
  item_name: string
  amount: number | null
  atom_code: string | null
  match_path: string | null
  confidence: string | null
  confirm_status: string
}

// ---------- 新工艺决策（第 8 步） ----------

export interface NewAtomSuggestion {
  id: number
  /** 条目原文写法 */
  source_text: string
  suggested_name: string | null
  suggested_domain_code: string | null
  suggested_stage_name: string | null
  occurrence_count: number
  quote_line_id: number
  quote_id: number
  amount: number | null
  supplier_name: string | null
}

export interface SuggestionResolveResult {
  suggestion_id: number
  action: 'create' | 'merge' | 'ignore'
  atom_code?: string
}

export interface DomainOption {
  code: string
  name: string
}

// ---------- 主数据管理（第 9 步） ----------

export interface MasterAtom {
  code: string
  name: string
  domain_code: string
  domain_name: string
  stage_name: string
  class_name: string
  remark: string | null
  is_fallback: boolean
  /** 关联品类名列表 */
  categories: string[]
  created_at: string
  updated_at: string
}

export interface MasterAlias {
  id: number
  atom_code: string
  alias_text: string
  atom_name: string
  source: 'initial' | 'manual_feedback' | 'new_process'
  hit_count: number
  created_at: string
  updated_at: string
}

export interface MasterCategory {
  code: string
  name: string
  atom_count: number
  created_at: string
  updated_at: string
}

export type DimGroupScope = 'process_domain' | 'process_stage' | 'process_class' | 'custom'

export interface MasterDimGroup {
  group_code: string
  group_name: string
  scope: DimGroupScope
  parent_code: string | null
  parent_name: string | null
  member_atoms: string[]
  member_count: number
  is_builtin: boolean
  created_at: string
  updated_at: string
}

export interface MasterSupplier {
  code: string
  name: string
  alias: string | null
  created_at: string
  updated_at: string
}

export interface MasterAtomCreatePayload {
  name: string
  domain_code: string
  stage_name: string
  class_name: string
  remark?: string
  category_codes?: string[]
}
