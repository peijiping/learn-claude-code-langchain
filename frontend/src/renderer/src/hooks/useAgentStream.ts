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

  useEffect(() => {
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
}