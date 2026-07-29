"use client"

import { createElement as h, useCallback, useEffect, useRef, useState } from "react"
import { mutate as globalMutate } from "swr"
import toast from "react-hot-toast"
import { streamChat, resumeChatStream } from "../utils/sse"
import { pingman } from "../utils/pingman"
import {
  buildAttachmentsFromCalls,
  buildToolResultMap,
} from "../utils/messageAttachments"
import { shouldPlaySound, shouldShowNotification } from "../utils/preferences"
import type { SSEEvent, Message, Row, MentionItem } from "../types"

const MSG_API = "/api/gideon/v1/chat"

/** Fetch fresh data and inject into SWR cache, bypassing dedupingInterval. */
function refreshSWR(key: string) {
  pingman.get(key).then((res: any) => {
    if (res.ok) globalMutate(key, res.data, { revalidate: false })
  })
}

// ---------------------------------------------------------------------------
// Browser notifications when tab is hidden
// ---------------------------------------------------------------------------

const NOTIF_PROMPT_KEY = "gideon_notif_prompted"

function promptNotificationPermission() {
  if (typeof window === "undefined" || typeof Notification === "undefined") return
  if (Notification.permission !== "default") return
  if (localStorage.getItem(NOTIF_PROMPT_KEY) === "1") return

  localStorage.setItem(NOTIF_PROMPT_KEY, "1")

  toast(
    (t) => {
      const enable = () => {
        Notification.requestPermission()
        toast.dismiss(t.id)
      }
      const dismiss = () => toast.dismiss(t.id)

      return h("div", { style: { display: "flex", alignItems: "center", gap: "12px" } },
        h("div", { style: { flex: 1, minWidth: 0 } },
          h("div", { style: { fontWeight: 600, fontSize: "13px", color: "#fff", marginBottom: "2px" } }, "Stay in the loop"),
          h("div", { style: { fontSize: "12px", color: "rgba(255,255,255,0.5)" } }, "Get notified when Gideon finishes responding"),
        ),
        h("button", {
          onClick: enable,
          style: {
            padding: "5px 12px", borderRadius: "8px", fontSize: "12px", fontWeight: 600,
            background: "rgba(59,130,246,0.8)", color: "#fff", border: "none", cursor: "pointer",
            whiteSpace: "nowrap",
          },
        }, "Enable"),
        h("button", {
          onClick: dismiss,
          style: {
            padding: "5px 8px", borderRadius: "8px", fontSize: "12px",
            background: "transparent", color: "rgba(255,255,255,0.3)", border: "none", cursor: "pointer",
          },
        }, "Skip"),
      )
    },
    { duration: 10000, style: { background: "#1c1c1c", padding: "12px 16px" } }
  )
}

// Chime played when a response finishes while the tab is backgrounded.
// No audio file ships with the project, so this is a no-op by default. Drop your
// own file into `public/` and point NEXT_PUBLIC_NOTIFICATION_SOUND_URL at it
// (e.g. "/notification.mp3") to enable it.
const NOTIFICATION_SOUND_URL = process.env.NEXT_PUBLIC_NOTIFICATION_SOUND_URL || ""

function playNotificationSound() {
  if (!NOTIFICATION_SOUND_URL) return
  try {
    const audio = new Audio(NOTIFICATION_SOUND_URL)
    audio.volume = 0.5
    audio.play().catch(() => {})
  } catch {}
}

function showBrowserNotification(title: string) {
  if (typeof Notification === "undefined") return
  if (Notification.permission === "default") {
    Notification.requestPermission()
    return
  }
  if (Notification.permission !== "granted") return
  const n = new Notification("Gideon", {
    body: title || "Your response is ready",
    icon: "/icon.png",
    silent: true,
  })
  n.onclick = () => { window.focus(); n.close() }
}

// ---------------------------------------------------------------------------
// Module-level stream store
//
// Keyed by conversationId (or "__new__" before the server assigns one).
// Lives outside React so navigation never destroys it.
// ---------------------------------------------------------------------------

interface StreamEntry {
  rows: Row[]
  seenDbIds: Set<string>
  isStreaming: boolean
  sessionId: string
  abortController: AbortController | null
  streamAcc: string
  thinkingAcc: string
  thinkingStartTime: number
  rafId: number
  thinkingRafId: number
  // gen ties an SSE callback to the entry that created it.
  // Only the latest send() for a given conversation matches.
  gen: number
  // turn_id of the in-flight background producer (from /chat/send response
  // or a resume attach). Used by the Stop button to call POST /chat/abort.
  turnId: string | null
  // Turn IDs this entry has already attached to. Prevents the auto-attach
  // effect from re-subscribing to the same turn when `conversations` SWR
  // cache is briefly stale after the producer finishes.
  attachedTurnIds: Set<string>
  // Last `id:` value seen on the resume SSE stream. Used as
  // `Last-Event-ID` on reconnect so replay resumes from this point
  // instead of from the beginning (deduplicates events).
  lastEventId: string | null
  // Wall-clock of the last SSE activity (any bytes, including keepalives) on
  // the active stream. The stale-stream watchdog uses it to stop a spinner
  // whose connection died without a terminal `done`/`error`/`aborted` event.
  lastActivityAt: number
  // Callbacks registered by the active hook instance so it can re-render.
  listeners: Set<() => void>
}

function makeEntry(): StreamEntry {
  return {
    rows: [],
    seenDbIds: new Set(),
    isStreaming: false,
    sessionId: "",
    abortController: null,
    streamAcc: "",
    thinkingAcc: "",
    thinkingStartTime: 0,
    rafId: 0,
    thinkingRafId: 0,
    gen: 0,
    turnId: null,
    attachedTurnIds: new Set(),
    lastEventId: null,
    lastActivityAt: 0,
    listeners: new Set(),
  }
}

