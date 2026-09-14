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

/** 抽屉编码：内置 3 个（process_domain/process_stage/process_class）+ 自定义，故为自由字符串 */
export type DrawerScope = string

export interface DrawerGroup {
  group_code: string
  group_name: string
  /** quote_id -> 该抽屉加工费合计（null = 未报） */
  values: Record<string, number | null>
  is_fallback_bucket: boolean
}

export interface Drawer {
  scope: DrawerScope
  /** 抽屉名称（来自 drawer 表，后端随比价结果下发） */
  name: string
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

// ---------- AI 分析 ----------

/** 优势/劣势/风险的固定子维度名（与后端 ai_analysis 模块一致，前端只做渲染） */
export interface AiAnalysisDimensions {
  advantage: string[]
  weakness: string[]
  risk: string[]
}

export interface AiAnalysisSupplier {
  quote_id: number
  name: string
  part_name: string | null
  scheme: string | null
  /** 含税单价升序排名（1 = 最低），取机械对比结果 */
  price_rank: number
  is_lowest: boolean
  final_unit_price_taxed: number | null
  /** 一级维度：≤25 字 */
  advantage: string
  weakness: string
  risk: string
  suggestion: string
  /** 子维度：维度名 -> 单元格文本（≤40 字），维度名固定且顺序固定 */
  advantage_detail: Record<string, string>
  weakness_detail: Record<string, string>
  risk_detail: Record<string, string>
}

export interface AiAnalysisContent {
  unit: string
  category: string | null
  category_name: string | null
  part: Record<string, string | number | null>
  /** ≤100 字专家总评 */
  overall: string
  suppliers: AiAnalysisSupplier[]
  dimensions: AiAnalysisDimensions
}

export interface AiAnalysis {
  id: number
  task_id: number
  /** 输入指纹（结构化输入 + prompt 版本），变了才需要重新分析 */
  signature: string
  status: 'running' | 'completed' | 'failed'
  content: AiAnalysisContent | null
  /** pass = 数字回检通过；mismatch = 部分数字与机械对比不一致 */
  check_status: 'pass' | 'mismatch' | null
  check_detail: {
    rounds: number
    checked: number
    suspicious: { value: number }[]
    structure_errors?: string[]
    missing_quote_ids?: number[]
  } | null
  error: string | null
  model: string | null
  tokens: number | null
  elapsed_ms: number | null
  created_at: string
  updated_at: string
}

export interface AiAnalysisResponse {
  signature: string | null
  analysis: AiAnalysis | null
  /** 已有分析结果的指纹与当前输入不一致（数据已更新，可重新分析） */
  stale: boolean
  /** 当前指纹下无任何分析记录 → 前端进入页面时自动触发一次 */
  auto_run: boolean
  in_progress: boolean
}

// ---------- 报价单数据 ----------

export interface QuoteListItem {
  quote_id: number
  task_id: number
  project_name: string | null
  supplier_name: string
  part_name: string | null
  category_code: string | null
  category_name: string | null
  final_unit_price_taxed: number | null
  tooling_total: number | null
  calc_check: string | null
  flags: string[]
  parse_status: string
  line_count: number
  has_other_info: boolean
  created_at: string
}

export interface QuoteLine {
  id: number
  module: string
  item_name: string | null
  item_type: string | null
  amount: number | null
  unit: string | null
  rate: number | null
  atom_code: string | null
  atom_name: string | null
  is_new_process: boolean
  bundle_flag: boolean
  candidate_atoms: string[]
  fingerprint: string | null
  confidence: number | null
  match_path: string | null
  cross_check: { diff?: number; verdict?: string } | null
  confirm_status: string | null
  note: string | null
  evidence: Record<string, unknown> | null
}

export interface QuoteToolingLine {
  tooling_type: ToolingType
  type_name: string
  item_name: string | null
  amount: number | null
  cavity_count: number | null
  lifespan: number | null
  note: string | null
}

export interface QuoteDetail {
  quote_id: number
  task_id: number
  project_name: string | null
  supplier_name: string
  supplier_code: string | null
  category_code: string | null
  basic: Record<string, unknown>
  parse_status: string
  calc_check: string | null
  flags: string[]
  modules: { module: string; name: string; total: number | null }[]
  summary: {
    untaxed_total: number | null
    tax_amount: number | null
    discount: number | null
    final_unit_price_taxed: number | null
    tooling_total: number | null
  }
  lines: QuoteLine[]
  tooling: QuoteToolingLine[]
  /** 解析时额外识别到的信息（markdown，已剔除手机号/姓名/邮箱/印章等个人信息与银行开户信息） */
  other_info: string | null
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

/** 分组所属维度 = 抽屉编码（自由字符串，含保留值 custom=自定义），由 drawer 表驱动 */
export type DimGroupScope = string

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

/** 对比抽屉（比价页面「加工费专区」的页签；内置 3 个不可改不可删） */
export interface MasterDrawer {
  code: string
  name: string
  is_builtin: boolean
  sort_order: number
  /** 该抽屉下已有的分组数（删除保护用） */
  group_count: number
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
