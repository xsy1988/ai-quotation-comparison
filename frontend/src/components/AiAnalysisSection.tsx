import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Card, Progress, Skeleton, Space, Spin, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { errorDetail, generateAiAnalysis, getAiAnalysis } from '../api/client'
import type { AiAnalysisContent, AiAnalysisResponse, AiAnalysisSupplier } from '../types'
import { useSupplierColumnFit } from './compareUtils'

/** 已经自动触发过的「任务+输入指纹」，避免刷新/重挂载重复触发（指纹变化才会重新触发） */
const AUTO_TRIGGERED = new Set<string>()

const DIMENSION_LABEL = '维度'
const DIMENSION_WIDTH = 132
/** AI 单元格是整句话（优势/劣势/风险），最小宽度比报价对比列更宽 */
const SUPPLIER_COL_MIN_WIDTH = 240

/** 推理中的阶段文案（无真实进度通道，按耗时轮播，属于占位动画的一部分） */
const RUNNING_STEPS = [
  '读取结构化对比数据（材料/加工/检验/包装运输/损管利税）…',
  '拆解各供应商成本结构与工艺覆盖…',
  '交叉校验金额与勾稽差异…',
  '归纳优势、劣势与风险子维度…',
  '编排供应商建议与总评…',
]

interface RowNode {
  key: string
  label: string
  cells: Record<string, React.ReactNode>
  children?: RowNode[]
}

function textCell(value: string | undefined, key?: string): React.ReactNode {
  if (!value || value === '—') return <span key={key} className="na-cell">—</span>
  return (
    <span key={key} style={{ whiteSpace: 'normal' }}>
      {value}
    </span>
  )
}

/** 子维度行：维度名固定且顺序固定（来自后端 dimensions），某家缺失时显示「—」 */
function detailRows(
  parentKey: string,
  names: string[],
  qids: string[],
  cellsByDim: Record<string, Record<string, string>>,
): RowNode[] {
  return names.map((name) => ({
    key: `${parentKey}::${name}`,
    label: name,
    cells: Object.fromEntries(
      qids.map((qid) => [qid, textCell(cellsByDim[name]?.[qid], `${parentKey}::${name}::${qid}`)]),
    ),
  }))
}

/** 维度名 -> quote_id -> 单元格文本 */
function detailMap(
  suppliers: AiAnalysisSupplier[],
  names: string[],
  get: (s: AiAnalysisSupplier) => Record<string, string> | undefined,
): Record<string, Record<string, string>> {
  return Object.fromEntries(
    names.map((name) => [
      name,
      Object.fromEntries(suppliers.map((s) => [String(s.quote_id), get(s)?.[name] ?? '—'])),
    ]),
  )
}

/** 含税单价行：最低/最高标签取自机械对比结果（不信任 LLM 输出） */
function priceCells(
  suppliers: AiAnalysisSupplier[],
  unit: string,
): Record<string, React.ReactNode> {
  const priced = suppliers.filter((s) => typeof s.final_unit_price_taxed === 'number')
  const max = priced.length > 0 ? Math.max(...priced.map((s) => s.final_unit_price_taxed!)) : null
  return Object.fromEntries(
    suppliers.map((s) => {
      const value = s.final_unit_price_taxed
      const qid = String(s.quote_id)
      if (typeof value !== 'number') {
        return [qid, <span key={qid} className="na-cell">—</span>]
      }
      const isLowest = s.is_lowest || s.price_rank === 1
      const isHighest = max !== null && priced.length > 1 && Math.abs(value - max) < 1e-9
      return [
        qid,
        <span key={qid} className="amount-cell" style={{ display: 'block' }}>
          {value.toFixed(2)}
          <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>
            {unit}
          </Typography.Text>
          {isLowest && (
            <Tag color="green" style={{ marginLeft: 6 }}>
              最低
            </Tag>
          )}
          {isHighest && !isLowest && (
            <Tag color="orange" style={{ marginLeft: 6 }}>
              最高
            </Tag>
          )}
        </span>,
      ]
    }),
  )
}

function buildRows(content: AiAnalysisContent, suppliers: AiAnalysisSupplier[]): RowNode[] {
  const qids = suppliers.map((s) => String(s.quote_id))
  const headline = (get: (s: AiAnalysisSupplier) => string) =>
    Object.fromEntries(qids.map((q, i) => [q, textCell(get(suppliers[i]))]))

  const { advantage, weakness, risk } = content.dimensions
  const advDetail = detailMap(suppliers, advantage, (s) => s.advantage_detail)
  const weakDetail = detailMap(suppliers, weakness, (s) => s.weakness_detail)
  const riskDetail = detailMap(suppliers, risk, (s) => s.risk_detail)

  return [
    {
      key: 'price',
      label: '含税单价',
      cells: priceCells(suppliers, content.unit),
    },
    {
      key: 'advantage',
      label: '优势',
      cells: headline((s) => s.advantage),
      children: detailRows('advantage', advantage, qids, advDetail),
    },
    {
      key: 'weakness',
      label: '劣势',
      cells: headline((s) => s.weakness),
      children: detailRows('weakness', weakness, qids, weakDetail),
    },
    {
      key: 'risk',
      label: '风险',
      cells: headline((s) => s.risk),
      children: detailRows('risk', risk, qids, riskDetail),
    },
    {
      key: 'suggestion',
      label: '建议',
      cells: headline((s) => s.suggestion),
    },
  ]
}

