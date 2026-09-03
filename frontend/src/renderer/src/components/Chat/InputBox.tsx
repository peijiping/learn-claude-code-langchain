import { useRef } from 'react'
import { Icon } from '@components/common/Icon'
import { useAgentStore } from '@store/agentStore'

interface InputBoxProps {
  value: string
  onChange: (v: string) => void
  onSend: () => void
}

/** 中央核心输入区：textarea + 工具栏 + 上下文条 */
export default function InputBox({ value, onChange, onSend }: InputBoxProps): JSX.Element {
  const isSending = useAgentStore((s) => s.isSending)
  const stop = useAgentStore((s) => s.stop)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const modelName = 'DeepSeek-V4-Flash'

  const autoGrow = (el: HTMLTextAreaElement): void => {
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (!isSending) onSend()
    }
  }

  return (
    <div className="composer">
      <textarea
        ref={taRef}
        className="composer-input"
        placeholder="有什么我可以帮你的吗？"
        value={value}
        rows={1}
        onChange={(e) => {
          onChange(e.target.value)
          autoGrow(e.target)
        }}
        onKeyDown={onKeyDown}
      />

      <div className="composer-toolbar">
        <div className="toolbar-left">
          <button className="tool-btn" title="添加内容">
            <Icon name="plus" size={16} />
          </button>
          <button className="tool-btn access">
            完全访问 <Icon name="chevronDown" size={12} />
          </button>
          <button className="tool-btn" title="附件（后续增量）">
            <Icon name="paperclip" size={16} />
          </button>
          <button className="tool-btn" title="图片（后续增量）">
            <Icon name="chart" size={16} />
          </button>
        </div>

        <div className="toolbar-right">
          <button className="tool-btn model" title="模型选择（占位）">
            <span className="model-name">{modelName}</span>
            <Icon name="chevronDown" size={12} />
          </button>
          <button className="tool-btn" title="通知开关（占位）">
            <Icon name="bell" size={16} />
          </button>
          {isSending ? (
            <button className="send-btn stop" onClick={stop} title="停止">
              <Icon name="stop" size={15} />
              <span>停止</span>
            </button>
          ) : (
            <button className="send-btn" onClick={onSend} disabled={!value.trim()} title="发送">
              <Icon name="send" size={15} />
            </button>
          )}
        </div>
      </div>

      <div className="composer-context">
        <span className="ctx-chip">
          <Icon name="terminal" size={13} /> 本地 <Icon name="chevronDown" size={11} />
        </span>
        <span className="ctx-chip">
          <Icon name="folder" size={13} /> learn-claude-code-… <Icon name="chevronDown" size={11} />
        </span>
      </div>
    </div>
  )
}