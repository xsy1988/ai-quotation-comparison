import { useState } from 'react'
import { App as AntdApp, Layout, Menu, Typography } from 'antd'
import {
  AppstoreOutlined,
  DatabaseOutlined,
  FolderOpenOutlined,
  HistoryOutlined,
  PlusSquareOutlined,
  ShopOutlined,
} from '@ant-design/icons'
import { Link, Outlet, useLocation } from 'react-router-dom'

const { Header, Sider, Content } = Layout

/** 左侧菜单：key 为路由前缀，最长前缀匹配决定选中项 */
const NAV_ITEMS = [
  { key: '/', label: <Link to="/">发起报价对比</Link>, icon: <PlusSquareOutlined /> },
  { key: '/tasks', label: <Link to="/tasks">报价对比历史</Link>, icon: <HistoryOutlined /> },
  { key: '/quotes', label: <Link to="/quotes">报价单数据</Link>, icon: <DatabaseOutlined /> },
  { key: '/suppliers', label: <Link to="/suppliers">供应商管理</Link>, icon: <ShopOutlined /> },
  { key: '/projects', label: <Link to="/projects">项目管理</Link>, icon: <AppstoreOutlined /> },
  {
    key: '/master',
    label: '基础数据维护',
    icon: <FolderOpenOutlined />,
    children: [
      { key: '/master/atoms', label: <Link to="/master/atoms">原子工艺</Link> },
      { key: '/master/aliases', label: <Link to="/master/aliases">别名管理</Link> },
      { key: '/master/categories', label: <Link to="/master/categories">品类管理</Link> },
      { key: '/master/drawers', label: <Link to="/master/drawers">抽屉管理</Link> },
      { key: '/master/dim-groups', label: <Link to="/master/dim-groups">分组管理</Link> },
    ],
  },
]

const ALL_KEYS = NAV_ITEMS.flatMap((item) =>
  item.children ? [item.key, ...item.children.map((c) => c.key)] : [item.key],
)

/** 最长前缀匹配，避免 /master/atoms 落到 / 或 /master 上 */
function matchKey(pathname: string): string {
  const hit = ALL_KEYS.filter((key) => key !== '/')
    .filter((key) => pathname === key || pathname.startsWith(`${key}/`))
    .sort((a, b) => b.length - a.length)[0]
  return hit ?? '/'
}

export default function App() {
  const { pathname } = useLocation()
  const selectedKey = matchKey(pathname)
  const [collapsed, setCollapsed] = useState(false)

  return (
    <AntdApp>
      <Layout style={{ minHeight: '100vh' }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        width={208}
        breakpoint="lg"
        style={{ position: 'sticky', top: 0, height: '100vh', overflow: 'auto' }}
      >
        <div
          style={{
            color: '#fff',
            padding: collapsed ? '16px 4px' : '16px 20px',
            fontSize: collapsed ? 12 : 15,
            fontWeight: 600,
            whiteSpace: 'nowrap',
            textAlign: 'center',
            background: 'rgba(255,255,255,0.06)',
          }}
        >
          {collapsed ? '采购 Agent' : '采购报价对比 Agent'}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          defaultOpenKeys={selectedKey.startsWith('/master') ? ['/master'] : []}
          items={NAV_ITEMS}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: '#fff',
            borderBottom: '1px solid #f0f0f0',
            paddingInline: 24,
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <Typography.Text type="secondary">
            上传多家供应商报价单，自动解析并生成比价界面
          </Typography.Text>
        </Header>
        <Content style={{ padding: 24, maxWidth: 1440, width: '100%', margin: '0 auto' }}>
          <Outlet />
        </Content>
      </Layout>
      </Layout>
    </AntdApp>
  )
}
