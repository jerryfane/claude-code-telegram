import type {
  DashboardStats,
  LogEntry,
  MessageRecord,
  Session,
  ToolUsageRecord,
} from '../types'

const BASE = '/api/dashboard'

function headers(): HeadersInit {
  const token = localStorage.getItem('dashboard_token')
  if (token) {
    return { Authorization: `Bearer ${token}` }
  }
  return {}
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: headers() })
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${res.statusText}`)
  }
  return res.json()
}

// --- REST endpoints ---

export async function fetchSessions(params?: {
  user_id?: number
  active?: boolean
  limit?: number
}): Promise<Session[]> {
  const qs = new URLSearchParams()
  if (params?.user_id != null) qs.set('user_id', String(params.user_id))
  if (params?.active != null) qs.set('active', String(params.active))
  if (params?.limit != null) qs.set('limit', String(params.limit))
  const query = qs.toString()
  return get(`/sessions${query ? '?' + query : ''}`)
}

export async function fetchSessionMessages(
  sessionId: string,
  limit = 100,
): Promise<MessageRecord[]> {
  return get(`/sessions/${sessionId}/messages?limit=${limit}`)
}

export async function fetchToolUsage(params?: {
  session_id?: string
  tool_name?: string
  limit?: number
}): Promise<ToolUsageRecord[]> {
  const qs = new URLSearchParams()
  if (params?.session_id) qs.set('session_id', params.session_id)
  if (params?.tool_name) qs.set('tool_name', params.tool_name)
  if (params?.limit != null) qs.set('limit', String(params.limit))
  const query = qs.toString()
  return get(`/tool-usage${query ? '?' + query : ''}`)
}

export async function fetchStats(days = 30): Promise<DashboardStats> {
  return get(`/stats?days=${days}`)
}

// --- SSE live stream ---

export type SSECallback = (entry: LogEntry) => void

export class SSEClient {
  private eventSource: EventSource | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private _onStatusChange: ((status: 'connected' | 'connecting' | 'disconnected') => void) | null = null

  onStatusChange(cb: (status: 'connected' | 'connecting' | 'disconnected') => void) {
    this._onStatusChange = cb
  }

  connect(onEvent: SSECallback): void {
    this.disconnect()
    this._onStatusChange?.('connecting')

    const token = localStorage.getItem('dashboard_token')
    const url = token
      ? `${BASE}/stream?token=${encodeURIComponent(token)}`
      : `${BASE}/stream`

    this.eventSource = new EventSource(url)

    this.eventSource.onopen = () => {
      this._onStatusChange?.('connected')
    }

    this.eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as LogEntry
        onEvent(data)
      } catch {
        // ignore parse errors (keepalives, etc.)
      }
    }

    this.eventSource.onerror = () => {
      this._onStatusChange?.('disconnected')
      this.eventSource?.close()
      this.eventSource = null
      // Auto-reconnect after 5s
      this.reconnectTimer = setTimeout(() => this.connect(onEvent), 5000)
    }
  }

  disconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.eventSource) {
      this.eventSource.close()
      this.eventSource = null
    }
    this._onStatusChange?.('disconnected')
  }
}