/** 供应商列顺序：一律以「报价对比」表的供应商列顺序为准（历史缓存的分析可能仍是旧的按价格排序） */
function orderSuppliers(
  suppliers: AiAnalysisSupplier[],
  order?: number[],
): AiAnalysisSupplier[] {
  if (!order?.length) return suppliers
  const rank = new Map(order.map((id, i) => [id, i]))
  return [...suppliers].sort(
    (a, b) =>
      (rank.get(a.quote_id) ?? Number.MAX_SAFE_INTEGER) -
      (rank.get(b.quote_id) ?? Number.MAX_SAFE_INTEGER),
  )
}

/** 推理中的占位模块：动画 + 阶段文案 + 已耗时，并预留表格空间避免布局跳动 */
function AnalysisPlaceholder({ elapsedMs }: { elapsedMs: number }) {
  const [step, setStep] = useState(0)
  useEffect(() => {
    const timer = window.setInterval(
      () => setStep((s) => (s + 1) % RUNNING_STEPS.length),
      2500,
    )
    return () => window.clearInterval(timer)
  }, [])

  const seconds = Math.round(elapsedMs / 1000)
  const percent = Math.min(92, 6 + seconds * 1.8)

  return (
    <div style={{ padding: '4px 0' }}>
      <Space align="center" size={12} style={{ marginBottom: 12 }}>
        <Spin />
        <Typography.Text strong>AI 正在分析本次报价数据…</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          已耗时 {seconds}s
        </Typography.Text>
      </Space>
      <Progress percent={percent} status="active" showInfo={false} />
      <Typography.Text type="secondary" style={{ fontSize: 13, display: 'block' }}>
        {RUNNING_STEPS[step]}
      </Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
        LLM 仅读取已解析的结构化对比数据，不接触原始报价文件。分析完成后本模块自动刷新。
      </Typography.Text>
      <div style={{ marginTop: 16 }}>
        <Skeleton active paragraph={{ rows: 5 }} title={false} />
      </div>
    </div>
  )
}

