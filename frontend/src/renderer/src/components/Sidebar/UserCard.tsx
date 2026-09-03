import { Icon } from '@components/common/Icon'
import { useAgentStore } from '@store/agentStore'

/** 底部用户卡：头像 + 用户名 + Lite Badge + 设置（后端无账号体系，本期硬编码） */
export default function UserCard(): JSX.Element {
  const openPanel = useAgentStore((s) => s.openPanel)

  return (
    <div className="usercard">
      <div className="usercard-avatar">P</div>
      <div className="usercard-meta">
        <div className="usercard-name">pei'ji'ping</div>
        <span className="badge-lite">Lite</span>
      </div>
      <button className="usercard-settings" title="设置" onClick={() => void openPanel('settings')}>
        <Icon name="gear" size={15} />
      </button>
    </div>
  )
}