const streamStore = new Map<string, StreamEntry>()
const MAX_ENTRIES = 10

function getOrCreate(key: string): StreamEntry {
  let entry = streamStore.get(key)
  if (!entry) {
    entry = makeEntry()
    streamStore.set(key, entry)
    // Evict oldest non-streaming entries when over cap
    if (streamStore.size > MAX_ENTRIES) {
      for (const [k, v] of streamStore) {
        if (k === key || v.isStreaming || v.listeners.size > 0) continue
        streamStore.delete(k)
        if (streamStore.size <= MAX_ENTRIES) break
      }
    }
  }
  return entry
}

function notifyEntry(entry: StreamEntry) {
  for (const cb of entry.listeners) cb()
}

// ---------------------------------------------------------------------------
// Stale-stream watchdog (A4)
//
// A turn normally ends with a `done`/`error`/`aborted` SSE frame. If the
// connection dies mid-stream (network drop, backgrounded tab killed by the OS,
// proxy reaped the response) those frames never arrive, so `isStreaming` would
// stay true forever and the spinner hangs — the exact "looks stuck but isn't"
// reports. The server sends a `: keepalive` every ~15s during long tool runs,
// so any genuinely-alive stream refreshes `lastActivityAt` well inside this
// window; only a truly silent connection trips it.
// ---------------------------------------------------------------------------
const STREAM_SILENCE_MS = 60_000

function _finalizeStuckEntry(key: string, e: StreamEntry) {
  if (e.rafId) { cancelAnimationFrame(e.rafId); e.rafId = 0 }
  if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
  // Flush any buffered deltas onto the current row before stopping.
  const cur = e.rows.find((r) => r.id === e.sessionId)
  if (cur && e.streamAcc) { cur.content += e.streamAcc; e.streamAcc = "" }
  if (cur && e.thinkingAcc) { cur.thinking = (cur.thinking || "") + e.thinkingAcc; e.thinkingAcc = "" }
  for (const r of e.rows) {
    if (r.id.startsWith("streaming-")) { r.streaming = false; r.thinkingStreaming = false }
  }
  e.isStreaming = false
  e.turnId = null
  try { e.abortController?.abort() } catch { /* best-effort */ }
  notifyEntry(e)
  notifySidebarListeners()
  // Clear the stale active_turn_id and pull the persisted final state.
  globalMutate(
    `${MSG_API}/conversations`,
    (current: any) => (Array.isArray(current) ? current.map((c: any) => (c.id === key ? { ...c, active_turn_id: null } : c)) : current),
    { revalidate: true },
  )
  globalMutate(`${MSG_API}/conversations/${key}/messages`)
}

let _watchdogTimer: ReturnType<typeof setInterval> | null = null
function _ensureWatchdog() {
  if (_watchdogTimer !== null || typeof window === "undefined") return
  _watchdogTimer = setInterval(() => {
    const now = Date.now()
    for (const [key, e] of streamStore) {
      if (e.isStreaming && e.lastActivityAt > 0 && now - e.lastActivityAt > STREAM_SILENCE_MS) {
        _finalizeStuckEntry(key, e)
      }
    }
  }, 10_000)
}

// ---------------------------------------------------------------------------
// Sidebar streaming indicators
//
// Tracks which conversations are streaming and which just finished (unread).
// ---------------------------------------------------------------------------

const readyConversations = new Set<string>()
const sidebarListeners = new Set<() => void>()

function notifySidebarListeners() {
  for (const cb of sidebarListeners) cb()
}

/**
 * Apply a streaming UI event (`thinking_*`, `text_delta`, `tool_*`,
 * `code_image`) to the given stream entry. Caller is responsible for any
 * `gen`/abort guards before invoking this — behavior is identical whether
 * the event came from a `/chat/send` POST or a `/chat/resume/{id}` GET.
 *
 * Extracted so `handleSendMessage` and `handleAttachToTurn` don't drift.
 * Terminal events (`done`, `error`, `aborted`, `turn_started`, `busy`,
 * `conversation_created`) are still handled inline per path because
 * their side effects differ (notification sound, new-chat navigation,
 * `__new__` aliasing, SWR cache writes — none of which apply to resume).
 */
