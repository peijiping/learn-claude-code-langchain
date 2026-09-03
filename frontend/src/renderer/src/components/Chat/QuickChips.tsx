const chips = [
  { icon: '💻', label: '应用开发', prompt: '帮我开发一个小工具 / 应用' },
  { icon: '📊', label: '项目理解', prompt: '帮我梳理一下当前项目的结构与核心逻辑' },
  { icon: '🎮', label: '游戏创意', prompt: '给我一个有趣的游戏创意并给出实现思路' },
  { icon: '🔧', label: '工具脚本', prompt: '写一个便捷的脚本工具' }
]

export default function QuickChips({ onPick }: { onPick: (prompt: string) => void }): JSX.Element {
  return (
    <div className="quickchips">
      {chips.map((c) => (
        <button key={c.label} className="quickchip" onClick={() => onPick(c.prompt)}>
          <span className="chip-icon">{c.icon}</span>
          <span>{c.label}</span>
        </button>
      ))}
    </div>
  )
}