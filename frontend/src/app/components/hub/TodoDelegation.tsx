"use client"

import { useState, useRef, useEffect } from "react"
import { createPortal } from "react-dom"
import {
  X,
  Loader2,
  Send,
  UserPlus,
  MessageCircle,
} from "lucide-react"
import { pingman } from "../../utils/pingman"
import type { Delegatee, TodoUpdateEntry } from "../../types"

// ---------------------------------------------------------------------------
// DelegateModal
// ---------------------------------------------------------------------------

export type ReminderCadence = "never" | "daily" | "every_2_days" | "weekly"

const CADENCE_OPTIONS: { value: ReminderCadence; label: string }[] = [
  { value: "never", label: "Never" },
  { value: "daily", label: "Daily" },
  { value: "every_2_days", label: "Every 2 days" },
  { value: "weekly", label: "Weekly" },
]

type FollowUpPreset = "none" | "1h" | "4h" | "tomorrow" | "3d" | "custom"

const FOLLOW_UP_OPTIONS: { value: FollowUpPreset; label: string }[] = [
  { value: "none", label: "No follow-up" },
  { value: "1h", label: "In 1 hour" },
  { value: "4h", label: "In 4 hours" },
  { value: "tomorrow", label: "Tomorrow 10am" },
  { value: "3d", label: "In 3 days" },
  { value: "custom", label: "Pick time…" },
]

function computeFollowUpAt(preset: FollowUpPreset, customISO: string): string | undefined {
  const now = new Date()
  switch (preset) {
    case "1h": return new Date(now.getTime() + 60 * 60 * 1000).toISOString()
    case "4h": return new Date(now.getTime() + 4 * 60 * 60 * 1000).toISOString()
    case "tomorrow": {
      const d = new Date(now)
      d.setDate(d.getDate() + 1)
      d.setHours(10, 0, 0, 0)
      return d.toISOString()
    }
    case "3d": return new Date(now.getTime() + 3 * 24 * 60 * 60 * 1000).toISOString()
    case "custom": return customISO ? new Date(customISO).toISOString() : undefined
    default: return undefined
  }
}

