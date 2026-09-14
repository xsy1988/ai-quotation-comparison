import axios from 'axios'
import type { AxiosError } from 'axios'
import type {
  AiAnalysis,
  AiAnalysisResponse,
  AtomOption,
  CategoryOption,
  Comparison,
  CreateTaskResponse,
  DimGroupScope,
  DomainOption,
  MasterAlias,
  MasterAtom,
  MasterAtomCreatePayload,
  MasterCategory,
  MasterDimGroup,
  MasterDrawer,
  MasterSupplier,
  NewAtomSuggestion,
  ProgressPayload,
  QuoteDetail,
  QuoteListItem,
  QuoteLinePatch,
  QuoteLinePatchResult,
  QuotePatch,
  QuotePatchResult,
  SuggestionResolveResult,
  TaskListItem,
} from '../types'

const baseURL = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8002'

export const http = axios.create({ baseURL, timeout: 30000 })

export async function createTask(files: File[]): Promise<CreateTaskResponse> {
  const form = new FormData()
  for (const file of files) {
    form.append('files', file)
  }
  const { data } = await http.post<CreateTaskResponse>('/api/tasks', form)
  return data
}

export async function getComparison(taskId: number): Promise<Comparison> {
  const { data } = await http.get<Comparison>(`/api/tasks/${taskId}/comparison`)
  return data
}

/**
 * 查询当前输入指纹下的 AI 分析结果。
 * auto_run=true 表示该数据版本尚无分析记录（前端进入页面时自动触发一次，刷新不重复触发）。
 */
export async function getAiAnalysis(taskId: number): Promise<AiAnalysisResponse> {
  const { data } = await http.get<AiAnalysisResponse>(`/api/tasks/${taskId}/ai-analysis`)
  return data
}

export async function generateAiAnalysis(taskId: number): Promise<AiAnalysis> {
  // LLM 生成含数字回检与一轮重生成，真实耗时 20~60s，放宽该请求超时
  const { data } = await http.post<AiAnalysis>(`/api/tasks/${taskId}/ai-analysis`, null, {
    timeout: 180000,
  })
  return data
}

export async function getTasks(): Promise<TaskListItem[]> {
  const { data } = await http.get<TaskListItem[]>('/api/tasks')
  return data
}

// ---------- 报价单数据 ----------

export async function listQuotes(params: {
  q?: string
  parse_status?: string
  task_id?: number
} = {}): Promise<QuoteListItem[]> {
  const { data } = await http.get<{ items: QuoteListItem[] }>('/api/quotes', { params })
  return data.items
}

export async function getQuote(quoteId: number): Promise<QuoteDetail> {
  const { data } = await http.get<QuoteDetail>(`/api/quotes/${quoteId}`)
  return data
}

