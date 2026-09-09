import { Alert, Button, Card, Space, Tag, Typography } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import { generateAiSummary, getAiSummary } from '../api/client'
import type { AiSummary } from '../types'

/** 502 时 axios 抛出，error.response.data.detail 为后端原始错误信息 */
function errorDetail(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: string } | undefined)?.detail
    if (detail) return detail
  }
  return error instanceof Error ? error.message : String(error)
}

export default function AiSummarySection({ taskId }: { taskId: number }) {
  const queryClient = useQueryClient()

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['ai-summary', taskId],
    queryFn: () => getAiSummary(taskId),
    enabled: Number.isFinite(taskId),
    retry: false,
  })

  const mutation = useMutation({
    mutationFn: () => generateAiSummary(taskId),
    onSuccess: (summary: AiSummary) => {
      queryClient.setQueryData(['ai-summary', taskId], { summary })
    },
    // 失败不降级不缓存：错误留在 mutation.error，由下方 Alert 展示
  })

  const summary = data?.summary ?? null

  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <span>AI 综合建议</span>
          {summary && <Tag color="purple">AI 生成</Tag>}
          {summary?.check_status === 'pass' && (
            <Tag color="green">数字回检通过</Tag>
          )}
          {summary?.check_status === 'mismatch' && (
            <Tag color="orange">部分数字与机械对比不一致，仅供参考</Tag>
          )}
        </Space>
      }
      extra={
        <Button
          type="primary"
          size="small"
          loading={mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          {summary ? '重新生成' : '生成 AI 建议'}
        </Button>
      }
    >
      {isLoading && <Typography.Text type="secondary">加载 AI 建议…</Typography.Text>}
      {isError && !summary && (
        <Alert type="error" message="加载 AI 建议失败" description={errorDetail(error)} showIcon />
      )}

      {mutation.isError && (
        <Alert
          type="error"
          style={{ marginBottom: 12 }}
          message="生成 AI 建议失败（LLM 服务不可用）"
          description={errorDetail(mutation.error)}
          showIcon
        />
      )}

      {summary ? (
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
          {summary.content}
        </Typography.Paragraph>
      ) : (
        !isLoading &&
        !isError && (
          <Typography.Text type="secondary">
            尚未生成 AI 综合建议。点击右上角按钮基于机械对比结果生成（LLM 仅读取结构化对比数据，不接触原始文件）。
          </Typography.Text>
        )
      )}
    </Card>
  )
}
