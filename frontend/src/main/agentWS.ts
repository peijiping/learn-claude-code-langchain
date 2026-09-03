export type ConnStatus = 'connecting' | 'connected' | 'disconnected'

export interface AgentWSOptions {
  port: number
  onEvent: (envelope: unknown) => void
  onStatus: (status: ConnStatus) => void
}

/**
 * agentWS - 主进程里的 WebSocket 客户端，连 Python 后端的 ws_bridge。
 * 职责：连接/自动重连（指数退避）、把渲染进程命令发给后端、把后端事件转发给渲染进程。
 */
export class AgentWS {
  private ws: WebSocket | null = null
  private status: ConnStatus = 'disconnected'
  private retryMs = 1000
  private opts: AgentWSOptions
  private manualClose = false
  private queue: string[] = []

  constructor(opts: AgentWSOptions) {
    this.opts = opts
  }

  get url(): string {
    return `ws://127.0.0.1:${this.opts.port}`
  }

  get currentStatus(): ConnStatus {
    return this.status
  }

  private setStatus(s: ConnStatus): void {
    if (this.status !== s) {
      this.status = s
      this.opts.onStatus(s)
    }
  }

  connect(): void {
    this.manualClose = false
    this.setStatus('connecting')
    const ws = new WebSocket(this.url)
    this.ws = ws

    ws.onopen = (): void => {
      this.setStatus('connected')
      this.retryMs = 1000
      // 连接建立后补发排队中的命令
      while (this.queue.length) {
        const raw = this.queue.shift()
        if (raw !== undefined) ws.send(raw)
      }
    }

    ws.onmessage = (ev: MessageEvent): void => {
      try {
        this.opts.onEvent(JSON.parse(ev.data as string))
      } catch (err) {
        this.opts.onEvent({ kind: 'error', payload: { msg: `解析失败: ${(err as Error).message}` } })
      }
    }

    ws.onclose = (): void => {
      if (this.ws === ws) this.ws = null
      if (this.manualClose) {
        this.setStatus('disconnected')
        return
      }
      this.setStatus('disconnected')
      this.scheduleReconnect()
    }

    ws.onerror = (): void => {
      // onclose 会随后触发，交给 onclose 统一处理
      try {
        ws.close()
      } catch {
        /* 忽略 */
      }
    }
  }

  private scheduleReconnect(): void {
    const delay = this.retryMs
    this.retryMs = Math.min(this.retryMs * 2, 30_000)
    this.opts.onStatus('disconnected')
    setTimeout(() => {
      if (!this.manualClose) this.connect()
    }, delay)
  }

  /** 发送一条命令；未连接时进入队列，连接后立刻补发 */
  send(raw: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(raw)
    } else {
      this.queue.push(raw)
      if (!this.ws) this.connect()
    }
  }

  close(): void {
    this.manualClose = true
    if (this.ws) this.ws.close()
    this.ws = null
    this.setStatus('disconnected')
  }
}