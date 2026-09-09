import { App as AntdApp, Layout, Menu, Typography } from 'antd'
import { Link, Outlet, useLocation } from 'react-router-dom'

const { Header, Content } = Layout

const NAV_ITEMS = [
  { key: '/', label: <Link to="/">新建对比</Link> },
  { key: '/master', label: <Link to="/master">主数据管理</Link> },
]

export default function App() {
  const { pathname } = useLocation()
  const selectedKey = pathname.startsWith('/master') ? '/master' : '/'

  return (
    <AntdApp>
      <Layout style={{ minHeight: '100vh' }}>
        <Header
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 24,
            background: '#001529',
            paddingInline: 24,
          }}
        >
          <Typography.Text strong style={{ color: '#fff', fontSize: 16, whiteSpace: 'nowrap' }}>
            采购报价对比 Agent
          </Typography.Text>
          <Menu
            theme="dark"
            mode="horizontal"
            selectedKeys={[selectedKey]}
            items={NAV_ITEMS}
            style={{ flex: 1, minWidth: 0, background: 'transparent' }}
          />
          <Typography.Text
            style={{ color: 'rgba(255,255,255,0.55)', fontSize: 12, whiteSpace: 'nowrap' }}
          >
            上传多家供应商报价单，自动解析并生成比价界面
          </Typography.Text>
        </Header>
        <Content style={{ padding: 24, maxWidth: 1440, width: '100%', margin: '0 auto' }}>
          <Outlet />
        </Content>
      </Layout>
    </AntdApp>
  )
}
