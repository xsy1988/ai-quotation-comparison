import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Modal, Space, Tooltip, Typography } from 'antd'
import { DownloadOutlined, EyeOutlined } from '@ant-design/icons'
import { downloadQuoteSource, fetchQuoteSourcePreview, httpStatus } from '../api/client'

/** 1.2 MB / 340 KB 这类可读体积 */
function formatSize(bytes: number | null | undefined): string | null {
  if (bytes === null || bytes === undefined) return null
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

type PreviewKind = 'pdf' | 'image' | 'other'

function previewKindOf(filename: string, contentType: string): PreviewKind {
  const lower = filename.toLowerCase()
  if (contentType.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|svg)$/.test(lower)) {
    return 'image'
  }
  if (contentType === 'application/pdf' || lower.endsWith('.pdf')) return 'pdf'
  return 'other'
}

export interface SourceFileInfo {
  name: string
  sizeBytes?: number | null
  previewable?: boolean
}

interface Props extends SourceFileInfo {
  quoteId: number
  /** buttons：详情页那种独立按钮；links：列表里紧凑的文字按钮 */
  variant?: 'buttons' | 'links'
  /** 是否同时显示文件体积 */
  showSize?: boolean
}

/**
 * 源文件查看入口：点文件名即可预览，右侧提供下载。
 * 预览走 /source/preview（仅 pdf/图片内联），其它格式直接提示下载。
 */
export default function SourceFileActions({
  quoteId,
  name,
  sizeBytes,
  previewable = true,
  variant = 'buttons',
  showSize = true,
}: Props) {
  const [downloading, setDownloading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [preview, setPreview] = useState<{ url: string; kind: PreviewKind } | null>(null)
  const [error, setError] = useState<string | null>(null)

  // blob URL 用完即弃，避免预览多次后堆积内存
  useEffect(
    () => () => {
      if (preview) URL.revokeObjectURL(preview.url)
    },
    [preview],
  )

  const closePreview = useCallback(() => setPreview(null), [])

  const onDownload = async () => {
    setError(null)
    setDownloading(true)
    try {
      await downloadQuoteSource(quoteId)
    } catch (err) {
      setError(httpStatus(err) === 404 ? '源文件不存在或已被清理' : '下载源文件失败')
    } finally {
      setDownloading(false)
    }
  }

  const onPreview = async () => {
    setError(null)
    setPreviewLoading(true)
    try {
      const { blob, filename, contentType } = await fetchQuoteSourcePreview(quoteId)
      setPreview({ url: URL.createObjectURL(blob), kind: previewKindOf(filename, contentType) })
    } catch (err) {
      if (httpStatus(err) === 415) {
        setError('该格式不支持在线预览，请下载后查看')
      } else if (httpStatus(err) === 404) {
        setError('源文件不存在或已被清理')
      } else {
        setError('预览源文件失败')
      }
    } finally {
      setPreviewLoading(false)
    }
  }

  const sizeText = showSize ? formatSize(sizeBytes) : null
  const linkStyle = { padding: 0, height: 'auto' as const }

  return (
    <Space direction="vertical" size={2} style={{ maxWidth: '100%' }}>
      <Space size={4} wrap={false} style={{ maxWidth: '100%' }}>
        <Tooltip title={previewable ? `点击预览：${name}` : `${name}（不支持在线预览，请下载查看）`}>
          <Button
            type="link"
            size="small"
            style={linkStyle}
            loading={previewLoading}
            onClick={previewable ? onPreview : onDownload}
          >
            <Typography.Text ellipsis style={{ maxWidth: 240 }}>
              {name}
            </Typography.Text>
          </Button>
        </Tooltip>
        {sizeText && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {sizeText}
          </Typography.Text>
        )}
      </Space>
      {variant === 'buttons' ? (
        <Space size={8}>
          <Button size="small" icon={<EyeOutlined />} disabled={!previewable} onClick={onPreview}>
            在线预览
          </Button>
          <Button size="small" icon={<DownloadOutlined />} loading={downloading} onClick={onDownload}>
            下载
          </Button>
        </Space>
      ) : (
        <Space size={8}>
          {previewable && (
            <Button
              type="link"
              size="small"
              style={linkStyle}
              icon={<EyeOutlined />}
              onClick={onPreview}
            >
              预览
            </Button>
          )}
          <Button
            type="link"
            size="small"
            style={linkStyle}
            icon={<DownloadOutlined />}
            loading={downloading}
            onClick={onDownload}
          >
            下载
          </Button>
        </Space>
      )}
      {error && (
        <Alert
          type="warning"
          banner
          showIcon={false}
          message={<Typography.Text style={{ fontSize: 12 }}>{error}</Typography.Text>}
          style={{ padding: '0 8px' }}
        />
      )}
      <Modal
        open={Boolean(preview)}
        title={name}
        footer={null}
        width={1000}
        onCancel={closePreview}
        destroyOnHidden
      >
        {preview?.kind === 'pdf' && (
          <iframe
            title={name}
            src={preview.url}
            style={{ width: '100%', height: '75vh', border: 0 }}
          />
        )}
        {preview?.kind === 'image' && (
          <div style={{ textAlign: 'center' }}>
            <img src={preview.url} alt={name} style={{ maxWidth: '100%', maxHeight: '75vh' }} />
          </div>
        )}
        {preview?.kind === 'other' && (
          <Alert
            type="info"
            showIcon
            message="该格式无法在线预览"
            description="已取回文件内容，请使用「下载」保存后查看。"
          />
        )}
      </Modal>
    </Space>
  )
}
