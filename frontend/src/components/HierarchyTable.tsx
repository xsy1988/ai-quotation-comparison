import { useEffect, useMemo, useState } from 'react'
import { Button, Dropdown, Table, Tag, Tooltip } from 'antd'
import { DownOutlined } from '@ant-design/icons'
import type { TableProps } from 'antd'
import type { Comparison, PriceTreeNode, ProcessingItem, Supplier } from '../types'
import Amount, { NA_TEXT } from './Amount'
import ProcessingItemCell from './ProcessingItemCell'

const NA_DETAIL_TEXT = '路线未含此项'

/** 加工费明细行 kind=amount 且 meta 带 quote_line id → 就地编辑（分组行除外） */
function isProcessingDetail(node: PriceTreeNode): boolean {
  return node.key.startsWith('processing::') && !node.children?.length
}

/** 其他模块明细行（键形如 "<module>::<名称>"） */
function isDetailRow(node: PriceTreeNode): boolean {
  return !node.children?.length && node.key.includes('::')
}

function formatRate(rate: number): string {
  const pct = rate * 100
  return `${Number.isInteger(pct) ? pct : pct.toFixed(2)}%`
}

type ProcScope = 'domain' | 'stage' | 'class'

const PROC_SCOPE_OPTIONS = [
  { label: '工艺域', value: 'domain' },
  { label: '工艺阶段', value: 'stage' },
  { label: '工艺类别', value: 'class' },
]

const UNMATCHED_KEY = '__unmatched__'

function hasAnyValue(node: PriceTreeNode): boolean {
  // 金额为 0 视同未报：所有供应商都是 0 或 null → 不显示
  return Object.values(node.values).some(
    (v) => typeof v === 'number' && Math.round(v * 1e6) !== 0,
  )
}

/** 按所选口径（工艺域/阶段/类别）给加工费明细分组；
 * 全供应商均为 0 或未报的条目/分组不显示。 */
function groupProcessing(children: PriceTreeNode[], scope: ProcScope): PriceTreeNode[] {
  const groups = new Map<string, PriceTreeNode>()
  for (const child of children) {
    if (!hasAnyValue(child)) continue // 所有供应商都是 0/未报 → 不进对比项
    const g = child.scope_meta?.[scope]
    const gKey = g?.code ?? UNMATCHED_KEY
    let group = groups.get(gKey)
    if (!group) {
      group = {
        key: `processing::${scope}:${gKey}`,
        label: g?.name ?? '未匹配',
        kind: 'group',
        values: {},
        children: [],
      }
      groups.set(gKey, group)
    }
    group.children!.push(child)
  }
  const result: PriceTreeNode[] = []
  for (const group of groups.values()) {
    const values: Record<string, number | null> = {}
    for (const child of group.children!) {
      for (const [qid, v] of Object.entries(child.values)) {
        if (typeof v === 'number') {
          const cur = values[qid]
          values[qid] = Math.round(((cur ?? 0) + v) * 1e6) / 1e6
        }
      }
    }
    group.values = values
    if (hasAnyValue(group)) result.push(group) // 分组合计全为 0/未报 → 整组不显示
  }
  return result
}

interface Props {
  comparison: Comparison
  highlighted: Set<number>
}