export function DelegateModal({
  onDelegate,
  onClose,
  originRect,
  existingDelegatees,
  existingCadence,
}: {
  onDelegate: (delegatees: Delegatee[], message?: string, reminderCadence?: ReminderCadence, followUpAt?: string) => void
  onClose: () => void
  originRect?: DOMRect | null
  // When set, the modal opens in REASSIGN mode: chips pre-filled, cadence
  // pre-selected, header + button copy reflect "Reassign". Submit then routes
  // through the existing delegate endpoint (REPLACE semantics on the BE).
  existingDelegatees?: Delegatee[]
  existingCadence?: ReminderCadence | null
}) {
  // Snapshot reassign-mode inputs at MOUNT and never read the live props
  // again. The parent re-renders on every SWR refetch, so reading
  // `existingDelegatees` after mount can compare the user's working set
  // against a moving target — silently producing a REPLACE that drops
  // someone added in another tab. We freeze identity here and rely on the
  // upstream `key={delegatingTodoId}` to remount when the user opens the
  // modal for a different todo.
  const initialDelegateesRef = useRef<Delegatee[]>(existingDelegatees ?? [])
  const initialCadenceRef = useRef<ReminderCadence>(existingCadence ?? "never")
  const isReassign = initialDelegateesRef.current.length > 0
  const ref = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState("")
  const [selected, setSelected] = useState<Delegatee[]>(initialDelegateesRef.current)
  const [message, setMessage] = useState("")
  const [cadence, setCadence] = useState<ReminderCadence>(initialCadenceRef.current)
  const [followUpPreset, setFollowUpPreset] = useState<FollowUpPreset>("none")
  const [followUpCustom, setFollowUpCustom] = useState("")
  const [results, setResults] = useState<Delegatee[]>([])
  const [searching, setSearching] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [phase, setPhase] = useState<"enter" | "open" | "exit">("enter")

  // Detect whether the user actually changed the delegatee set so we can
  // gate submission. Reassigning to the EXACT same set with no cadence
  // / message edit is a no-op the BE special-cases — but disabling the
  // button is the clearer affordance. Comparisons use the mount-time
  // snapshot, not the live props (see ref comment above).
  const delegateesUnchanged = isReassign
    && selected.length === initialDelegateesRef.current.length
    && selected.every((s) => initialDelegateesRef.current.some((e) => e.email.toLowerCase() === s.email.toLowerCase()))
  const cadenceUnchanged = initialCadenceRef.current === cadence
  const nothingChanged = isReassign && delegateesUnchanged && cadenceUnchanged && !message.trim() && followUpPreset === "none"

  // Animate in
  useEffect(() => {
    requestAnimationFrame(() => setPhase("open"))
  }, [])

  const handleClose = () => {
    setPhase("exit")
    setTimeout(onClose, 250)
  }

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) handleClose()
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [])

  useEffect(() => {
    if (!query.trim()) { setResults([]); return }
    const timer = setTimeout(async () => {
      setSearching(true)
      try {
        const res = await pingman.get<{ items: { id: string; label: string; subtitle: string; avatar_url?: string }[] }>(
          `/api/gideon/v1/mentions/search?category=collaborator&query=${encodeURIComponent(query)}`
        )
        if (res.ok) {
          setResults(
            res.data.items
              .filter((item) => !selected.some((s) => s.email === item.subtitle))
              .map((item) => ({ name: item.label, email: item.subtitle, avatar_url: item.avatar_url }))
          )
        }
      } catch { /* noop */ }
      setSearching(false)
    }, 200)
    return () => clearTimeout(timer)
  }, [query, selected])

  const handleSubmit = async () => {
    if (selected.length === 0) return
    setSubmitting(true)
    try {
      const followUpAt = computeFollowUpAt(followUpPreset, followUpCustom)
      await onDelegate(selected, message.trim() || undefined, cadence, followUpAt)
    } catch {
      /* onDelegate handles error display */
    } finally {
      setSubmitting(false)
    }
  }

  // Compute start/end positions for the fly animation. Clamp width to viewport on mobile.
  const viewportW = typeof window !== "undefined" ? window.innerWidth : 800
  const modalW = Math.min(400, viewportW - 24)
  const modalH = 420 // approximate
  const centerX = (typeof window !== "undefined" ? window.innerWidth : 800) / 2 - modalW / 2
  const centerY = (typeof window !== "undefined" ? window.innerHeight : 600) / 2 - modalH / 2

  const startX = originRect ? originRect.left + originRect.width / 2 - modalW / 2 : centerX
  const startY = originRect ? originRect.top + originRect.height / 2 - modalH / 2 : centerY

  const isOpen = phase === "open"
  const isExit = phase === "exit"

  const posX = isOpen ? centerX : startX
  const posY = isOpen ? centerY : startY
  const scale = isOpen ? 1 : isExit ? 0.85 : 0.4

  return createPortal(
    <div
      className="fixed inset-0 z-[9999]"
      style={{
        background: isOpen ? "rgba(0,0,0,0.4)" : "rgba(0,0,0,0)",
        transition: "background 250ms ease-out",
      }}
    >
      <div
        ref={ref}
        className="absolute rounded-xl shadow-2xl p-5 flex flex-col gap-3"
        style={{
          width: modalW,
          maxHeight: 500,
          background: "rgba(8,8,8,0.72)",
          backdropFilter: "blur(48px) saturate(1.8)",
          WebkitBackdropFilter: "blur(48px) saturate(1.8)",
          border: "1px solid rgba(255,255,255,0.06)",
          boxShadow: "0 12px 40px rgba(0,0,0,0.5), inset 0 0.5px 0 rgba(255,255,255,0.05)",
          left: posX,
          top: posY,
          transform: `scale(${scale})`,
          opacity: isOpen ? 1 : 0,
          transition: "left 300ms cubic-bezier(0.34,1.56,0.64,1), top 300ms cubic-bezier(0.34,1.56,0.64,1), transform 300ms cubic-bezier(0.34,1.56,0.64,1), opacity 200ms ease-out",
        }}
      >
        <div className="flex items-center justify-between">
          <h3 className="text-[14px] font-medium" style={{ color: "rgba(255,255,255,0.9)" }}>
            {isReassign ? "Reassign" : "Delegate to"}
          </h3>
          <button onClick={handleClose} className="w-6 h-6 flex items-center justify-center rounded hover:bg-white/10">
            <X size={14} style={{ color: "rgba(255,255,255,0.6)" }} />
          </button>
        </div>
        {isReassign && (
          <p className="text-[11px] -mt-1" style={{ color: "rgba(255,255,255,0.5)" }}>
            Add or remove people. We&apos;ll open a fresh Slack thread for the new group and let the old one know it&apos;s been reassigned.
          </p>
        )}

        {/* Selected chips */}
        {selected.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {selected.map((d) => (
              <span
                key={d.email}
                className="inline-flex items-center gap-1 pl-1 pr-2 py-0.5 rounded-full text-[11px]"
                style={{ background: "rgba(59,130,246,0.2)", color: "rgba(59,130,246,0.9)", border: "1px solid rgba(59,130,246,0.3)" }}
              >
                {d.avatar_url ? (
                  <img src={d.avatar_url} alt="" className="w-4 h-4 rounded-full object-cover" />
                ) : null}
                {d.name}
                <button onClick={() => setSelected((prev) => prev.filter((s) => s.email !== d.email))}>
                  <X size={10} />
                </button>
              </span>
            ))}
          </div>
        )}

        {/* Search input */}
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by name or email..."
          className="w-full px-3 py-2 rounded-lg bg-white/5 text-[13px] outline-none placeholder:text-white/30"
          style={{ color: "rgba(255,255,255,0.9)", border: "1px solid rgba(255,255,255,0.1)" }}
        />

        {/* Results */}
        {results.length > 0 && (
          <div className="flex flex-col max-h-[150px] overflow-y-auto" style={{ scrollbarWidth: "thin" }}>
            {results.map((r) => (
              <button
                key={r.email}
                onClick={() => { setSelected((prev) => [...prev, r]); setQuery(""); setResults([]) }}
                className="flex items-center gap-2 px-3 py-2 text-left hover:bg-white/10 rounded-md transition-colors"
              >
                {r.avatar_url ? (
                  <img src={r.avatar_url} alt="" className="w-6 h-6 rounded-full shrink-0 object-cover" />
                ) : (
                  <div className="w-6 h-6 rounded-full shrink-0 flex items-center justify-center" style={{ background: "rgba(255,255,255,0.1)" }}>
                    <span className="text-[9px] font-semibold" style={{ color: "rgba(255,255,255,0.6)" }}>
                      {r.name?.trim().split(/\s+/).filter(Boolean).map(w => w[0]).join("").slice(0, 2).toUpperCase() || "?"}
                    </span>
                  </div>
                )}
                <div>
                  <div className="text-[12px]" style={{ color: "rgba(255,255,255,0.9)" }}>{r.name}</div>
                  <div className="text-[10px]" style={{ color: "rgba(255,255,255,0.4)" }}>{r.email}</div>
                </div>
              </button>
            ))}
          </div>
        )}

        {searching && (
          <div className="flex items-center gap-2 px-3 py-2">
            <Loader2 size={12} className="animate-spin" style={{ color: "rgba(255,255,255,0.4)" }} />
            <span className="text-[11px]" style={{ color: "rgba(255,255,255,0.4)" }}>Searching...</span>
          </div>
        )}

        {/* Message */}
        <textarea
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder="Add a message (optional)..."
          rows={2}
          className="w-full px-3 py-2 rounded-lg bg-white/5 text-[12px] outline-none resize-none placeholder:text-white/30"
          style={{ color: "rgba(255,255,255,0.9)", border: "1px solid rgba(255,255,255,0.1)" }}
        />

        {/* First follow-up time */}
        <div className="flex flex-col gap-1.5">
          <span className="text-[11px]" style={{ color: "rgba(255,255,255,0.55)" }}>
            Follow up
          </span>
          <div className="flex flex-wrap gap-1.5">
            {FOLLOW_UP_OPTIONS.map((opt) => {
              const active = followUpPreset === opt.value
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setFollowUpPreset(opt.value)}
                  className="px-2.5 py-1 rounded-full text-[11px] transition-colors"
                  style={{
                    background: active ? "rgba(59,130,246,0.25)" : "rgba(255,255,255,0.05)",
                    color: active ? "rgba(96,165,250,0.95)" : "rgba(255,255,255,0.6)",
                    border: active ? "1px solid rgba(59,130,246,0.4)" : "1px solid rgba(255,255,255,0.08)",
                  }}
                >
                  {opt.label}
                </button>
              )
            })}
          </div>
          {followUpPreset === "custom" && (
            <input
              type="datetime-local"
              value={followUpCustom}
              onChange={(e) => setFollowUpCustom(e.target.value)}
              className="w-full px-2 py-1.5 rounded text-[11px] outline-none"
              style={{
                background: "rgba(255,255,255,0.05)",
                color: "rgba(255,255,255,0.9)",
                border: "1px solid rgba(255,255,255,0.1)",
                colorScheme: "dark",
              }}
            />
          )}
        </div>

        {/* Reminder cadence */}
        <div className="flex flex-col gap-1.5">
          <span className="text-[11px]" style={{ color: "rgba(255,255,255,0.55)" }}>
            Then remind
          </span>
          <div className="flex flex-wrap gap-1.5">
            {CADENCE_OPTIONS.map((opt) => {
              const active = cadence === opt.value
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setCadence(opt.value)}
                  className="px-2.5 py-1 rounded-full text-[11px] transition-colors"
                  style={{
                    background: active ? "rgba(59,130,246,0.25)" : "rgba(255,255,255,0.05)",
                    color: active ? "rgba(96,165,250,0.95)" : "rgba(255,255,255,0.6)",
                    border: active ? "1px solid rgba(59,130,246,0.4)" : "1px solid rgba(255,255,255,0.08)",
                  }}
                >
                  {opt.label}
                </button>
              )
            })}
          </div>
        </div>

        {/* Submit */}
        <button
          onClick={handleSubmit}
          disabled={selected.length === 0 || submitting || nothingChanged}
          className="flex items-center justify-center gap-2 px-4 py-2 rounded-lg text-[13px] font-medium transition-all"
          style={{
            background: selected.length > 0 && !nothingChanged ? "rgba(59,130,246,0.8)" : "rgba(59,130,246,0.3)",
            color: "#fff",
            opacity: submitting ? 0.5 : 1,
          }}
        >
          <Send size={13} />
          {submitting
            ? (isReassign ? "Reassigning..." : "Delegating...")
            : isReassign
              ? `Reassign${selected.length > 0 ? ` to ${selected.length}` : ""}`
              : `Delegate${selected.length > 0 ? ` to ${selected.length}` : ""}`}
        </button>
      </div>
    </div>,
    document.body
  )
}