export async function getSnapshot(
  taskId: number,
  quoteId: number,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<any> {
  const { data } = await http.get(`/api/tasks/${taskId}/quotes/${quoteId}/snapshot`)
  return data
}

// ---------- 就地编辑（第 7 步） ----------

/** 后端错误 detail（400/404/502 的 {"detail": "..."}），取不到则回退 error.message */
export function errorDetail(e: unknown): string {
  const resp = (e as AxiosError<{ detail?: string }>).response
  return resp?.data?.detail ?? (e instanceof Error ? e.message : String(e))
}

export async function patchQuote(quoteId: number, patch: QuotePatch): Promise<QuotePatchResult> {
  const { data } = await http.patch<QuotePatchResult>(`/api/quotes/${quoteId}`, patch)
  return data
}

export async function patchQuoteLine(
  lineId: number,
  patch: QuoteLinePatch,
): Promise<QuoteLinePatchResult> {
  const { data } = await http.patch<QuoteLinePatchResult>(`/api/quote_lines/${lineId}`, patch)
  return data
}

export async function getAtoms(q: string): Promise<AtomOption[]> {
  const { data } = await http.get<AtomOption[]>('/api/atoms', { params: { q } })
  return data
}

export async function getCategories(): Promise<CategoryOption[]> {
  const { data } = await http.get<CategoryOption[]>('/api/categories')
  return data
}

// ---------- 新工艺决策（第 8 步） ----------

export async function getSuggestions(taskId: number): Promise<NewAtomSuggestion[]> {
  const { data } = await http.get<{ suggestions: NewAtomSuggestion[] }>(
    `/api/tasks/${taskId}/suggestions`,
  )
  return data.suggestions
}

export interface ResolveSuggestionPayload {
  action: 'create' | 'merge' | 'ignore'
  name?: string
  domain_code?: string
  stage_name?: string
  class_name?: string
  category_code?: string
  atom_code?: string
}

export async function resolveSuggestion(
  suggestionId: number,
  payload: ResolveSuggestionPayload,
): Promise<SuggestionResolveResult> {
  const { data } = await http.post<SuggestionResolveResult>(
    `/api/suggestions/${suggestionId}/resolve`,
    payload,
  )
  return data
}

export async function getDomains(): Promise<DomainOption[]> {
  const { data } = await http.get<DomainOption[]>('/api/domains')
  return data
}

export async function getStages(): Promise<string[]> {
  const { data } = await http.get<string[]>('/api/stages')
  return data
}

export async function getClasses(): Promise<string[]> {
  const { data } = await http.get<string[]>('/api/classes')
  return data
}

// ---------- 主数据管理（第 9 步） ----------

export async function listMasterAtoms(q: string): Promise<MasterAtom[]> {
  const { data } = await http.get<{ atoms: MasterAtom[] }>('/api/master/atoms', { params: { q } })
  return data.atoms
}

export async function createMasterAtom(payload: MasterAtomCreatePayload): Promise<MasterAtom> {
  const { data } = await http.post<MasterAtom>('/api/master/atoms', payload)
  return data
}

export async function patchMasterAtom(
  code: string,
  patch: { name?: string; stage_name?: string; class_name?: string; remark?: string },
): Promise<MasterAtom> {
  const { data } = await http.patch<MasterAtom>(`/api/master/atoms/${encodeURIComponent(code)}`, patch)
  return data
}

export async function deleteMasterAtom(code: string): Promise<void> {
  await http.delete(`/api/master/atoms/${encodeURIComponent(code)}`)
}

export async function listMasterAliases(q: string, sort?: string): Promise<MasterAlias[]> {
  const { data } = await http.get<{ aliases: MasterAlias[] }>('/api/master/aliases', {
    params: { q, sort },
  })
  return data.aliases
}

export async function createMasterAlias(payload: {
  atom_code: string
  alias_text: string
}): Promise<MasterAlias> {
  const { data } = await http.post<MasterAlias>('/api/master/aliases', payload)
  return data
}

export async function deleteMasterAlias(id: number): Promise<void> {
  await http.delete(`/api/master/aliases/${id}`)
}

export async function listMasterCategories(): Promise<MasterCategory[]> {
  const { data } = await http.get<{ categories: MasterCategory[] }>('/api/master/categories')
  return data.categories
}

export async function createMasterCategory(payload: { code: string; name: string }): Promise<MasterCategory> {
  const { data } = await http.post<MasterCategory>('/api/master/categories', payload)
  return data
}

export async function patchMasterCategory(code: string, patch: { name: string }): Promise<MasterCategory> {
  const { data } = await http.patch<MasterCategory>(
    `/api/master/categories/${encodeURIComponent(code)}`,
    patch,
  )
  return data
}

export async function deleteMasterCategory(code: string): Promise<void> {
  await http.delete(`/api/master/categories/${encodeURIComponent(code)}`)
}

export async function listMasterDimGroups(): Promise<MasterDimGroup[]> {
  const { data } = await http.get<{ dim_groups: MasterDimGroup[] }>('/api/master/dim_groups')
  return data.dim_groups
}

export async function createMasterDimGroup(payload: {
  group_code: string
  group_name: string
  scope: DimGroupScope
  parent_code?: string | null
  member_atoms?: string[]
}): Promise<MasterDimGroup> {
  const { data } = await http.post<MasterDimGroup>('/api/master/dim_groups', payload)
  return data
}

export async function patchMasterDimGroup(
  groupCode: string,
  patch: {
    group_name?: string
    scope?: DimGroupScope
    parent_code?: string | null
    member_atoms?: string[]
  },
): Promise<MasterDimGroup> {
  const { data } = await http.patch<MasterDimGroup>(
    `/api/master/dim_groups/${encodeURIComponent(groupCode)}`,
    patch,
  )
  return data
}

export async function deleteMasterDimGroup(groupCode: string): Promise<void> {
  await http.delete(`/api/master/dim_groups/${encodeURIComponent(groupCode)}`)
}

// ---------- 对比抽屉（基础数据维护 / 抽屉管理） ----------

export async function listMasterDrawers(): Promise<MasterDrawer[]> {
  const { data } = await http.get<{ drawers: MasterDrawer[] }>('/api/master/drawers')
  return data.drawers
}

export async function createMasterDrawer(payload: {
  code: string
  name: string
  sort_order?: number
}): Promise<MasterDrawer> {
  const { data } = await http.post<MasterDrawer>('/api/master/drawers', payload)
  return data
}

export async function patchMasterDrawer(
  code: string,
  patch: { name?: string; sort_order?: number },
): Promise<MasterDrawer> {
  const { data } = await http.patch<MasterDrawer>(
    `/api/master/drawers/${encodeURIComponent(code)}`,
    patch,
  )
  return data
}

export async function deleteMasterDrawer(code: string): Promise<void> {
  await http.delete(`/api/master/drawers/${encodeURIComponent(code)}`)
}

export async function listMasterSuppliers(q: string): Promise<MasterSupplier[]> {
  const { data } = await http.get<{ suppliers: MasterSupplier[] }>('/api/master/suppliers', {
    params: { q },
  })
  return data.suppliers
}

export async function createMasterSupplier(payload: {
  code: string
  name: string
  alias?: string
}): Promise<MasterSupplier> {
  const { data } = await http.post<MasterSupplier>('/api/master/suppliers', payload)
  return data
}

export async function patchMasterSupplier(
  code: string,
  patch: { name?: string; alias?: string },
): Promise<MasterSupplier> {
  const { data } = await http.patch<MasterSupplier>(
    `/api/master/suppliers/${encodeURIComponent(code)}`,
    patch,
  )
  return data
}

export async function deleteMasterSupplier(code: string): Promise<void> {
  await http.delete(`/api/master/suppliers/${encodeURIComponent(code)}`)
}

/**
 * 订阅 SSE 进度流，返回取消函数。
 * onEvent 收到 progress / done / error（后端显式推送的 SSE 事件）。
 * onConnectionError 在网络层断开时触发（EventSource 的 error 事件，区别于 SSE error 命名事件），
 * 供调用方决定重连策略。
 */
export function subscribeProgress(
  taskId: number,
  onEvent: (event: string, payload: ProgressPayload | { detail: string }) => void,
  onConnectionError?: () => void,
): () => void {
  const url = `${baseURL}/api/tasks/${taskId}/progress`
  const source = new EventSource(url)

  const named = (event: string) => (e: Event) => {
    if (!(e instanceof MessageEvent)) return
    try {
      onEvent(event, JSON.parse(e.data as string))
    } catch {
      onEvent('error', { detail: '进度数据解析失败' })
    }
  }
  source.addEventListener('progress', named('progress'))
  source.addEventListener('done', named('done'))
  source.addEventListener('error', named('error'))
  source.onerror = (e) => {
    // SSE error 命名事件也会走 onerror，但带 data；纯网络错误是裸 Event
    if (!(e instanceof MessageEvent) && onConnectionError) onConnectionError()
  }
  return () => source.close()
}
