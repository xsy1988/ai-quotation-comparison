import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import './index.css'
import { isClientError } from './api/client'
import App from './App'
import CreatePage from './pages/CreatePage'
import ProgressPage from './pages/ProgressPage'
import ComparisonPage from './pages/ComparisonPage'
import MasterPage from './pages/MasterPage'
import TaskHistoryPage from './pages/TaskHistoryPage'
import QuoteListPage from './pages/QuoteListPage'
import QuoteDetailPage from './pages/QuoteDetailPage'
import ProjectPage from './pages/ProjectPage'
import SupplierHistoryPage from './pages/SupplierHistoryPage'

dayjs.locale('zh-cn')

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 4xx 重试无意义（且重试被暂停时会卡在假加载态），只对网络/5xx 失败重试一次
      retry: (failureCount, error) => (isClientError(error) ? false : failureCount < 1),
      refetchOnWindowFocus: false,
    },
  },
})

export default function Main() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#1677ff',
          borderRadius: 6,
          fontSize: 14,
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
        },
        components: {
          Table: {
            cellPaddingBlock: 12,
            cellPaddingInline: 14,
            headerBg: '#f5f7fa',
            rowHoverBg: '#f0f7ff',
          },
          Card: {
            headerFontSize: 15,
          },
        },
      }}
    >
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<App />}>
              <Route index element={<CreatePage />} />
              <Route path="tasks" element={<TaskHistoryPage />} />
              <Route path="tasks/:id/progress" element={<ProgressPage />} />
              <Route path="tasks/:id/comparison" element={<ComparisonPage />} />
              <Route path="quotes" element={<QuoteListPage />} />
              <Route path="quotes/:id" element={<QuoteDetailPage />} />
              <Route path="suppliers" element={<MasterPage section="suppliers" />} />
              <Route path="suppliers/:code" element={<SupplierHistoryPage />} />
              <Route path="projects" element={<ProjectPage />} />
              <Route path="master" element={<Navigate to="/master/atoms" replace />} />
              <Route path="master/atoms" element={<MasterPage section="atoms" />} />
              <Route path="master/aliases" element={<MasterPage section="aliases" />} />
              <Route path="master/categories" element={<MasterPage section="categories" />} />
              <Route path="master/drawers" element={<MasterPage section="drawers" />} />
              <Route path="master/dim-groups" element={<MasterPage section="dim-groups" />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </ConfigProvider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Main />
  </StrictMode>,
)