// ---------------------------------------------------------------------------
// TodoTimeline
// ---------------------------------------------------------------------------

export function TodoTimeline({
  todoId,
  todoText,
  delegatedTo,
  getTodoUpdates,
  onNudge,
  onClose,
}: {
  todoId: string
  todoText: string
  delegatedTo: Delegatee[]
  getTodoUpdates: (todoId: string) => Promise<TodoUpdateEntry[]>
  onNudge: () => void
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [updates, setUpdates] = useState<TodoUpdateEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [nudging, setNudging] = useState(false)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [onClose])

  useEffect(() => {
    let cancelled = false
    getTodoUpdates(todoId).then((data) => {
      if (!cancelled) { setUpdates(data); setLoading(false) }
    }).catch(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [todoId, getTodoUpdates])

  const nudgeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    return () => { if (nudgeTimerRef.current) clearTimeout(nudgeTimerRef.current) }
  }, [])

  const handleNudge = async () => {
    setNudging(true)
    try {
      await onNudge()
    } catch {
      setNudging(false)
      return
    }
    nudgeTimerRef.current = setTimeout(async () => {
      try {
        const data = await getTodoUpdates(todoId)
        setUpdates(data)
      } catch { /* refresh failed, updates will load on next open */ }
      setNudging(false)
    }, 1500)
  }

  const sourceIcon = (source: string) => {
    if (source === "slack") return <MessageCircle size={10} />
    if (source === "nudge") return <Send size={10} />
    if (source === "delegation") return <UserPlus size={10} />
    return <MessageCircle size={10} />
  }

  return createPortal(
    <div className="fixed inset-0 z-[9999] flex items-center justify-center" style={{ background: "rgba(0,0,0,0.5)" }}>
      <div
        ref={ref}
        className="rounded-xl shadow-2xl p-5 max-h-[600px] flex flex-col gap-3 animate-[fadeIn_120ms_ease-out]"
        style={{ background: "rgba(30,30,30,0.98)", border: "1px solid rgba(255,255,255,0.1)", width: "calc(100% - 24px)", maxWidth: 420 }}
      >
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex-1 min-w-0">
            <h3 className="text-[14px] font-medium truncate" style={{ color: "rgba(255,255,255,0.9)" }}>
              {todoText}
            </h3>
            <p className="text-[11px] mt-0.5" style={{ color: "rgba(255,255,255,0.4)" }}>
              Delegated to {delegatedTo.map((d) => d.name).join(", ")}
            </p>
          </div>
          <button onClick={onClose} className="w-6 h-6 flex items-center justify-center rounded hover:bg-white/10 shrink-0">
            <X size={14} style={{ color: "rgba(255,255,255,0.6)" }} />
          </button>
        </div>

        <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)" }} />

        {/* Timeline */}
        <div className="flex-1 overflow-y-auto flex flex-col gap-0" style={{ scrollbarWidth: "thin" }}>
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 size={16} className="animate-spin" style={{ color: "rgba(255,255,255,0.4)" }} />
            </div>
          ) : updates.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-8 gap-2">
              <MessageCircle size={20} style={{ color: "rgba(255,255,255,0.2)" }} />
              <span className="text-[12px]" style={{ color: "rgba(255,255,255,0.3)" }}>No updates yet</span>
            </div>
          ) : (
            updates.map((upd, i) => (
              <div key={upd.id} className="flex gap-3 py-2.5 relative">
                {i < updates.length - 1 && (
                  <div
                    className="absolute left-[9px] top-[28px] bottom-0 w-[1px]"
                    style={{ background: "rgba(255,255,255,0.08)" }}
                  />
                )}
                <div
                  className="w-[18px] h-[18px] rounded-full flex items-center justify-center shrink-0 mt-0.5"
                  style={{
                    background: upd.source === "delegation" ? "rgba(59,130,246,0.2)" :
                      upd.source === "nudge" ? "rgba(251,146,60,0.2)" :
                      "rgba(255,255,255,0.08)",
                  }}
                >
                  {sourceIcon(upd.source)}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-baseline gap-2">
                    <span className="text-[12px] font-medium" style={{ color: "rgba(255,255,255,0.8)" }}>
                      {upd.author_name}
                    </span>
                    <span className="text-[10px]" style={{ color: "rgba(255,255,255,0.3)" }}>
                      {new Date(upd.created_at).toLocaleDateString([], { month: "short", day: "numeric" })}{" "}
                      {new Date(upd.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                    </span>
                  </div>
                  <p className="text-[12px] mt-0.5 leading-snug" style={{ color: "rgba(255,255,255,0.6)" }}>
                    {upd.content}
                  </p>
                </div>
              </div>
            ))
          )}
        </div>

        {/* Nudge button */}
        <button
          onClick={handleNudge}
          disabled={nudging}
          className="flex items-center justify-center gap-2 px-4 py-2 rounded-lg text-[13px] font-medium transition-all hover:brightness-110"
          style={{
            background: "rgba(251,146,60,0.15)",
            color: "rgba(251,146,60,0.9)",
            border: "1px solid rgba(251,146,60,0.2)",
            opacity: nudging ? 0.5 : 1,
          }}
        >
          <Send size={13} />
          {nudging ? "Sending..." : "Ask for status update"}
        </button>
      </div>
    </div>,
    document.body
  )
}