/** 层级金额对比树：dataSource 直接给 price_tree，antd Table children 嵌套展开 */
export default function HierarchyTable({ comparison, highlighted }: Props) {
  const { suppliers, price_tree } = comparison
  const [procScope, setProcScope] = useState<ProcScope>('domain')

  const tree = useMemo(() => {
    // 加工费节点嵌套在 unit_price 下，需递归变换
    const mapNode = (node: PriceTreeNode): PriceTreeNode =>
      node.key === 'processing' && node.children
        ? { ...node, children: groupProcessing(node.children, procScope) }
        : node.children
          ? { ...node, children: node.children.map(mapNode) }
          : node
    return price_tree.map(mapNode)
  }, [price_tree, procScope])

  // 受控展开：数据/分组口径变化时重新全部展开（defaultExpandAllRows 对异步数据不可靠）
  const allKeys = useMemo(() => {
    const keys: string[] = []
    const walk = (nodes: PriceTreeNode[]) => {
      for (const n of nodes) {
        if (n.children?.length) {
          keys.push(n.key)
          walk(n.children)
        }
      }
    }
    walk(tree)
    return keys
  }, [tree])
  const [expandedKeys, setExpandedKeys] = useState<string[]>(allKeys)
  useEffect(() => setExpandedKeys(allKeys), [allKeys])

  const columns: TableProps<PriceTreeNode>['columns'] = [    {
      title: '项目',
      key: 'label',
      fixed: 'left',
      width: 280,
      render: (_: unknown, node) => {
        // 生产加工费分组行：行内靠右放下拉按钮切换 工艺域/阶段/类别
        if (node.key === 'processing') {
          return (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontWeight: 600 }}>{node.label}</span>
              <Dropdown
                trigger={['click']}
                menu={{
                  items: PROC_SCOPE_OPTIONS.map((o) => ({ key: o.value, label: o.label })),
                  selectedKeys: [procScope],
                  onClick: ({ key }) => setProcScope(key as ProcScope),
                }}
              >
                <Button size="small">
                  {PROC_SCOPE_OPTIONS.find((o) => o.value === procScope)?.label}
                  <DownOutlined />
                </Button>
              </Dropdown>
            </div>
          )
        }
        const strong = node.kind === 'group' || node.key === 'final'
        // 明细行备注/原文名称：tooltip 展示
        const metas = Object.values(node.meta ?? {})
        const tips = metas
          .map((m) => m.note ?? m.name)
          .filter((t): t is string => Boolean(t))
        const label = (
          <span
            style={
              node.key === 'final'
                ? { fontWeight: 700, background: '#fff7e6', padding: '2px 6px', borderRadius: 4 }
                : strong
                  ? { fontWeight: 600 }
                  : undefined
            }
          >
            {node.label}
          </span>
        )
        return tips.length > 0 ? <Tooltip title={tips.join('；')}>{label}</Tooltip> : label
      },
    },
    ...suppliers.map((s: Supplier) => ({
      title: (
        <span>
          {highlighted.has(s.quote_id) && (
            <Tag color="orange" style={{ marginRight: 4 }}>
              警示
            </Tag>
          )}
          {s.supplier_name}
        </span>
      ),
      key: `q${s.quote_id}`,
      align: 'right' as const,
      render: (_: unknown, node: PriceTreeNode) => {
        const raw = node.values[String(s.quote_id)]
        const meta = node.meta?.[String(s.quote_id)]

        if (node.kind === 'text') {
          return raw === null || raw === undefined ? '—' : String(raw)
        }

        const value = typeof raw === 'number' ? raw : null

        // 加工费明细行：金额 + 原子映射就地编辑（PATCH /api/quote_lines/{id}）
        if (isProcessingDetail(node)) {
          if (!meta?.id) {
            return <span className="na-cell">{NA_DETAIL_TEXT}</span>
          }
          const item: ProcessingItem = {
            id: meta.id,
            name: meta.name ?? node.label,
            amount: value,
            atom_code: meta.atom_code ?? null,
            confidence: meta.confidence ?? null,
            fingerprint: meta.fingerprint ?? null,
            bundle_flag: meta.bundle_flag ?? false,
            is_new_process: meta.is_new_process ?? false,
            note: meta.note ?? null,
          }
          return <ProcessingItemCell item={item} />
        }

        if (value === null) {
          return isDetailRow(node) ? (
            <span className="na-cell">{NA_DETAIL_TEXT}</span>
          ) : (
            <Amount value={null} />
          )
        }

        const rateSuffix = meta?.rate != null && <span>（{formatRate(meta.rate)}）</span>
        // 材料费明细行：各家材料名称不同，名称+金额同格展示
        const nameLine =
          node.key.startsWith('materials::') && meta?.name ? (
            <div style={{ fontSize: 12, color: '#888' }}>{meta.name}</div>
          ) : null
        const typeTag =
          isDetailRow(node) && meta?.item_type && meta.item_type !== node.label ? (
            <Tag style={{ marginLeft: 4 }}>{meta.item_type}</Tag>
          ) : null
        return (
          <>
            {nameLine}
            <Amount value={value} strong={node.key === 'final'} />
            {rateSuffix}
            {typeTag}
          </>
        )
      },
    })),
  ]

  return (
    <Table<PriceTreeNode>
      size="small"
      rowKey="key"
      columns={columns}
      dataSource={tree}
      pagination={false}
      bordered
      expandable={{
        expandedRowKeys: expandedKeys,
        onExpandedRowsChange: (keys) => setExpandedKeys(keys as string[]),
      }}
      locale={{ emptyText: NA_TEXT }}
    />
  )
}
