import { Breadcrumb } from 'antd'
import { Link, useLocation } from 'react-router-dom'

interface Crumb {
  label: string
  /** 有独立路由的层级才可点击；动态段（任务/报价单）与末级不给链接 */
  to?: string
}

/** 固定路径段 → 面包屑标题 */
const SEGMENT_LABEL: Record<string, string> = {
  tasks: '报价对比历史',
  quotes: '报价单数据',
  suppliers: '供应商管理',
  projects: '项目管理',
  master: '基础数据维护',
  atoms: '原子工艺',
  aliases: '别名管理',
  categories: '品类管理',
  drawers: '抽屉管理',
  'dim-groups': '分组管理',
  comparison: '比价结果',
  progress: '解析进度',
}

/** 动态段标题：用父级路径段判断语义（任务 #12 / 报价单 #34 / 供应商编码） */
function dynamicLabel(parent: string, value: string): string {
  if (parent === 'tasks') return `任务 #${value}`
  if (parent === 'quotes') return `报价单 #${value}`
  if (parent === 'suppliers') return `供应商 ${value}`
  return value
}

function crumbsOf(pathname: string): Crumb[] {
  const parts = pathname.split('/').filter(Boolean)
  if (parts.length === 0) return [{ label: '发起报价对比' }]
  const crumbs: Crumb[] = [{ label: '首页', to: '/' }]
  let path = ''
  parts.forEach((part, index) => {
    const parent = parts[index - 1] ?? ''
    path += `/${part}`
    if (SEGMENT_LABEL[part]) {
      crumbs.push({ label: SEGMENT_LABEL[part], to: path })
      return
    }
    crumbs.push({ label: dynamicLabel(parent, part) })
  })
  return crumbs
}

/** 顶部面包屑：一眼看清当前页面在站点里的层级 */
export default function AppBreadcrumb() {
  const { pathname } = useLocation()
  const crumbs = crumbsOf(pathname)
  return (
    <Breadcrumb
      style={{ marginBottom: 12 }}
      items={crumbs.map((crumb, index) => ({
        title:
          crumb.to && index !== crumbs.length - 1 ? (
            <Link to={crumb.to}>{crumb.label}</Link>
          ) : (
            crumb.label
          ),
      }))}
    />
  )
}
