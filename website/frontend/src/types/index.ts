export type EventKind =
  | 'THINKING'
  | 'TOOL_CALL'
  | 'TOOL_RESULT'
  | 'RESPONSE'
  | 'ERROR'
  | 'SESSION_START'
  | 'SESSION_END'

export interface LogEntry {
  id: string
  timestamp: string
  kind: EventKind
  session_id: string
  user_id: number
  content: string
  tool_name?: string | null
  tool_input?: Record<string, unknown> | null
  expanded?: boolean
}

export interface Session {
  session_id: string
  user_id: number
  project_path: string
  created_at: string
  last_used: string
  total_cost: number
  total_turns: number
  message_count: number
  is_active: boolean
}

export interface MessageRecord {
  message_id: number
  session_id: string
  user_id: number
  timestamp: string
  prompt: string
  response: string | null
  cost: number
  duration_ms: number | null
  error: string | null
}

export interface ToolUsageRecord {
  id: number
  session_id: string
  message_id: number | null
  tool_name: string
  tool_input: Record<string, unknown> | null
  timestamp: string
  success: boolean
  error_message: string | null
}

export interface DashboardStats {
  summary: {
    total_users: number
    total_sessions: number
    total_messages: number
    total_cost: number
    active_sessions: number
  }
  daily: Array<{
    date: string
    active_users: number
    total_messages: number
    total_cost: number
    avg_duration: number | null
  }>
  tool_stats: Array<{
    tool_name: string
    count: number
  }>
}
