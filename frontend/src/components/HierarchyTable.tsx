import { useEffect, useMemo, useState } from 'react'
import { Button, Dropdown, Table, Tag, Tooltip, Typography } from 'antd'
import { DownOutlined } from '@ant-design/icons'
import type { TableProps } from 'antd'
import type { Comparison, PriceTreeNode, ProcessingItem, Supplier } from '../types'
import Amount, { NA_TEXT } from './Amount'
import ProcessingItemCell from './ProcessingItemCell'
import {
  SUPPLIER_COL_MIN_WIDTH,
  supplierColumnGroups,
  supplierSeparatorStyle,
} from './compareUtils'

const NA_DETAIL_TEXT = '/'

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

/** 分组排序的制造工艺理序（通用流程直觉）：基板/SMT → 成型 → 去料/机加 → 热处理 →
 *  焊接/贴合 → 表面整平 → 清洁 → 转化膜/电镀/涂装 → 印刷 → 固化 → 组装 → 检测 → 返修/其他。
 *  未匹配恒排最后；表中未收录的工艺保持出现顺序跟在后面。 */
const PROC_GROUP_ORDER: Record<ProcScope, string[]> = {
  domain: [
    'JB', 'XY', 'YJ', 'BC', 'CX', 'LH', 'QL', 'QX', 'TJ', 'FQ', 'MQ', 'DJ', 'RC',
    'HJ', 'FB', 'TH', 'ZJ', 'ZP', 'WL', 'QJ', 'ZH', 'DD', 'ZK', 'TZ', 'TF', 'YS',
    'BH', 'YZ', 'GH', 'ZZ', 'ZD', 'JC', 'FX', 'QT',
  ],
  stage: [
    '毛坯', 'SMT制程', 'THT制程', '机加', '后加工', '表面前处理', '成膜', '装饰',
    '后处理', '组装', '测试', '其他',
  ],
  class: ['成型加工', '主制程', '后工序'],
}

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
  const codeOf = new Map<string, string>()
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
      codeOf.set(group.key, gKey)
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
  // 按制造工艺理序排：同序保持出现顺序（sort 稳定），未匹配恒最后
  const order = PROC_GROUP_ORDER[scope]
  const rank = (key: string): number => {
    const code = codeOf.get(key)
    if (!code || code === UNMATCHED_KEY) return Number.MAX_SAFE_INTEGER
    const i = order.indexOf(code)
    return i === -1 ? order.length : i
  }
  result.sort((a, b) => rank(a.key) - rank(b.key))
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

  // 供应商列分组（同一供应商的报价列由后端排在相邻位置）：组间画分隔线 + 列最小宽度
  const groups = useMemo(() => supplierColumnGroups(suppliers), [suppliers])

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

  // 受控展开：仅 产品单价 顶层模块默认展开；基本信息、产品单价下的各费用模块
  // （材料/生产加工/检验/包装运输/损管利/税费等）与 模/治具费用 及其所有子项均默认折叠。
  // 只在数据变化时重置为默认；切换加工费分组口径不重置（见 switchProcScope）。
  const [expandedKeys, setExpandedKeys] = useState<string[]>(['unit_price'])
  useEffect(() => setExpandedKeys(['unit_price']), [price_tree])

  // 切换 工艺域/阶段/类别：展开到生产加工费的子一级分组这一层（分组可见但保持折叠，
  // 其明细子级不展开），剔除旧口径的分组键，其余模块的展开状态保持不变
  const switchProcScope = (scope: ProcScope) => {
    setProcScope(scope)
    setExpandedKeys((prev) => [
      ...prev.filter((k) => k !== 'processing' && !k.startsWith('processing::')),
      'processing',
    ])
  }

  // 每个节点在树中的层级深度（0=顶层模块），用于父子层级底色区分
  const depthByKey = useMemo(() => {
    const map = new Map<string, number>()
    const walk = (nodes: PriceTreeNode[], depth: number) => {
      for (const n of nodes) {
        map.set(n.key, depth)
        if (n.children?.length) walk(n.children, depth + 1)
      }
    }
    walk(tree, 0)
    return map
  }, [tree])

  // 同一层级内相邻行：同色系深浅交替（斑马纹），避免相邻行底色糊在一起
  const altKeys = useMemo(() => {
    const keys = new Set<string>()
    const walk = (nodes: PriceTreeNode[]) => {
      nodes.forEach((n, i) => {
        if (i % 2 === 1) keys.add(n.key)
        if (n.children?.length) walk(n.children)
      })
    }
    walk(tree)
    return keys
  }, [tree])

  // 计算总价（含税）最低价的供应商列（null 值不参与比价）
  const lowestFinalQids = useMemo(() => {
    const finalNode = tree
      .find((n) => n.key === 'unit_price')
      ?.children?.find((n) => n.key === 'final')
    const entries = Object.entries(finalNode?.values ?? {}).filter(
      (e): e is [string, number] => typeof e[1] === 'number',
    )
    if (entries.length === 0) return new Set<string>()
    const min = Math.min(...entries.map(([, v]) => v))
    return new Set(entries.filter(([, v]) => Math.abs(v - min) < 1e-9).map(([q]) => q))
  }, [tree])

  const SUMMARY_KEYS = new Set(['final', 'untaxed', 'discount'])

  const rowClassName = (node: PriceTreeNode) => {
    const classes = [`hier-l${Math.min(depthByKey.get(node.key) ?? 0, 3)}`]
    // 总价/折扣行：中性浅灰底，脱离层级色系与斑马纹，降低视觉引导
    if (SUMMARY_KEYS.has(node.key)) {
      classes.push('summary-row')
    } else if (altKeys.has(node.key)) {
      classes.push('hier-alt')
    }
    return classes.join(' ')
  }

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
                  onClick: ({ key }) => switchProcScope(key as ProcScope),
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
        const strong = node.kind === 'group'
        // 明细行备注/原文名称：tooltip 展示
        const metas = Object.values(node.meta ?? {})
        const tips = metas
          .map((m) => m.note ?? m.name)
          .filter((t): t is string => Boolean(t))
        const label = (
          <span
            style={
              node.key === 'unit_price'
                ? { fontWeight: 700, color: '#d4380d' }
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
          {(s.part_name || s.scheme) && (
            <div style={{ fontSize: 12, fontWeight: 400, color: '#888' }}>
              {[s.part_name, s.scheme].filter(Boolean).join(' · ')}
            </div>
          )}
        </span>
      ),
      key: `q${s.quote_id}`,
      align: 'right' as const,
      // 列最小宽度：宽度不够时表体左右拖动；tableLayout=auto 由 rc-table 落到 <col min-width>
      minWidth: SUPPLIER_COL_MIN_WIDTH,
      onHeaderCell: () => ({ style: supplierSeparatorStyle(groups.get(s.quote_id)) }),
      // 产品单价行最低价单元格：浅绿底标识（内联样式，压过层级底色）；组首列加分隔线
      onCell: (node: PriceTreeNode) => ({
        style: {
          ...supplierSeparatorStyle(groups.get(s.quote_id)),
          ...(node.key === 'unit_price' && lowestFinalQids.has(String(s.quote_id))
            ? { background: '#f0fff4' }
            : {}),
        },
      }),
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
            is_shared: meta.is_shared ?? false,
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
        // 包装运输/损管利等明细行的类型标签（包装/运输、损耗/管理费等）；包装运输行标签与行名重复，不再显示
        const typeTag =
          isDetailRow(node) &&
          !node.key.startsWith('packaging_transport::') &&
          meta?.item_type &&
          meta.item_type !== node.label ? (
            <Tag style={{ marginLeft: 4 }}>{meta.item_type}</Tag>
          ) : null
        const isEmphasisRow = node.key === 'unit_price'
        const isLowest = isEmphasisRow && lowestFinalQids.has(String(s.quote_id))
        return (
          <>
            {nameLine}
            <span style={isEmphasisRow ? { color: '#d4380d', fontWeight: 700 } : undefined}>
              <Amount value={value} />
            </span>
            {isLowest && (
              <Tag color="green" style={{ marginLeft: 4 }}>
                最低
              </Tag>
            )}
            {rateSuffix}
            {typeTag}
          </>
        )
      },
    })),
  ]

  return (
    <>
      <Table<PriceTreeNode>
        size="middle"
        rowKey="key"
        columns={columns}
        dataSource={tree}
        pagination={false}
        bordered
        sticky
        rowClassName={rowClassName}
        // 供应商多时左右拖动；项目列固定，供应商列最小宽度由 minWidth 保证
        scroll={{ x: 'max-content' }}
        expandable={{
          expandedRowKeys: expandedKeys,
          onExpandedRowsChange: (keys) => setExpandedKeys(keys as string[]),
        }}
        locale={{ emptyText: NA_TEXT }}
      />
      <Typography.Text
        type="secondary"
        style={{ fontSize: 12, display: 'block', marginTop: 8 }}
      >
        同一供应商的多份报价列已相邻排列（灰色竖线分组）· 列较多时可左右拖动查看
      </Typography.Text>
    </>
  )
}
