<template>
  <div class="h-screen bg-gray-950 font-mono text-sm">
    <div class="flex flex-col h-full max-w-6xl mx-auto border-x border-gray-800/50 shadow-[0_0_40px_rgba(0,0,0,0.5)]">
      <!-- Top bar -->
      <header class="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2 bg-gray-900 border-b border-gray-800 flex-shrink-0">
        <!-- Row 1: Title + search + connection -->
        <h1 class="text-green-400 font-bold text-base tracking-wide">
          <span class="hidden sm:inline">CLAUDE AGENT TERMINAL</span>
          <span class="sm:hidden">TERMINAL</span>
        </h1>

        <input
          v-model="searchQuery"
          type="text"
          placeholder="Search..."
          class="ml-auto bg-gray-800 text-gray-300 text-xs rounded px-2 py-1 border border-gray-700 w-32 sm:w-48 focus:border-green-500 outline-none"
        />

        <span class="flex items-center gap-1.5 sm:hidden">
          <span
            class="w-2 h-2 rounded-full"
            :class="{
              'bg-green-500': connectionStatus === 'connected',
              'bg-yellow-500 animate-pulse': connectionStatus === 'connecting',
              'bg-red-500': connectionStatus === 'disconnected',
            }"
          ></span>
        </span>

        <!-- Row 2: Session selector + auto-scroll -->
        <div class="flex items-center gap-3 w-full sm:w-auto">
          <select
            v-model="selectedSession"
            class="bg-gray-800 text-gray-300 text-xs rounded px-2 py-1 border border-gray-700 focus:border-green-500 outline-none flex-1 sm:flex-none"
          >
            <option value="">All sessions</option>
            <option v-for="s in sessions" :key="s.session_id" :value="s.session_id">
              {{ s.session_id.slice(0, 8) }}... ({{ s.project_path.split('/').pop() }})
            </option>
          </select>

          <label class="flex items-center gap-1 text-xs text-gray-400 cursor-pointer flex-shrink-0">
            <input type="checkbox" v-model="autoScroll" class="accent-green-500" />
            Auto-scroll
          </label>
        </div>

        <!-- Row 3: Filters -->
        <div class="flex flex-wrap gap-2 text-xs w-full">
          <label v-for="kind in filterKinds" :key="kind" class="flex items-center gap-1 cursor-pointer">
            <input type="checkbox" v-model="activeFilters" :value="kind" class="accent-green-500" />
            <span :class="badgeClass(kind)" class="px-1 rounded text-[10px] font-bold">{{ kind }}</span>
          </label>
        </div>
      </header>

      <!-- Stats bar -->
      <div v-if="stats" class="flex flex-wrap gap-x-6 gap-y-1 px-4 py-1.5 bg-gray-900/50 border-b border-gray-800/50 text-xs text-gray-500 flex-shrink-0">
        <span>Sessions: <span class="text-gray-300">{{ stats.summary.active_sessions }}</span> active</span>
        <span>Messages: <span class="text-gray-300">{{ stats.summary.total_messages }}</span></span>
        <span>Cost: <span class="text-gray-300">${{ (stats.summary.total_cost || 0).toFixed(2) }}</span></span>
        <span>Users: <span class="text-gray-300">{{ stats.summary.total_users }}</span></span>
      </div>

      <!-- Terminal log area -->
      <main ref="terminalRef" class="flex-1 overflow-y-auto terminal-scroll px-2 sm:px-4 py-2 space-y-0.5">
        <div v-if="filteredEntries.length === 0" class="text-gray-600 py-8 text-center">
          <template v-if="loading">Loading logs...</template>
          <template v-else>No log entries{{ selectedSession ? ' for this session' : '' }}. Waiting for agent activity...</template>
        </div>

        <div
          v-for="entry in filteredEntries"
          :key="entry.id"
          class="group hover:bg-gray-900/50 rounded px-1 sm:px-2 py-0.5 cursor-pointer transition-colors"
          @click="entry.expanded = !entry.expanded"
        >
          <!-- Compact row -->
          <div class="flex items-start gap-1 sm:gap-2">
            <span class="text-gray-600 flex-shrink-0 w-16">{{ formatTime(entry.timestamp) }}</span>
            <span :class="badgeClass(entry.kind)" class="px-1 sm:px-1.5 rounded text-[10px] font-bold flex-shrink-0 w-14 sm:w-20 text-center truncate">
              {{ entry.kind }}
            </span>
            <span class="text-gray-600 flex-shrink-0 w-16 truncate hidden sm:inline" :title="entry.session_id">
              {{ entry.session_id ? entry.session_id.slice(0, 8) : '---' }}
            </span>
            <span class="text-gray-300 truncate flex-1" :class="{ 'text-green-400': entry.kind === 'RESPONSE', 'text-red-400': entry.kind === 'ERROR' }">
              <template v-if="entry.kind === 'TOOL_CALL' && entry.tool_name">
                <span class="text-blue-400">{{ entry.tool_name }}</span>
                <span class="text-gray-500 ml-1 hidden sm:inline">{{ summarizeToolInput(entry.tool_input) }}</span>
              </template>
              <template v-else>{{ entry.content }}</template>
            </span>
          </div>

          <!-- Expanded detail -->
          <div v-if="entry.expanded" class="ml-0 sm:ml-36 mt-1 mb-2 p-2 bg-gray-900 rounded border border-gray-800 text-xs">
            <div class="text-gray-500 mb-1">Session: {{ entry.session_id }} | User: {{ entry.user_id }}</div>
            <pre class="text-gray-300 whitespace-pre-wrap break-words max-h-64 overflow-y-auto">{{ formatDetail(entry) }}</pre>
          </div>
        </div>
      </main>

      <!-- Bottom status bar -->
      <footer class="flex flex-wrap items-center gap-3 px-4 py-1.5 bg-gray-900 border-t border-gray-800 text-xs flex-shrink-0">
        <span class="flex items-center gap-1.5">
          <span
            class="w-2 h-2 rounded-full"
            :class="{
              'bg-green-500': connectionStatus === 'connected',
              'bg-yellow-500 animate-pulse': connectionStatus === 'connecting',
              'bg-red-500': connectionStatus === 'disconnected',
            }"
          ></span>
          <span class="text-gray-400">{{ connectionStatus }}</span>
        </span>
        <span class="text-gray-600">|</span>
        <span class="text-gray-500">{{ entries.length }} entries</span>
        <span class="ml-auto text-gray-600 hidden sm:inline">Press a row to expand</span>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import type { LogEntry, Session, DashboardStats, EventKind, MessageRecord, ToolUsageRecord } from '../types'
