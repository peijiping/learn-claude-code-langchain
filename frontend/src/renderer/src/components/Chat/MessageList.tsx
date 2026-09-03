import { useEffect, useRef } from 'react'
import { useAgentStore } from '@store/agentStore'
import MessageItem from './MessageItem'

export default function MessageList(): JSX.Element {
  const messages = useAgentStore((s) => s.messages)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  return (
    <div className="msglist">
      {messages.map((m) => (
        <MessageItem key={m.id} msg={m} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}