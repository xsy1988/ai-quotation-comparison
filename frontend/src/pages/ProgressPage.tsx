import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, List, message, Steps, Tag, Typography } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import { subscribeProgress } from '../api/client'
import type { ProgressPayload } from '../types'

// 五段：查重→接入→解析→匹配→校验；后端 parse_log.stage 编码 → 段下标
const STAGES = ['查重', '接入', '解析', '匹配', '校验']
const STAGE_INDEX: Record<string, number> = {
  task: 0,
  dedup: 1, // 查重+文件接入同属 dedup 日志段
  layout: 2,
  match: 3,
  validate: 4,
  persist: 4,
}
// 进度条上方状态标签的友好文案
const STAGE_LABEL: Record<string, string> = {
  task: '已上传',
  dedup: '接入查重',
  layout: '版面解析',
  match: '语义映射',
  validate: '校验',
  persist: '落库',
}

const DONE_STATUSES = new Set(['parsed', 'reviewed'])
const FAILED_STATUSES = new Set(['failed', 'parse_failed'])

function stageIndex(stage: string | null): number {
  if (!stage) return -1
  return STAGE_INDEX[stage] ?? 0
}

export default function ProgressPage() {
  const { id } = useParams<{ id: string }>()
  const taskId = Number(id)
  const navigate = useNavigate()
  const [payload, setPayload] = useState<ProgressPayload | null>(null)
  const [streamError, setStreamError] = useState<string | null>(null)
  const [taskError, setTaskError] = useState<string | null>(null)
  const [connected, setConnected] = useState(true)
  const finished = useRef(false)
  const reconnects = useRef(0)

  const start = useCallback(() => {
    setConnected(true)
    return subscribeProgress(
      taskId,
      (event, data) => {
        if (event === 'error') {
          setStreamError('detail' in data ? data.detail : '进度流异常')
          finished.current = true
          return
        }
        setPayload(data as ProgressPayload)
        if (event === 'done') {
          finished.current = true
          const p = data as ProgressPayload
          if (p.task_status === 'failed') {
            // LLM 故障等致命错误：任务已中止，展示错误信息，不跳转比价页
            const firstError = p.quotes.find((q) => q.error)?.error
            setTaskError(firstError ?? '任务失败，详见各报价单错误信息')
            message.error('任务已中止，未进入比价')
          } else {
            window.setTimeout(
              () => navigate(`/tasks/${taskId}/comparison`, { replace: true }),
              800,
            )
          }
        }
      },
      () => {
        // 网络层断开（收到 done/error 后后端主动关流也会触发，此时 finished=true 不重连）
        setConnected(false)
        if (finished.current || reconnects.current >= 1) return
        reconnects.current += 1
        window.setTimeout(() => {
          if (!finished.current) start()
        }, 1500)
      },
    )
  }, [taskId, navigate])

  useEffect(() => {
    if (!Number.isFinite(taskId)) {
      setStreamError('无效的任务 ID')
      return
    }
    const cancel = start()
    return () => cancel()
  }, [start, taskId])

  const taskStatus = payload?.task_status ?? 'parsing'
  const allSettled =
    payload !== null && payload.quotes.every((q) => DONE_STATUSES.has(q.parse_status))

  return (
    <Card style={{ maxWidth: 860, margin: '24px auto' }}>
      <Typography.Title level={4}>任务 #{taskId} 解析进度</Typography.Title>
      <div style={{ marginBottom: 16 }}>
        <Tag color={connected ? 'green' : 'red'}>
          {connected ? '进度推送已连接' : '连接中断，重连中…'}
        </Tag>
        <Tag
          color={
            taskStatus === 'failed'
              ? 'red'
              : DONE_STATUSES.has(taskStatus)
                ? 'green'
                : 'processing'
          }
        >
          任务状态：{taskStatus}
        </Tag>
      </div>
      {streamError && (
        <Alert type="error" message={streamError} style={{ marginBottom: 16 }} showIcon />
      )}
      {taskError && (
        <Alert
          type="error"
          message="任务已中止"
          description={taskError}
          style={{ marginBottom: 16 }}
          showIcon
        />
      )}
      {!payload && !streamError && (
        <div style={{ padding: 24, textAlign: 'center' }}>等待进度数据…</div>
      )}
      {payload && (
        <List
          dataSource={payload.quotes}
          locale={{ emptyText: '暂无报价单' }}
          renderItem={(quote) => {
            const failed = FAILED_STATUSES.has(quote.parse_status)
            const done = DONE_STATUSES.has(quote.parse_status)
            const idx = stageIndex(quote.stage)
            return (
              <List.Item>
                <div style={{ width: '100%' }}>
                  <div style={{ marginBottom: 8 }}>
                    <Typography.Text strong>{quote.supplier_name}</Typography.Text>{' '}
                    {failed ? (
                      <Tag color="red">解析失败：{quote.parse_status}</Tag>
                    ) : done ? (
                      <Tag color="green">解析完成</Tag>
                    ) : (
                      <Tag color="processing">
                        {quote.stage ? (STAGE_LABEL[quote.stage] ?? quote.stage) : '排队中'}
                      </Tag>
                    )}
                  </div>
                  {failed && quote.error && (
                    <Typography.Paragraph
                      type="danger"
                      style={{ marginBottom: 8 }}
                      ellipsis={{ rows: 3, expandable: true }}
                    >
                      {quote.error}
                    </Typography.Paragraph>
                  )}
                  <Steps
                    size="small"
                    current={failed ? idx : done ? STAGES.length : idx}
                    status={failed ? 'error' : done ? 'finish' : 'process'}
                    items={STAGES.map((s) => ({ title: s }))}
                  />
                </div>
              </List.Item>
            )
          }}
        />
      )}
      <Button
        type="primary"
        block
        style={{ marginTop: 16 }}
        disabled={!allSettled}
        onClick={() => navigate(`/tasks/${taskId}/comparison`)}
      >
        进入比价界面
      </Button>
    </Card>
  )
}