import { fetchSessions, fetchSessionMessages, fetchToolUsage, fetchStats, SSEClient } from '../api/client'

// State
const entries = ref<LogEntry[]>([])
const sessions = ref<Session[]>([])
const stats = ref<DashboardStats | null>(null)
const selectedSession = ref('')
const searchQuery = ref('')
const autoScroll = ref(true)
const loading = ref(true)
const connectionStatus = ref<'connected' | 'connecting' | 'disconnected'>('disconnected')
const terminalRef = ref<HTMLElement | null>(null)

const filterKinds: EventKind[] = ['THINKING', 'TOOL_CALL', 'TOOL_RESULT', 'RESPONSE', 'ERROR', 'SESSION_START', 'SESSION_END']
const activeFilters = ref<EventKind[]>([...filterKinds])

// SSE client
const sse = new SSEClient()
sse.onStatusChange((s) => { connectionStatus.value = s })

// Computed
const filteredEntries = computed(() => {
  let result = entries.value.filter((e) => activeFilters.value.includes(e.kind))

  if (selectedSession.value) {
    result = result.filter((e) => e.session_id === selectedSession.value)
  }

  if (searchQuery.value) {
    const q = searchQuery.value.toLowerCase()
    result = result.filter(
      (e) =>
        e.content.toLowerCase().includes(q) ||
        (e.tool_name && e.tool_name.toLowerCase().includes(q)) ||
        e.session_id.toLowerCase().includes(q),
    )
  }

  return result
})

// Helpers
function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return '??:??:??'
  }
}

function badgeClass(kind: EventKind): string {
  const map: Record<EventKind, string> = {
    THINKING: 'bg-purple-900/60 text-purple-300',
    TOOL_CALL: 'bg-blue-900/60 text-blue-300',
    TOOL_RESULT: 'bg-gray-800 text-gray-400',
    RESPONSE: 'bg-green-900/60 text-green-300',
    ERROR: 'bg-red-900/60 text-red-300',
    SESSION_START: 'bg-yellow-900/60 text-yellow-300',
    SESSION_END: 'bg-yellow-900/60 text-yellow-300',
  }
  return map[kind] || 'bg-gray-800 text-gray-400'
}

