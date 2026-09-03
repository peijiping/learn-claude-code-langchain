import { useAgentStream } from '@hooks/useAgentStream'
import { useAgentStore } from '@store/agentStore'
import Sidebar from '@components/Sidebar/Sidebar'
import ChatPanel from '@components/Chat/ChatPanel'
import SettingsPanel from '@components/SettingsPanel'
import StatusBar from '@components/StatusBar'
import Toast from '@components/common/Toast'

export default function App(): JSX.Element {
  useAgentStream()

  const settingsOpen = useAgentStore((s) => s.settingsOpen)

  return (
    <div className="app">
      <div className="app-body">
        <Sidebar />
        <ChatPanel />
      </div>
      <StatusBar />
      {settingsOpen && <SettingsPanel />}
      <Toast />
    </div>
  )
}