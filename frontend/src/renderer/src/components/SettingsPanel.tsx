import { useAgentStore, PanelKind } from '@store/agentStore'

const tabs: { key: PanelKind; label: string }[] = [
  { key: 'goal', label: '目标' },
  { key: 'tasks', label: '待办' },
  { key: 'skills', label: '技能' },
  { key: 'settings', label: '设置' }
]

/** 右侧抽屉：目标/待办/技能等只读展示面板 */
export default function SettingsPanel(): JSX.Element {
  const activePanel = useAgentStore((s) => s.activePanel)
  const panelText = useAgentStore((s) => s.panelText)
  const openPanel = useAgentStore((s) => s.openPanel)
  const closeSettings = useAgentStore((s) => s.closeSettings)

  return (
    <>
      <div className="drawer-mask" onClick={closeSettings} />
      <aside className="drawer">
        <div className="drawer-tabs">
          {tabs.map((t) => (
            <button
              key={t.key}
              className={`drawer-tab ${t.key === activePanel ? 'active' : ''}`}
              onClick={() => void openPanel(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <pre className="drawer-content">{panelText}</pre>
      </aside>
    </>
  )
}