function summarizeToolInput(input: Record<string, unknown> | null | undefined): string {
  if (!input) return ''
  // Show first key's value truncated
  const keys = Object.keys(input)
  if (keys.length === 0) return ''
  const first = input[keys[0]]
  const str = typeof first === 'string' ? first : JSON.stringify(first)
  return str.length > 80 ? str.slice(0, 80) + '...' : str
}

function formatDetail(entry: LogEntry): string {
  const parts = [entry.content]
  if (entry.tool_name) parts.push(`\nTool: ${entry.tool_name}`)
  if (entry.tool_input) parts.push(`\nInput: ${JSON.stringify(entry.tool_input, null, 2)}`)
  return parts.join('')
}

function scrollToBottom() {
  if (autoScroll.value && terminalRef.value) {
    nextTick(() => {
      terminalRef.value!.scrollTop = terminalRef.value!.scrollHeight
    })
  }
}

// Convert historical data to log entries
function messagesToEntries(messages: MessageRecord[]): LogEntry[] {
  const result: LogEntry[] = []
  for (const m of messages) {
    result.push({
      id: `msg-prompt-${m.message_id}`,
      timestamp: m.timestamp,
      kind: 'SESSION_START',
      session_id: m.session_id,
      user_id: m.user_id,
      content: m.prompt.slice(0, 200),
    })
    if (m.response) {
      result.push({
        id: `msg-response-${m.message_id}`,
        timestamp: m.timestamp,
        kind: 'RESPONSE',
        session_id: m.session_id,
        user_id: m.user_id,
        content: m.response.slice(0, 500),
      })
    }
    if (m.error) {
      result.push({
        id: `msg-error-${m.message_id}`,
        timestamp: m.timestamp,
        kind: 'ERROR',
        session_id: m.session_id,
        user_id: m.user_id,
        content: m.error,
      })
    }
  }
  return result
}

function toolUsageToEntries(tools: ToolUsageRecord[]): LogEntry[] {
  return tools.map((t) => ({
    id: `tool-${t.id}`,
    timestamp: t.timestamp,
    kind: (t.success ? 'TOOL_CALL' : 'ERROR') as EventKind,
    session_id: t.session_id,
    user_id: 0,
    content: t.error_message || t.tool_name,
    tool_name: t.tool_name,
    tool_input: t.tool_input,
  }))
}

// Load historical data
async function loadHistory() {
  loading.value = true
  try {
    const [sessionsData, statsData] = await Promise.all([
      fetchSessions({ limit: 50 }),
      fetchStats(7),
    ])
    sessions.value = sessionsData
    stats.value = statsData

    // Load messages and tools for recent sessions
    const recentSessions = sessionsData.slice(0, 5)
    const allEntries: LogEntry[] = []

    await Promise.all(
      recentSessions.map(async (s) => {
        const [msgs, tools] = await Promise.all([
          fetchSessionMessages(s.session_id, 50),
          fetchToolUsage({ session_id: s.session_id, limit: 100 }),
        ])
        allEntries.push(...messagesToEntries(msgs))
        allEntries.push(...toolUsageToEntries(tools))
      }),
    )

    // Sort by timestamp and dedupe
    allEntries.sort((a, b) => a.timestamp.localeCompare(b.timestamp))
    entries.value = allEntries
  } catch (err) {
    console.error('Failed to load history:', err)
  } finally {
    loading.value = false
  }
}

// SSE handler
function handleSSEEvent(entry: LogEntry) {
  entries.value.push(entry)
  // Cap at 2000 entries
  if (entries.value.length > 2000) {
    entries.value = entries.value.slice(-1500)
  }
  scrollToBottom()
}

// Watch for new entries → auto-scroll
watch(() => entries.value.length, () => scrollToBottom())

// Lifecycle
onMounted(async () => {
  await loadHistory()
  sse.connect(handleSSEEvent)
  scrollToBottom()
})

onUnmounted(() => {
  sse.disconnect()
})
</script>