function _applyStreamingUiEvent(event: SSEEvent, e: StreamEntry) {
  e.lastActivityAt = Date.now()
  switch (event.type) {
    case "thinking_start": {
      const rowIdx = e.rows.findIndex((r) => r.id === e.sessionId)
      const row = rowIdx >= 0 ? e.rows[rowIdx] : undefined
      if (row) {
        const hasDoneTools = row.activeTools?.some((t) => t.status === "done")
        if (row.content || hasDoneTools) {
          row.streaming = false
          const newSessionId = `streaming-${Date.now()}`
          e.sessionId = newSessionId
          const newRow = {
            id: newSessionId,
            role: "assistant" as const,
            content: "",
            streaming: true,
            thinking: "",
            thinkingStreaming: true,
          }
          // Splice right after the current session row so the turn's
          // assistant rows stay contiguous even if a late-arriving DB
          // sync row landed at the tail.
          e.rows.splice(rowIdx + 1, 0, newRow)
        } else {
          row.thinking = ""
          row.thinkingStreaming = true
        }
      }
      e.thinkingStartTime = Date.now()
      notifyEntry(e)
      break
    }
    case "thinking_delta": {
      e.thinkingAcc += event.content
      if (!e.thinkingRafId) {
        e.thinkingRafId = requestAnimationFrame(() => {
          const chunk = e.thinkingAcc
          e.thinkingAcc = ""
          e.thinkingRafId = 0
          const row = e.rows.find((r) => r.id === e.sessionId)
          if (row) row.thinking = (row.thinking || "") + chunk
          notifyEntry(e)
        })
      }
      break
    }
    case "text_delta": {
      const tRow = e.rows.find((r) => r.id === e.sessionId)
      if (tRow?.thinkingStreaming) {
        tRow.thinkingStreaming = false
        tRow.thinkingDuration = e.thinkingStartTime
          ? Math.round((Date.now() - e.thinkingStartTime) / 1000)
          : undefined
        if (e.thinkingAcc) {
          tRow.thinking = (tRow.thinking || "") + e.thinkingAcc
          e.thinkingAcc = ""
        }
        if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
      }
      e.streamAcc += event.content
      if (!e.rafId) {
        e.rafId = requestAnimationFrame(() => {
          const chunk = e.streamAcc
          e.streamAcc = ""
          e.rafId = 0
          const row = e.rows.find((r) => r.id === e.sessionId)
          if (row) row.content += chunk
          notifyEntry(e)
        })
      }
      break
    }
    case "tool_start": {
      const row = e.rows.find((r) => r.id === e.sessionId)
      if (row) {
        if (row.thinkingStreaming) {
          row.thinkingStreaming = false
          row.thinkingDuration = e.thinkingStartTime
            ? Math.round((Date.now() - e.thinkingStartTime) / 1000)
            : undefined
          if (e.thinkingAcc) {
            row.thinking = (row.thinking || "") + e.thinkingAcc
            e.thinkingAcc = ""
          }
          if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
        }
        row.activeTools = [...(row.activeTools || []), { name: event.tool, status: "running" }]
      }
      notifyEntry(e)
      break
    }
    case "tool_result": {
      const row = e.rows.find((r) => r.id === e.sessionId)
      if (row?.activeTools) {
        row.activeTools = row.activeTools.map((tc) =>
          tc.name === event.tool
            ? { ...tc, status: "done", summary: event.summary, reconnect: event.reconnect, draft: event.draft, data_table: event.data_table }
            : tc
        )
      }
      if (event.draft && row) {
        const existing = row.drafts || []
        const idx = existing.findIndex((d) => d.draft_id === event.draft!.draft_id)
        if (idx >= 0) {
          const next = existing.slice()
          next[idx] = event.draft
          row.drafts = next
        } else {
          row.drafts = [...existing, event.draft]
        }
      }
      if (event.tool === "send_draft" && row) row.draftSent = true
      if (event.data_table && row) row.dataTables = [...(row.dataTables || []), event.data_table]
      if (event.chart_data && row) row.charts = [...(row.charts || []), event.chart_data]
      if (event.preview && row) row.previews = [...(row.previews || []), event.preview]
      notifyEntry(e)
      break
    }
    case "code_image": {
      const row = e.rows.find((r) => r.id === e.sessionId)
      if (row) {
        row.codeImages = [
          ...(row.codeImages || []),
          { data: event.data, mime: event.mime, filename: event.filename },
        ]
      }
      notifyEntry(e)
      break
    }
    case "code_file": {
      const row = e.rows.find((r) => r.id === e.sessionId)
      if (row) {
        row.codeFiles = [
          ...(row.codeFiles || []),
          {
            filename: event.filename,
            mime: event.mime,
            drive_url: event.drive_url,
            drive_file_id: event.drive_file_id,
            size_bytes: event.size_bytes,
          },
        ]
      }
      notifyEntry(e)
      break
    }
  }
}

/** Returns conversation IDs currently streaming (excludes "__new__"). */
export function getStreamingConversationIds(): Set<string> {
  const ids = new Set<string>()
  for (const [key, entry] of streamStore) {
    if (key !== "__new__" && entry.isStreaming) ids.add(key)
  }
  return ids
}

/** Returns conversation IDs that finished streaming but user hasn't opened yet. */
export function getReadyConversationIds(): Set<string> {
  return new Set(readyConversations)
}

/** Mark a conversation as read (user opened it). */
export function markConversationRead(id: string) {
  if (readyConversations.delete(id)) notifySidebarListeners()
}