export default function AiAnalysisSection({
  taskId,
  supplierOrder,
}: {
  taskId: number
  /** 「报价对比」表的供应商列顺序（quote_id 数组）；用于让两张表的供应商列前后一致 */
  supplierOrder?: number[]
}) {
  const queryClient = useQueryClient()
  const queryKey = useMemo(() => ['ai-analysis', taskId], [taskId])
  const [elapsedMs, setElapsedMs] = useState(0)

  const { data, isLoading, isError, error } = useQuery({
    queryKey,
    queryFn: () => getAiAnalysis(taskId),
    enabled: Number.isFinite(taskId),
    retry: false,
    // 后台已有推理中的记录（如另一个页面触发）→ 轮询等待结果
    refetchInterval: (query) => {
      const payload = query.state.data as AiAnalysisResponse | undefined
      return payload?.analysis?.status === 'running' ? 3000 : false
    },
  })

  const mutation = useMutation({
    mutationFn: () => generateAiAnalysis(taskId),
    onSuccess: (analysis) => {
      queryClient.setQueryData<AiAnalysisResponse>(queryKey, {
        signature: analysis.signature,
        analysis,
        stale: false,
        auto_run: false,
        in_progress: analysis.status === 'running',
      })
    },
  })

  const analysis = data?.analysis ?? null
  const running = mutation.isPending || analysis?.status === 'running'

  // 自动触发：仅当该输入指纹下没有任何分析记录（首次进入 / 新增供应商 / 数据变化后首次进入）。
  // 刷新页面时指纹已有记录 → auto_run=false，不会重复触发。
  const autoRan = useRef(false)
  useEffect(() => {
    if (!data || !data.auto_run || !data.signature || mutation.isPending) return
    const key = `${taskId}:${data.signature}`
    if (autoRan.current || AUTO_TRIGGERED.has(key)) return
    autoRan.current = true
    AUTO_TRIGGERED.add(key)
    mutation.mutate()
  }, [data, mutation, taskId])

  // 推理计时（占位动画的耗时展示）
  useEffect(() => {
    if (!running) {
      setElapsedMs(0)
      return
    }
    const start = Date.now()
    const timer = window.setInterval(() => setElapsedMs(Date.now() - start), 1000)
    return () => window.clearInterval(timer)
  }, [running])

  const content = analysis?.status === 'completed' ? analysis.content : null
  const suppliers = useMemo(
    () => orderSuppliers(content?.suppliers ?? [], supplierOrder),
    [content, supplierOrder],
  )
  // 列宽自适应：放得下就均分撑满一屏，放不下（每列已到最小宽度）才左右拖动
  const fit = useSupplierColumnFit(suppliers.length, DIMENSION_WIDTH, SUPPLIER_COL_MIN_WIDTH)

  const columns: TableProps<RowNode>['columns'] = content
    ? [
        {
          title: DIMENSION_LABEL,
          dataIndex: 'label',
          key: 'label',
          width: DIMENSION_WIDTH,
          fixed: 'left',
        },
        ...suppliers.map((s, index) => ({
          title: (
            <span>
              {s.name}
              {(s.part_name || s.scheme) && (
                <div style={{ fontSize: 12, fontWeight: 400, color: '#888' }}>
                  {[s.part_name, s.scheme].filter(Boolean).join(' · ')}
                </div>
              )}
            </span>
          ),
          key: `q${s.quote_id}`,
          width: fit.widthOf(index),
          render: (_: unknown, row: RowNode) => row.cells[String(s.quote_id)] ?? '—',
        })),
      ]
    : []

  const rows = useMemo(() => (content ? buildRows(content, suppliers) : []), [content, suppliers])

  // 与「报价对比」一致：顶层行 L0 底色、子维度行 L1 底色，同层内深浅交替
  const rowClassName = (row: RowNode): string => {
    const nodes = rows
    const isChild = nodes.some((r) => r.children?.some((c) => c.key === row.key))
    const siblings = isChild
      ? (nodes.find((r) => r.children?.some((c) => c.key === row.key))?.children ?? [])
      : nodes
    const index = siblings.findIndex((n) => n.key === row.key)
    const classes = [isChild ? 'hier-l1' : 'hier-l0']
    if (index % 2 === 1) classes.push('hier-alt')
    return classes.join(' ')
  }

  const checkTag = () => {
    if (analysis?.check_status === 'pass') return <Tag color="green">数字回检通过</Tag>
    if (analysis?.check_status === 'mismatch') {
      return <Tag color="orange">部分数字待核对</Tag>
    }
    return null
  }

  return (
    <Card
      size="small"
      title={
        <Space size={8} wrap>
          <span>AI 分析</span>
          {content && <Tag color="purple">AI 生成</Tag>}
          {content && checkTag()}
          {data?.stale && <Tag color="gold">数据已更新，建议重新分析</Tag>}
          {content?.overall && (
            <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              {content.overall}
            </Typography.Text>
          )}
        </Space>
      }
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={running}
          onClick={() => mutation.mutate()}
        >
          {content ? '重新分析' : '生成 AI 分析'}
        </Button>
      }
    >
      {mutation.isError && (
        <Alert
          type="error"
          style={{ marginBottom: 12 }}
          message="AI 分析失败（LLM 服务不可用或输出未通过结构校验）"
          description={errorDetail(mutation.error)}
          showIcon
        />
      )}

      {isLoading && !content && <AnalysisPlaceholder elapsedMs={0} />}

      {isError && !content && !running && (
        <Alert
          type="error"
          message="加载 AI 分析失败"
          description={error instanceof Error ? error.message : String(error)}
          showIcon
        />
      )}

      {running && <AnalysisPlaceholder elapsedMs={elapsedMs} />}

      {!running && analysis?.status === 'failed' && !mutation.isError && (
        <Alert
          type="error"
          message="上次 AI 分析未成功完成"
          description={analysis.error ?? '未知错误'}
          showIcon
        />
      )}

      {!running && !content && !isLoading && !isError && analysis?.status !== 'failed' && (
        <Typography.Text type="secondary">
          尚未生成 AI 分析。进入本页会自动触发一次（数据变化后重新进入会再次触发），也可点击右上角「生成
          AI 分析」手动触发。
        </Typography.Text>
      )}

      {!running && content && (
        <div ref={fit.ref}>
          <Table<RowNode>
            size="small"
            rowKey="key"
            bordered
            pagination={false}
            columns={columns}
            dataSource={rows}
            rowClassName={rowClassName}
            // 供应商较多时允许左右拖动；「维度」列固定
            scroll={{ x: fit.scrollX }}
            expandable={{
              defaultExpandedRowKeys: [],
              indentSize: 16,
            }}
            locale={{ emptyText: '—' }}
          />
        </div>
      )}
    </Card>
  )
}
