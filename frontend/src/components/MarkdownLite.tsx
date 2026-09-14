import { useEffect, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import { Typography } from 'antd'

/** 极简 Markdown 渲染：「# 标题」「- 列表」「| 表格 |」与普通段落，够用于「其它信息」。 */
export default function MarkdownLite({ text }: { text: string }) {
  return (
    <div style={{ fontSize: 13, lineHeight: 1.75 }}>
      {text.replace(/\r\n/g, '\n').split('\n').map((raw, i) => renderLine(raw.trimEnd(), i))}
    </div>
  )
}

function renderLine(line: string, key: number): ReactElement {
  const heading = /^(#{1,6})\s+(.*)$/.exec(line)
  if (heading) {
    return (
      <Typography.Text key={key} strong style={{ display: 'block', marginTop: key === 0 ? 0 : 8 }}>
        {heading[2]}
      </Typography.Text>
    )
  }
  if (line.includes('|')) {
    // 表格行：等宽 + 保留原始分隔，避免误读列边界
    return (
      <div
        key={key}
        style={{
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          whiteSpace: 'pre-wrap',
        }}
      >
        {line}
      </div>
    )
  }
  const bullet = /^([-*+]|\d+[.)])\s+(.*)$/.exec(line.trim())
  if (bullet) {
    return (
      <div key={key} style={{ paddingLeft: 12 }}>
        · {bullet[2]}
      </div>
    )
  }
  if (!line.trim()) return <div key={key} style={{ height: 6 }} />
  return (
    <div key={key} style={{ whiteSpace: 'pre-wrap' }}>
      {line}
    </div>
  )
}

/** 折叠展示长文本（「其它信息」可能很长，默认折叠若干行） */
export function CollapsibleText({
  text,
  collapsedHeight = 120,
}: {
  text: string
  collapsedHeight?: number
}) {
  const [expanded, setExpanded] = useState(false)
  const [overflow, setOverflow] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setOverflow((ref.current?.scrollHeight ?? 0) > collapsedHeight + 8)
  }, [text, collapsedHeight])

  return (
    <div>
      <div
        ref={ref}
        style={{
          maxHeight: expanded ? undefined : collapsedHeight,
          overflow: expanded ? undefined : 'hidden',
        }}
      >
        <MarkdownLite text={text} />
      </div>
      {overflow && (
        <Typography.Link onClick={() => setExpanded((v) => !v)}>
          {expanded ? '收起' : '展开全部'}
        </Typography.Link>
      )}
    </div>
  )
}
