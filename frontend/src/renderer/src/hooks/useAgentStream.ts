import { useEffect } from 'react'
import { useAgentStore, ConnState, PythonState } from '@store/agentStore'
import type { UiEvent } from '@protocols/agentProtocol'

/**
 * useAgentStream - 订阅主进程转发的后端事件与状态，写入 agentStore。
 * 渲染进程不直接连 Python，只经 preload 桥接收。
 */
export function useAgentStream(): void {
  const handleEvent = useAgentStore((s) => s.handleEvent)
  const setConnection = useAgentStore((s) => s.setConnection)
  const setPython = useAgentStore((s) => s.setPython)
  const connection = useAgentStore((s) => s.connection)

  useEffect(() => {
    if (!window.agent) return // preload 未就绪时静默跳过，避免整树崩溃
    const offEvent = window.agent.onEvent((e) => handleEvent(e as UiEvent))
    const offStatus = window.agent.onStatus((s) => setConnection(s as ConnState))
    const offPython = window.agent.onPythonStatus((s) => setPython(s as PythonState))
    // 初次进入查询一次连接与会话
    useAgentStore.getState().refreshSessions().catch(() => undefined)
    return () => {
      offEvent()
      offStatus()
      offPython()
    }
  }, [handleEvent, setConnection, setPython])

  // 启动瞬间后端往往尚未就绪，首次 listSessions 会超时；
  // 等 WebSocket 真正连上后再补一次会话列表查询
  useEffect(() => {
    if (connection === 'connected') {
      useAgentStore.getState().refreshSessions().catch(() => undefined)
    }
  }, [connection])
}