/** Hook for sidebar to subscribe to streaming/ready state changes. */
export function useStreamingStatus() {
  const [, setTick] = useState(0)
  useEffect(() => {
    const cb = () => setTick((t) => t + 1)
    sidebarListeners.add(cb)
    return () => { sidebarListeners.delete(cb) }
  }, [])
  return {
    streamingIds: getStreamingConversationIds(),
    readyIds: getReadyConversationIds(),
  }
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

interface UseChatStreamOptions {
  authToken: string | null | undefined
  activeConversationId: string | null
  onConversationCreated: (id: string) => void
  onTitleUpdate: (conversationId: string, title: string) => void
  mutateMessages: () => void
  mutateConversations: () => void
}

export default function useChatStream({
  authToken,
  activeConversationId,
  onConversationCreated,
  onTitleUpdate,
  mutateMessages,
  mutateConversations,
}: UseChatStreamOptions) {
  // The store key for whatever conversation is currently visible.
  const storeKey = activeConversationId ?? "__new__"

  // Live ref so SSE closures see the current activeConversationId,
  // not the stale value captured when handleSendMessage was called.
  const activeConvIdRef = useRef(activeConversationId)
  activeConvIdRef.current = activeConversationId

  // Simple tick counter driven by the entry's notifyEntry() calls.
  // When the entry notifies, we bump the tick to force a re-render.
  const [renderTick, setRenderTick] = useState(0)

  useEffect(() => {
    const entry = getOrCreate(storeKey)
    const cb = () => setRenderTick((t) => t + 1)
    entry.listeners.add(cb)
    // Fire once immediately so we pick up any rows already in the entry
    cb()
    return () => { entry.listeners.delete(cb) }
  }, [storeKey])

  // Derived values read directly from the store.
  // renderTick is only used to trigger re-render; we always read fresh.
  void renderTick
  const entry = getOrCreate(storeKey)
  const rows: Row[] = [...entry.rows]
  const isStreaming = entry.isStreaming

  // Edit message ref
  const editMessageIdRef = useRef<string | null>(null)

  // ------------------------------------------------------------------
  // syncDbMessages -- merge server messages into the active entry
  // ------------------------------------------------------------------
  const syncDbMessages = useCallback(
    (dbMessages: Message[]) => {
      const e = getOrCreate(storeKey)
      let changed = false
      let pendingThinking: string | undefined
      let pendingToolCalls: { id: string; name: string; input: Record<string, any> }[] = []

      // If the entry has any stream-originated rows (streaming or finalized
      // prefix), skip all assistant DB messages. The stream rows already have
      // the correct content and there's no reliable 1:1 mapping between DB
      // messages and stream rows (frontend splits by thinking blocks, backend
      // splits by tool-call rounds). On page reload, stream rows are gone and
      // DB messages load cleanly.
      const hasStreamRows = e.rows.some(
        (r) =>
          r.role === "assistant" &&
          (r.id.startsWith("streaming-") || r.id.startsWith("finalized-"))
      )

      // Build a map of tool_use_id -> tool result so historical assistant rows
      // can re-render their data tables / charts / drafts (which are computed
      // from tool_results, not stored on the assistant row directly). The
      // map-builder and attachment walker are shared with SharedChatView
      // via ../utils/messageAttachments — keep them in lockstep.
      const toolResultMap = buildToolResultMap(dbMessages)

      for (const msg of dbMessages) {
        if (e.seenDbIds.has(msg.id)) continue
        if (msg.role !== "user" && msg.role !== "assistant") continue
        e.seenDbIds.add(msg.id)
        changed = true

        if (msg.role === "user") {
          const msgFiles = msg.files ?? undefined
          const pendingIdx = e.rows.findIndex(
            (r) => r.id === "pending-user" && r.content === (msg.content || "")
          )
          if (pendingIdx >= 0) {
            e.rows[pendingIdx].id = msg.id
            if (msgFiles) e.rows[pendingIdx].files = msgFiles
          } else {
            const userRow = { id: msg.id, role: "user" as const, content: msg.content || "", files: msgFiles }
            // If the turn's streaming / finalized assistant rows are already
            // in place (resume attached before `messages` SWR landed), the
            // user message must still render BEFORE them — splice at the
            // first stream-origin row instead of pushing to the end.
            const firstStreamIdx = e.rows.findIndex(
              (r) => r.id.startsWith("streaming-") || r.id.startsWith("finalized-")
            )
            if (firstStreamIdx >= 0) {
              e.rows.splice(firstStreamIdx, 0, userRow)
            } else {
              e.rows.push(userRow)
            }
          }
        } else {
          if (hasStreamRows) continue

          // Skip intermediate tool-call-only assistant messages, but remember
          // their tool_calls so attachments roll up onto the next visible
          // assistant row (matches streaming behavior where everything from
          // one /chat/send appears under one assistant row).
          if (msg.tool_calls?.length && !msg.content?.trim()) {
            if (msg.thinking) pendingThinking = msg.thinking
            pendingToolCalls.push(...msg.tool_calls)
            continue
          }

          const allCalls = [...pendingToolCalls, ...(msg.tool_calls || [])]
          const attachments = buildAttachmentsFromCalls(allCalls, toolResultMap)

          // `toolCalls` is what ChatMessages reads to render "tool done"
          // indicator badges. It MUST be `allCalls`, not `msg.tool_calls`
          // — otherwise the badges for tools that ran on intermediate
          // tool-only assistant messages (whose tool_calls were rolled
          // up into pendingToolCalls) silently disappear from history.
          e.rows.push({
            id: msg.id,
            role: "assistant",
            content: msg.content || "",
            thinking: msg.thinking || pendingThinking || undefined,
            toolCalls: allCalls.length ? allCalls : undefined,
            ...attachments,
          })
          pendingThinking = undefined
          pendingToolCalls = []
        }
      }

      if (changed) notifyEntry(e)
    },
    [storeKey]
  )

  // ------------------------------------------------------------------
  // resetChat -- clears only the "__new__" slot.
  //
  // This is the right scope: the only time we truly need a blank slate is
  // when the user starts a new conversation (storeKey will become "__new__"
  // after activeConversationId → null). Existing conversation entries are
  // never wiped here -- their streams continue in the background uninterrupted.
  // ------------------------------------------------------------------
  const resetChat = useCallback(() => {
    const fresh = makeEntry()
    streamStore.set("__new__", fresh)
    // Notify only if we're currently viewing the new-chat slot
    if (storeKey === "__new__") notifyEntry(fresh)
  }, [storeKey])

  // ------------------------------------------------------------------
  // clearConversation -- wipe a specific conversation's streamStore entry.
  // Used after deleting a conversation so its leftover rows don't bleed into
  // the next view (e.g. the empty/home state above its last message).
  // ------------------------------------------------------------------
  const clearConversation = useCallback((convId: string) => {
    const entry = streamStore.get(convId)
    if (entry?.abortController) {
      try { entry.abortController.abort() } catch { /* noop */ }
    }
    if (entry?.rafId) cancelAnimationFrame(entry.rafId)
    if (entry?.thinkingRafId) cancelAnimationFrame(entry.thinkingRafId)
    streamStore.delete(convId)
    readyConversations.delete(convId)
    notifySidebarListeners()
  }, [])

  // ------------------------------------------------------------------
  // resumeStream -- called when switching to a conversation.
  // If there is already a live/completed entry for that conversation,
  // the hook picks it up automatically via useSyncExternalStore.
  // We only need to force-refresh SWR so DB messages merge in.
  // ------------------------------------------------------------------
  const resumeStream = useCallback((convId: string | null) => {
    if (!convId) return
    // Always force a fresh message fetch so DB rows merge in
    refreshSWR(`${MSG_API}/conversations/${convId}/messages`)
    // The store entry (if any) is already there -- useSyncExternalStore will
    // subscribe to it on the next render when activeConversationId changes.
  }, [])

  // ------------------------------------------------------------------
  // handleSendMessage
  // ------------------------------------------------------------------
  const handleSendMessage = useCallback(
    async (
      message: string,
      mentions?: MentionItem[],
      options?: {
        resume?: boolean
        conversationId?: string
        newConversation?: boolean
        fileIds?: string[]
        files?: { filename: string; ext: string }[]
        autoMode?: boolean
      }
    ) => {
      if (!authToken) return

      const isResume = options?.resume || false
      const targetConversationId = options?.newConversation
        ? undefined
        : options?.conversationId || activeConversationId

      const sendKey = targetConversationId ?? "__new__"
      const e = getOrCreate(sendKey)

      // Edit mode: truncate history from the edited message
      const editMsgId = editMessageIdRef.current
      if (editMsgId) {
        editMessageIdRef.current = null
        const editIdx = e.rows.findIndex((r) => r.id === editMsgId)
        if (editIdx >= 0) {
          for (let i = editIdx; i < e.rows.length; i++) {
            const rowId = e.rows[i].id
            if (!rowId.startsWith("pending-") && !rowId.startsWith("streaming-") && !rowId.startsWith("finalized-")) {
              e.seenDbIds.delete(rowId)
            }
          }
          e.rows.splice(editIdx)
        }
        if (targetConversationId && editMsgId && !editMsgId.startsWith("pending-")) {
          pingman.delete(`${MSG_API}/conversations/${targetConversationId}/messages-from/${editMsgId}`).catch(() => {})
        }
      }

      // Bump generation so stale SSE callbacks from a previous send() are silenced
      // for UI-only events. Core events (done, conversation_created) always run.
      e.gen++
      const gen = e.gen

      // Cancel previous abort controller (stop old stream for this conversation)
      if (e.abortController) e.abortController.abort()
      const controller = new AbortController()
      e.abortController = controller

      // Cancel pending RAFs
      if (e.rafId) { cancelAnimationFrame(e.rafId); e.rafId = 0 }
      if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
      e.streamAcc = ""
      e.thinkingAcc = ""
      e.thinkingStartTime = 0

      // Finalize any existing streaming rows from a previous exchange
      for (const r of e.rows) {
        if (r.id.startsWith("streaming-")) {
          r.id = `finalized-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
          r.streaming = false
          r.activeTools = undefined
        }
      }

      // Append user message immediately
      if (!isResume) {
        e.rows.push({ id: "pending-user", role: "user", content: message, files: options?.files })
      }

      // Create assistant streaming row
      const sessionId = `streaming-${Date.now()}`
      e.sessionId = sessionId
      e.rows.push({
        id: sessionId,
        role: "assistant",
        content: "",
        streaming: true,
      })

      e.isStreaming = true
      e.lastActivityAt = Date.now()
      _ensureWatchdog()
      notifyEntry(e)
      notifySidebarListeners()

      // Show notification permission prompt once (no-op if already granted/denied/prompted)
      promptNotificationPermission()

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"

      streamChat(
        `${apiUrl}${MSG_API}/send`,
        {
          message: isResume ? "" : message,
          conversation_id: targetConversationId || undefined,
          mentions,
          resume: isResume || undefined,
          file_ids: options?.fileIds,
          auto_mode: options?.autoMode || undefined,
        },
        authToken,
        (event: SSEEvent) => {
          if (controller.signal.aborted) return

          // ----------------------------------------------------------------
          // Events that always run (even for background streams)
          // ----------------------------------------------------------------

          if (event.type === "turn_started") {
            e.turnId = event.turn_id
            // Mark this turn as "handled" in this entry so the auto-attach
            // effect in Layout doesn't re-subscribe to the same turn after
            // it finishes locally (SWR cache briefly holds stale value).
            e.attachedTurnIds.add(event.turn_id)
            notifySidebarListeners()
            return
          }

          if (event.type === "busy") {
            // Server says a different turn is already running on this
            // conversation — surface the turn_id so the caller can attach.
            e.turnId = event.turn_id
            e.isStreaming = false
            notifyEntry(e)
            notifySidebarListeners()
            return
          }

          if (event.type === "aborted") {
            if (e.gen !== gen) return
            if (e.rafId) { cancelAnimationFrame(e.rafId); e.rafId = 0 }
            if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
            if (e.streamAcc) {
              const row = e.rows.find((r) => r.id === e.sessionId)
              if (row) row.content += e.streamAcc
              e.streamAcc = ""
            }
            if (e.thinkingAcc) {
              const row = e.rows.find((r) => r.id === e.sessionId)
              if (row) row.thinking = (row.thinking || "") + e.thinkingAcc
              e.thinkingAcc = ""
            }
            const row = e.rows.find((r) => r.id === e.sessionId)
            if (row) {
              row.streaming = false
              row.thinkingStreaming = false
            }
            e.isStreaming = false
            e.turnId = null
            notifyEntry(e)
            notifySidebarListeners()
            return
          }

          if (event.type === "conversation_created") {
            if (event.conversation_id) {
              const convId = event.conversation_id

              // Add the real conversation ID as a second key pointing to the
              // same entry object. Keep "__new__" alive so the hook (whose
              // storeKey is still "__new__" until the state update processes)
              // continues to see updates. Both keys share the same object,
              // same listeners, same rows.
              const newEntry = streamStore.get("__new__")
              if (newEntry && newEntry.gen === gen) {
                streamStore.set(convId, newEntry)
                // "__new__" is NOT deleted -- it stays until resetChat()
              }

              // Optimistically inject into sidebar cache
              const convTitle = event.title || null
              globalMutate(
                `${MSG_API}/conversations`,
                (current: any) => {
                  if (!current) return current
                  if (current.some((c: any) => c.id === convId)) return current
                  return [
                    { id: convId, title: convTitle, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), is_pinned: false },
                    ...current,
                  ]
                },
                { revalidate: true }
              )

              // Only navigate if the user is still on the new-chat screen
              // AND our entry hasn't been replaced by resetChat or another send.
              if (activeConvIdRef.current === null && streamStore.get("__new__") === e) {
                onConversationCreated(convId)
              }
              if (event.title) {
                onTitleUpdate(convId, event.title)
              }
            }
            return
          }

          if (event.type === "done") {
            const completedConvId = event.conversation_id || targetConversationId
            if (event.title && event.conversation_id) {
              onTitleUpdate(event.conversation_id, event.title)
            }
            // Force sidebar refresh
            refreshSWR(`${MSG_API}/conversations`)

            // Resolve which entry to finalize (may have been re-keyed)
            const doneEntry = completedConvId ? streamStore.get(completedConvId) : e
            if (doneEntry && doneEntry.gen === gen) {
              // Flush remaining accumulators
              if (doneEntry.rafId) { cancelAnimationFrame(doneEntry.rafId); doneEntry.rafId = 0 }
              if (doneEntry.streamAcc) {
                const row = doneEntry.rows.find((r) => r.id === doneEntry.sessionId)
                if (row) row.content += doneEntry.streamAcc
                doneEntry.streamAcc = ""
              }
              if (doneEntry.thinkingRafId) { cancelAnimationFrame(doneEntry.thinkingRafId); doneEntry.thinkingRafId = 0 }
              if (doneEntry.thinkingAcc) {
                const tRow = doneEntry.rows.find((r) => r.id === doneEntry.sessionId)
                if (tRow) tRow.thinking = (tRow.thinking || "") + doneEntry.thinkingAcc
                doneEntry.thinkingAcc = ""
              }

              // Defensive: clear in-progress flags on every streaming-origin
              // row in this entry, not just the sessionId one. A dangling
              // thinkingStreaming=true would render a stuck "Thinking…"
              // spinner after the turn finished.
              for (const r of doneEntry.rows) {
                if (r.id.startsWith("streaming-")) {
                  r.streaming = false
                  if (r.thinkingStreaming) {
                    r.thinkingStreaming = false
                    r.thinkingDuration = doneEntry.thinkingStartTime
                      ? Math.round((Date.now() - doneEntry.thinkingStartTime) / 1000)
                      : r.thinkingDuration
                  }
                }
              }

              doneEntry.isStreaming = false
              doneEntry.turnId = null
              notifyEntry(doneEntry)
            }

            // Mark as "ready" if user is viewing a different conversation
            if (completedConvId && completedConvId !== activeConvIdRef.current) {
              readyConversations.add(completedConvId)
            }
            notifySidebarListeners()

            // Sound + browser notification — gated by user preferences
            if (shouldPlaySound()) {
              playNotificationSound()
            }
            if (shouldShowNotification()) {
              showBrowserNotification(event.title || "Your response is ready")
            }

            // Refresh messages for background-completed conversations
            if (completedConvId && completedConvId !== activeConvIdRef.current) {
              refreshSWR(`${MSG_API}/conversations/${completedConvId}/messages`)
            }

            // Foreground: also trigger SWR mutate
            if (completedConvId === activeConvIdRef.current || e.gen === gen) {
              mutateMessages()
            }
            return
          }

          if (event.type === "error") {
            if (e.gen !== gen) return
            if (e.rafId) { cancelAnimationFrame(e.rafId); e.rafId = 0 }
            e.streamAcc = ""
            if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
            e.thinkingAcc = ""
            for (const r of e.rows) {
              if (r.id.startsWith("streaming-")) {
                r.streaming = false
                r.thinkingStreaming = false
              }
            }
            const row = e.rows.find((r) => r.id === e.sessionId)
            if (row) {
              // BE-emitted error codes can carry a specific message; render it
              // (markdown link friendly) instead of the generic fallback.
              // `provider_overloaded` is the canonical "Anthropic is having a bad
              // day" signal — append a status-page link so the user can verify.
              let msg = (event as { message?: string }).message || "Sorry, something went wrong."
              const code = (event as { code?: string }).code
              if (code === "provider_overloaded") {
                msg = `${msg} See [status.claude.com](https://status.claude.com).`
              }
              row.content = row.content || msg
            }
            e.isStreaming = false
            e.turnId = null
            notifyEntry(e)
            notifySidebarListeners()
            return
          }

          // ----------------------------------------------------------------
          // UI events: accumulate into the entry regardless of whether this
          // conversation is currently active. The user will see them when
          // they switch back.
          // ----------------------------------------------------------------
          if (e.gen !== gen) return

          _applyStreamingUiEvent(event, e)
        },
        () => {
          if (controller.signal.aborted) return
          if (e.gen !== gen) return
          if (e.rafId) { cancelAnimationFrame(e.rafId); e.rafId = 0 }
          e.streamAcc = ""
          if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
          e.thinkingAcc = ""
          e.isStreaming = false
          notifyEntry(e)
          notifySidebarListeners()
        },
        controller.signal,
        () => { e.lastActivityAt = Date.now() },
      )
    },
    [authToken, activeConversationId, mutateMessages, onConversationCreated, onTitleUpdate]
  )

  // Edit message -- truncate from this message and resend with new text
  const handleEditMessage = useCallback(
    (messageId: string, newText: string) => {
      editMessageIdRef.current = messageId
      handleSendMessage(newText)
    },
    [handleSendMessage]
  )

  // ------------------------------------------------------------------
  // handleAttachToTurn -- attach to an in-flight background turn via
  // GET /chat/resume/{turn_id}. Used when the user reopens a
  // conversation whose producer is still generating (page refresh /
  // cross-device / multi-tab).
  // ------------------------------------------------------------------
  const handleAttachToTurn = useCallback(
    (turnId: string, convId: string) => {
      if (!authToken) return
      const e = getOrCreate(convId)
      // Guard 1 — re-attach: once we've attached to a turn (live or
      // already-finished), never attach again. The conversations SWR cache
      // can briefly hold a stale active_turn_id after the producer
      // finishes, which would otherwise loop us right back here.
      if (e.attachedTurnIds.has(turnId)) return
      // Guard 2 — already live in this tab.
      if (e.isStreaming && e.turnId === turnId) return
      e.attachedTurnIds.add(turnId)

      e.gen++
      const gen = e.gen
      e.turnId = turnId

      if (e.abortController) e.abortController.abort()
      const controller = new AbortController()
      e.abortController = controller

      if (e.rafId) { cancelAnimationFrame(e.rafId); e.rafId = 0 }
      if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
      e.streamAcc = ""
      e.thinkingAcc = ""
      e.thinkingStartTime = 0

      // Finalize any prior streaming rows so the resume accumulator writes
      // into a clean row.
      for (const r of e.rows) {
        if (r.id.startsWith("streaming-")) {
          r.id = `finalized-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
          r.streaming = false
          r.activeTools = undefined
        }
      }

      // Purge trailing assistant rows that came from an earlier DB sync.
      // They belong to this in-flight turn and will be replayed in full
      // from the Redis stream — keeping the DB copy on screen would double
      // the response. Stop at the last user message so prior turns stay.
      while (e.rows.length > 0) {
        const last = e.rows[e.rows.length - 1]
        if (last.role !== "assistant") break
        if (last.id.startsWith("finalized-") || last.id.startsWith("streaming-")) break
        e.rows.pop()
        if (last.id && !last.id.startsWith("pending-")) e.seenDbIds.delete(last.id)
      }

      const sessionId = `streaming-${Date.now()}`
      e.sessionId = sessionId
      e.rows.push({ id: sessionId, role: "assistant", content: "", streaming: true })
      e.isStreaming = true
      e.lastActivityAt = Date.now()
      _ensureWatchdog()
      notifyEntry(e)
      notifySidebarListeners()

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"

      resumeChatStream(
        `${apiUrl}${MSG_API}/resume/${turnId}`,
        authToken,
        (event: SSEEvent) => {
          if (controller.signal.aborted) return

          if (event.type === "turn_started" || event.type === "busy") {
            e.turnId = event.turn_id
            return
          }
          if (event.type === "conversation_created") {
            // Replayed from a fresh turn — we already know the conv, nothing to do.
            return
          }
          if (event.type === "done") {
            // State mutations are gated on gen so a late `done` from a
            // prior attach can't null out a newer stream's isStreaming /
            // turnId when the user triggers another send/attach on this
            // conv before the old resume finishes. Side effects
            // (SWR refresh, title) are fine to fire regardless.
            if (e.gen === gen) {
              if (e.rafId) { cancelAnimationFrame(e.rafId); e.rafId = 0 }
              if (e.streamAcc) {
                const row = e.rows.find((r) => r.id === e.sessionId)
                if (row) row.content += e.streamAcc
                e.streamAcc = ""
              }
              if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
              if (e.thinkingAcc) {
                const row = e.rows.find((r) => r.id === e.sessionId)
                if (row) row.thinking = (row.thinking || "") + e.thinkingAcc
                e.thinkingAcc = ""
              }
              // Defensive: clear every row's in-progress flags. Resume
              // replays can interleave thinking_start/text_delta/tool_*
              // events across multiple rows, and a single lingering
              // `thinkingStreaming=true` would keep the "Thinking…"
              // spinner rendered after the turn finishes.
              for (const r of e.rows) {
                if (r.id.startsWith("streaming-")) {
                  r.streaming = false
                  if (r.thinkingStreaming) {
                    r.thinkingStreaming = false
                    r.thinkingDuration = e.thinkingStartTime
                      ? Math.round((Date.now() - e.thinkingStartTime) / 1000)
                      : r.thinkingDuration
                  }
                }
              }
              e.isStreaming = false
              e.turnId = null
              notifyEntry(e)
            }
            // Optimistically clear active_turn_id in the conversations SWR
            // cache so the auto-attach effect doesn't re-fire on a stale
            // cache entry before the refresh lands.
            globalMutate(
              `${MSG_API}/conversations`,
              (current: any) => {
                if (!current) return current
                return current.map((c: any) => (c.id === convId ? { ...c, active_turn_id: null } : c))
              },
              { revalidate: true },
            )
            refreshSWR(`${MSG_API}/conversations/${convId}/messages`)
            mutateMessages()
            if (event.title) onTitleUpdate(convId, event.title)
            notifySidebarListeners()
            return
          }
          if (event.type === "error" || event.type === "aborted") {
            if (e.gen === gen) {
              if (e.rafId) { cancelAnimationFrame(e.rafId); e.rafId = 0 }
              if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }
              if (e.streamAcc) {
                const row = e.rows.find((r) => r.id === e.sessionId)
                if (row) row.content += e.streamAcc
                e.streamAcc = ""
              }
              if (e.thinkingAcc) {
                const row = e.rows.find((r) => r.id === e.sessionId)
                if (row) row.thinking = (row.thinking || "") + e.thinkingAcc
                e.thinkingAcc = ""
              }
              for (const r of e.rows) {
                if (r.id.startsWith("streaming-")) {
                  r.streaming = false
                  r.thinkingStreaming = false
                }
              }
              // Mirror the send-path error handling: surface the BE-emitted
              // message (and the provider_overloaded status link) on the
              // resumed row when nothing else streamed in. Skipped for
              // `aborted` so user-initiated stops stay silent.
              if (event.type === "error") {
                const row = e.rows.find((r) => r.id === e.sessionId)
                if (row) {
                  let msg = (event as { message?: string }).message || "Sorry, something went wrong."
                  const code = (event as { code?: string }).code
                  if (code === "provider_overloaded") {
                    msg = `${msg} See [status.claude.com](https://status.claude.com).`
                  }
                  row.content = row.content || msg
                }
              }
              e.isStreaming = false
              e.turnId = null
              notifyEntry(e)
            }
            globalMutate(
              `${MSG_API}/conversations`,
              (current: any) => {
                if (!current) return current
                return current.map((c: any) => (c.id === convId ? { ...c, active_turn_id: null } : c))
              },
              { revalidate: true },
            )
            notifySidebarListeners()
            refreshSWR(`${MSG_API}/conversations/${convId}/messages`)
            return
          }

          if (e.gen !== gen) return

          _applyStreamingUiEvent(event, e)
        },
        () => {
          if (controller.signal.aborted) return
          if (e.gen !== gen) return
          e.isStreaming = false
          e.turnId = null
          notifyEntry(e)
          notifySidebarListeners()
        },
        controller.signal,
        e.lastEventId || undefined,
        (id: string) => {
          // Track the most recent Redis stream entry id so if this tail
          // is ever restarted (e.g. future auto-reconnect on transient
          // disconnect), we can resume from here instead of replaying
          // from the start.
          if (e.gen === gen) e.lastEventId = id
        },
        () => { e.lastActivityAt = Date.now() },
      ).then((result) => {
        if (result === "not-found") {
          // Turn expired from Redis — DB has the final state. Re-fetch +
          // clear stale active_turn_id so the effect doesn't re-attach.
          // State mutation gated on gen so a newer send/attach started
          // while this resume was inflight isn't clobbered.
          if (e.gen === gen) {
            e.isStreaming = false
            e.turnId = null
            // Drop the streaming placeholder so DB sync can repopulate
            // the assistant row cleanly (hasStreamRows would otherwise
            // suppress it).
            const idx = e.rows.findIndex((r) => r.id === e.sessionId)
            if (idx >= 0) e.rows.splice(idx, 1)
            notifyEntry(e)
          }
          globalMutate(
            `${MSG_API}/conversations`,
            (current: any) => {
              if (!current) return current
              return current.map((c: any) => (c.id === convId ? { ...c, active_turn_id: null } : c))
            },
            { revalidate: true },
          )
          refreshSWR(`${MSG_API}/conversations/${convId}/messages`)
          // Sidebar streaming indicator was pulsing for this conv; clear it.
          notifySidebarListeners()
        }
      })
    },
    [authToken, mutateMessages, onTitleUpdate],
  )

  // Stop streaming -- tells the backend producer to wind down (so the
  // Claude loop stops in the background, not just on this client) and
  // closes the local tail. The producer will save any partial output,
  // emit an `aborted` sentinel, and every tailer on this turn closes.
  const handleStop = useCallback(() => {
    const e = streamStore.get(storeKey)
    if (!e) return

    // Fire-and-forget abort — rely on the sentinel to clean up UI state.
    // Clear turnId locally so a rapid second Stop doesn't fire a redundant
    // POST. The server-side abort flag survives independently.
    const abortingTurnId = e.turnId
    e.turnId = null
    if (abortingTurnId) {
      pingman.post(`${MSG_API}/abort/${abortingTurnId}`).catch((err) => {
        // eslint-disable-next-line no-console
        console.warn("chat abort POST failed", { turnId: abortingTurnId, err })
      })
    }

    if (e.abortController) e.abortController.abort()
    e.abortController = null
    if (e.rafId) { cancelAnimationFrame(e.rafId); e.rafId = 0 }
    if (e.thinkingRafId) { cancelAnimationFrame(e.thinkingRafId); e.thinkingRafId = 0 }

    const streamRow = [...e.rows].reverse().find(
      (r) => r.id.startsWith("streaming-") && r.streaming
    ) ?? e.rows.find((r) => r.id.startsWith("streaming-"))

    if (e.streamAcc) {
      if (streamRow) streamRow.content += e.streamAcc
      e.streamAcc = ""
    }
    if (e.thinkingAcc) {
      if (streamRow) streamRow.thinking = (streamRow.thinking || "") + e.thinkingAcc
      e.thinkingAcc = ""
    }
    if (streamRow) {
      streamRow.streaming = false
      streamRow.thinkingStreaming = false
    }
    e.isStreaming = false
    notifyEntry(e)
    notifySidebarListeners()
  }, [storeKey])

  return {
    rows,
    isStreaming,
    syncDbMessages,
    resetChat,
    clearConversation,
    resumeStream,
    handleSendMessage,
    handleEditMessage,
    handleAttachToTurn,
    handleStop,
  }
}
