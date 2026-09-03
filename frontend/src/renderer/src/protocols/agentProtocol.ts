/**
 * agentProtocol.ts - 前端事件线协议（与后端 streaming_client.py StreamEvent.to_dict 对齐）。
 * JSON 行协议：后端 WSSink 序列化后的每一行就是一条下面的 AgentEvent。
 */

export type StreamEventType =
  | 'thinking_delta'
  | 'content_delta'
  | 'tool_call_start'
  | 'tool_call_delta'
  | 'tool_call'
  | 'turn_end'

export interface AgentEvent {
  type: StreamEventType
  text?: string
  tool_id?: string
  tool_name?: string
  args?: string
  finish_reason?: string
  usage?: Record<string, number>
}

/** 面向 UI 的产物事件（非增量），由 bridge 把底层 event 聚合/透传而来 */
export type UiEvent =
  | { kind: 'event'; payload: AgentEvent }
  | { kind: 'pong'; payload: { msg?: string } }
  | { kind: 'error'; payload: { msg?: string } }
  | { kind: 'goal_status'; payload: { text: string } }
  | { kind: 'tasks'; payload: { text: string } }
  | { kind: 'skills'; payload: { text: string } }
  | { kind: 'sessions'; payload: { sessions: SessionMeta[] } }
  | { kind: 'session'; payload: { num: number; message_count: number } }

export interface SessionMeta {
  num: number
  message_count: number
  file?: string
}

/** 前端 → 后端命令信封 */
export type ControlKind =
  | 'chat'
  | 'session_new'
  | 'session_switch'
  | 'session_clear'
  | 'sessions_list'
  | 'goal_status'
  | 'tasks'
  | 'skills'

export interface WsOutbound {
  kind: ControlKind | 'ping'
  payload?: Record<string, unknown>
}

export function parseWsLine(raw: string): UiEvent {
  const parsed = JSON.parse(raw)
  if (!parsed || typeof parsed.kind !== 'string') {
    throw new Error('不合法的事件行: ' + raw)
  }
  return parsed as UiEvent
}

/** 后端未知的流式事件 type：忽略并告警（前后端版本兼容） */
export function isKnownAgentEvent(ev: AgentEvent): boolean {
  return [
    'thinking_delta',
    'content_delta',
    'tool_call_start',
    'tool_call_delta',
    'tool_call',
    'turn_end'
  ].includes(ev.type)
}