"use client"

import { useEffect, useRef, useState, useCallback, useMemo } from "react"
import { User, ChevronDown, Copy, Check, RotateCcw, Pencil, FileText } from "lucide-react"
import dynamic from "next/dynamic"
import type { Row } from "../../types"
import ChatInput from "./ChatInput"
import UpcomingMeetingBanner from "./UpcomingMeetingBanner"
import FeedbackButtons from "./FeedbackButtons"
import MessageRow from "./MessageRow"

const GideonHead = dynamic(() => import("../GideonHead"), { ssr: false })

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getGreeting(): string {
  const h = new Date().getHours()
  if (h >= 5 && h < 12) return "Good Morning"
  if (h >= 12 && h < 17) return "Good Afternoon"
  if (h >= 17 && h < 21) return "Good Evening"
  return "Good Night"
}

function getDateString(): string {
  const d = new Date()
  const weekday = d.toLocaleDateString("en-US", { weekday: "long" })
  const month = d.toLocaleDateString("en-US", { month: "long" })
  const day = d.getDate()
  const s = [1, 21, 31].includes(day) ? "st" : [2, 22].includes(day) ? "nd" : [3, 23].includes(day) ? "rd" : "th"
  return `${weekday}, ${month} ${day}${s}`
}

// ---------------------------------------------------------------------------
// Copy button
// ---------------------------------------------------------------------------

function stripMarkdown(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, (m) => m.replace(/```\w*\n?/g, "").trim())
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/_([^_]+)_/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "- ")
    .replace(/^\s*\d+\.\s+/gm, (m) => m.trimStart())
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/^>\s?/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim()
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(stripMarkdown(text))
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {}
  }, [text])

  return (
    <button
      onClick={handleCopy}
      className="p-1 rounded transition-all"
      style={{
        color: copied ? "var(--success)" : "var(--text-tertiary)",
        background: copied ? "rgba(16, 185, 129, 0.1)" : "transparent",
      }}
      onMouseEnter={(e) => { if (!copied) e.currentTarget.style.color = "var(--text-secondary)" }}
      onMouseLeave={(e) => { if (!copied) e.currentTarget.style.color = "var(--text-tertiary)" }}
      title="Copy"
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
    </button>
  )
}

// ---------------------------------------------------------------------------
// Message grouping
// ---------------------------------------------------------------------------

// Group consecutive assistant rows into unified blocks (one avatar + one
// feedback strip per turn).
type MsgGroup =
  | { type: "user"; row: Row; rowIndex: number }
  | { type: "assistant"; rows: { row: Row; rowIndex: number }[] }

function buildMessageGroups(rows: Row[]): MsgGroup[] {
  const groups: MsgGroup[] = []
  rows.forEach((row, i) => {
    if (row.role === "user") {
      groups.push({ type: "user", row, rowIndex: i })
    } else {
      const last = groups[groups.length - 1]
      if (last?.type === "assistant") {
        last.rows.push({ row, rowIndex: i })
      } else {
        groups.push({ type: "assistant", rows: [{ row, rowIndex: i }] })
      }
    }
  })
  return groups
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ChatMessagesProps {
  rows: Row[]
  isStreaming: boolean
  userName?: string
  userAvatarUrl?: string
  onSend?: (message: string, mentions?: import("../../types").MentionItem[], options?: { fileIds?: string[]; files?: { filename: string; ext: string }[] }) => void
  onEditMessage?: (messageId: string, newText: string) => void
  onStop?: () => void
  sendDisabled?: boolean
  connectedProviders?: string[]
  readOnly?: boolean
  bottomInputRef?: React.RefObject<HTMLDivElement | null>
  autoMode?: boolean
  onAutoModeChange?: (on: boolean) => void
}

// ---------------------------------------------------------------------------
// Main component -- pure renderer, no state management
// ---------------------------------------------------------------------------

export default function ChatMessages({
  rows,
  isStreaming,
  userName,
  userAvatarUrl,
  onSend,
  onEditMessage,
  onStop,
  sendDisabled,
  connectedProviders,
  readOnly,
  bottomInputRef,
  autoMode,
  onAutoModeChange,
}: ChatMessagesProps) {
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const prevRowCountRef = useRef(0)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editText, setEditText] = useState("")
  const editRef = useRef<HTMLTextAreaElement>(null)

  const outerRef = useRef<HTMLDivElement>(null)
  const inlineInputRef = useRef<HTMLDivElement>(null)
  const hasMessages = rows.length > 0 || isStreaming

  // Morph animation -- input box shrinks into a bubble and flies to top-right
  const [morphAnim, setMorphAnim] = useState<{
    text: string
    files?: { filename: string; ext: string }[]
    vars: Record<string, string>
  } | null>(null)

  // Input morph -- inline input flies down to bottom input position
  const [inputMorph, setInputMorph] = useState<{
    startRect: DOMRect
    endRect: DOMRect
    phase: "start" | "end"
  } | null>(null)

  // Track the morph-clear timeouts so we can cancel them when the user navigates
  // away mid-animation (e.g. tap "New chat" while the bubble is still flying to
  // the top-right). Without this, the phantom stays visible on the new screen.
  const morphTimeoutRef = useRef<number | null>(null)
  const inputMorphTimeoutRef = useRef<number | null>(null)

  const handleInlineSend = useCallback((message: string, mentions?: import("../../types").MentionItem[], options?: { fileIds?: string[]; files?: { filename: string; ext: string }[] }) => {
    if (!onSend) return

    // Measure the input card position for the morph animation
    const cardEl = inlineInputRef.current?.querySelector(".rounded-2xl") as HTMLElement | null
    const outerEl = outerRef.current
    if (cardEl && outerEl) {
      const outerRect = outerEl.getBoundingClientRect()
      const cardRect = cardEl.getBoundingClientRect()

      // Measure end bubble size (text + file chips)
      const fileCount = options?.files?.length || 0
      const fileChipHeight = fileCount > 0 ? fileCount * 38 + 8 : 0  // 38px per chip + 8px margin
      const measure = document.createElement("div")
      measure.style.cssText = `position:fixed;visibility:hidden;padding:12px 16px;font-size:14px;white-space:pre-wrap;max-width:${outerRect.width * 0.6}px`
      measure.textContent = message
      document.body.appendChild(measure)
      const endW = Math.min(Math.max(measure.offsetWidth + 2, fileCount > 0 ? 220 : 0), outerRect.width * 0.8)
      const endH = measure.offsetHeight + fileChipHeight
      document.body.removeChild(measure)

      // Start: input card position relative to outer wrapper
      const startTop = cardRect.top - outerRect.top
      const startLeft = cardRect.left - outerRect.left
      const startW = cardRect.width
      const startH = cardRect.height

      // End: top-right of messages area (px-6 py-8, max-w-5xl mx-auto, right-aligned)
      const avatarSpace = 48
      const maxContentW = Math.min(1024, outerRect.width - 48)
      const contentLeft = (outerRect.width - maxContentW) / 2
      const endTop = 32
      const endLeft = contentLeft + maxContentW - endW - avatarSpace

      // Mid: shrunk bubble, left edge stays fixed, right edge collapses inward
      const midLeft = startLeft

      setMorphAnim({
        text: message,
        files: options?.files,
        vars: {
          "--morph-start-top": startTop + "px",
          "--morph-start-left": startLeft + "px",
          "--morph-start-w": startW + "px",
          "--morph-start-h": startH + "px",
          "--morph-mid-left": midLeft + "px",
          "--morph-end-top": endTop + "px",
          "--morph-end-left": endLeft + "px",
          "--morph-end-w": endW + "px",
          "--morph-end-h": endH + "px",
        },
      })
      if (morphTimeoutRef.current !== null) window.clearTimeout(morphTimeoutRef.current)
      morphTimeoutRef.current = window.setTimeout(() => {
        setMorphAnim(null)
        morphTimeoutRef.current = null
      }, 1100)

      // Input morph: fly inline input down to bottom input position
      if (bottomInputRef?.current) {
        const bottomCard = bottomInputRef.current.querySelector(".rounded-2xl") as HTMLElement | null
        if (bottomCard) {
          const endRect = bottomCard.getBoundingClientRect()
          setInputMorph({ startRect: cardRect, endRect, phase: "start" })
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              setInputMorph(prev => prev ? { ...prev, phase: "end" } : null)
            })
          })
          if (inputMorphTimeoutRef.current !== null) window.clearTimeout(inputMorphTimeoutRef.current)
          inputMorphTimeoutRef.current = window.setTimeout(() => {
            setInputMorph(null)
            inputMorphTimeoutRef.current = null
          }, 700)
        }
      }
    }

    onSend(message, mentions, options)
  }, [onSend, bottomInputRef])

  // Focus the inline input when empty state is shown (new chat).
  // Also cancel any in-flight morph animation — when the user taps "New chat"
  // mid-animation (within the 1.1s window after a send), the phantom bubble
  // would otherwise linger on the welcome screen until the setTimeout fires.
  useEffect(() => {
    if (!hasMessages) {
      if (morphTimeoutRef.current !== null) {
        window.clearTimeout(morphTimeoutRef.current)
        morphTimeoutRef.current = null
      }
      if (inputMorphTimeoutRef.current !== null) {
        window.clearTimeout(inputMorphTimeoutRef.current)
        inputMorphTimeoutRef.current = null
      }
      setMorphAnim(null)
      setInputMorph(null)
      requestAnimationFrame(() => {
        const editor = inlineInputRef.current?.querySelector<HTMLElement>("[contenteditable]")
        editor?.focus()
      })
    }
  }, [hasMessages])

  // Cancel pending timeouts on unmount so we don't call setState on an unmounted component
  useEffect(() => {
    return () => {
      if (morphTimeoutRef.current !== null) window.clearTimeout(morphTimeoutRef.current)
      if (inputMorphTimeoutRef.current !== null) window.clearTimeout(inputMorphTimeoutRef.current)
    }
  }, [])

  // Track whether user is near bottom -- show/hide scroll-to-bottom button
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const onScroll = () => {
      const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      setShowScrollBtn(distFromBottom > 150)
    }
    el.addEventListener("scroll", onScroll, { passive: true })
    return () => el.removeEventListener("scroll", onScroll)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Auto-scroll when new messages appear
  useEffect(() => {
    const wasEmpty = prevRowCountRef.current === 0
    const prevCount = prevRowCountRef.current
    prevRowCountRef.current = rows.length

    if (wasEmpty && rows.length > 0) {
      requestAnimationFrame(() => {
        bottomRef.current?.scrollIntoView({ behavior: "instant" })
      })
      return
    }

    if (rows.length > prevCount) {
      const lastNew = rows[rows.length - 1]
      if (lastNew?.role === "user") {
        requestAnimationFrame(() => {
          bottomRef.current?.scrollIntoView({ behavior: "instant" })
        })
      }
      if (lastNew?.role === "assistant" && lastNew.streaming && !lastNew.content) {
        requestAnimationFrame(() => {
          bottomRef.current?.scrollIntoView({ behavior: "instant" })
        })
      }
    }
  }, [rows])

  const scrollToBottom = useCallback(() => {
    scrollContainerRef.current?.scrollTo({ top: scrollContainerRef.current.scrollHeight, behavior: "smooth" })
  }, [])

  // Grouping depends only on the role sequence of `rows`. Every structural
  // change (push/splice/pop, streaming→finalized re-id of the tail) changes
  // rows.length or the last row's id, so keying on those is sufficient.
  // Per-frame content mutations do NOT need a rebuild: groups hold references
  // to the same live Row objects the stream store mutates in place, and the
  // memoized <MessageRow> re-renders (or skips) based on the row's fields.
  const lastRowId = rows.length > 0 ? rows[rows.length - 1].id : null
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const groups = useMemo(() => buildMessageGroups(rows), [rows.length, lastRowId])

  return (
    <div ref={outerRef} className="flex-1 flex flex-col relative overflow-hidden">
      {/* Greeting overlay -- fades out when messages appear, no DOM swap */}
      <div
        className="absolute inset-0 z-10 flex flex-col justify-center items-center px-8 md:px-16"
        style={{
          opacity: hasMessages ? 0 : 1,
          transition: "opacity 0.4s ease-out",
          pointerEvents: hasMessages ? "none" : "auto",
        }}
      >
        <div className="mb-2"><GideonHead size={100} /></div>
        <p className="text-lg mb-1" style={{ color: "var(--text-tertiary)", textShadow: "0 2px 12px rgba(0,0,0,0.4)" }}>{getDateString()}</p>
        <h1 className="text-[42px] font-light leading-tight animate-text-shimmer text-center" style={{ textShadow: "0 2px 20px rgba(0,0,0,0.5)" }}>
          {getGreeting()}, {userName || "there"}
        </h1>
        <div className="mb-8" />
        {onSend && (
          <div ref={inlineInputRef} className="w-full max-w-2xl" style={{ opacity: inputMorph || morphAnim ? 0 : 1 }}>
            <ChatInput onSend={handleInlineSend} disabled={sendDisabled || false} inline connectedProviders={connectedProviders} autoMode={autoMode} onAutoModeChange={onAutoModeChange} />
          </div>
        )}
        <UpcomingMeetingBanner />
      </div>

      {/* Morph phantom -- input box shrinks into bubble and flies to top-right */}
      {morphAnim && (
        <div
          className="absolute z-20 overflow-hidden chat-morph-bubble"
          style={{
            ...(morphAnim.vars as React.CSSProperties),
            padding: "12px 16px",
            color: "var(--text-primary)",
          }}
        >
          {morphAnim.files && morphAnim.files.length > 0 && (
            <div className="flex flex-col gap-1.5 mb-2">
              {morphAnim.files.map((f, fi) => (
                <div
                  key={fi}
                  className="flex items-center gap-2 px-2.5 py-2 rounded-lg"
                  style={{ background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.08)" }}
                >
                  <FileText size={14} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
                  <span className="text-[12px] truncate" style={{ color: "var(--text-secondary)", maxWidth: 180 }}>{f.filename}</span>
                  {f.ext && (
                    <span className="text-[9px] font-medium px-1.5 py-0.5 rounded shrink-0 ml-auto" style={{ background: "rgba(255,255,255,0.08)", color: "var(--text-tertiary)" }}>{f.ext}</span>
                  )}
                </div>
              ))}
            </div>
          )}
          <span className="whitespace-pre-wrap" style={{ fontSize: "var(--font-size-content)" }}>{morphAnim.text}</span>
        </div>
      )}

      {/* Input morph phantom -- inline input flies down to bottom position */}
      {inputMorph && (
        <div
          className="fixed z-[100] rounded-2xl pointer-events-none"
          style={{
            top: inputMorph.phase === "end" ? inputMorph.endRect.top : inputMorph.startRect.top,
            left: inputMorph.phase === "end" ? inputMorph.endRect.left : inputMorph.startRect.left,
            width: inputMorph.phase === "end" ? inputMorph.endRect.width : inputMorph.startRect.width,
            height: inputMorph.phase === "end" ? inputMorph.endRect.height : inputMorph.startRect.height,
            background: "#000000",
            border: "0.5px solid rgba(255,255,255,0.10)",
            boxShadow: "0 -4px 24px rgba(0,0,0,0.5), 0 0 30px rgba(59,130,246,0.1), 0 0 0 1px rgba(59,130,246,0.12), inset 0 0.5px 0 rgba(255,255,255,0.06)",
            transition: "top 600ms cubic-bezier(0.4, 0, 0.2, 1), left 600ms cubic-bezier(0.4, 0, 0.2, 1), width 600ms cubic-bezier(0.4, 0, 0.2, 1), height 600ms cubic-bezier(0.4, 0, 0.2, 1)",
          }}
        />
      )}

      {/* Messages scroll area -- always mounted */}
      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto px-6 py-8 relative">
      {/* Scroll-to-bottom button */}
      {showScrollBtn && (
        <button
          onClick={scrollToBottom}
          className="sticky float-right bottom-4 right-0 z-10 p-2 rounded-full transition-all hover:scale-110"
          style={{
            background: "var(--surface-elevated)",
            border: "1px solid var(--border)",
            color: "var(--text-secondary)",
            boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
          }}
          title="Scroll to bottom"
        >
          <ChevronDown size={18} />
        </button>
      )}
      <div className="max-w-5xl mx-auto space-y-8">
        {groups.map((group) => {
            // -- User message --
            if (group.type === "user") {
              const row = group.row
              const rowIndex = group.rowIndex
              return (
                <div key={row.id} style={morphAnim && rowIndex === 0 ? { opacity: 0 } : undefined}>
                  {editingId === row.id ? (
                    <div className="flex flex-col items-end gap-2">
                      <div className="w-full rounded-2xl overflow-hidden" style={{ border: "1px solid var(--accent)", background: "var(--surface-elevated)" }}>
                        <textarea
                          ref={editRef}
                          value={editText}
                          onChange={(e) => setEditText(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && !e.shiftKey) {
                              e.preventDefault()
                              const trimmed = editText.trim()
                              if (trimmed && onEditMessage) {
                                onEditMessage(row.id, trimmed)
                                setEditingId(null)
                              }
                            }
                            if (e.key === "Escape") setEditingId(null)
                          }}
                          className="w-full bg-transparent text-sm p-4 outline-none resize-none"
                          style={{ color: "var(--text-primary)", minHeight: 60, maxHeight: 200 }}
                          autoFocus
                        />
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => setEditingId(null)}
                          className="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors hover:bg-white/10"
                          style={{ color: "var(--text-secondary)", border: "1px solid var(--border)" }}
                        >
                          Cancel
                        </button>
                        <button
                          onClick={() => {
                            const trimmed = editText.trim()
                            if (trimmed && onEditMessage) {
                              onEditMessage(row.id, trimmed)
                              setEditingId(null)
                            }
                          }}
                          className="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors"
                          style={{ background: "var(--text-primary)", color: "var(--background)" }}
                        >
                          Save
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="group/user flex flex-col items-end gap-1.5">
                      <div className="flex gap-3 items-center justify-end">
                        <div
                          className="max-w-[70%] px-5 py-3.5 rounded-2xl whitespace-pre-wrap break-words"
                          style={{ fontSize: "var(--font-size-content)", background: "linear-gradient(135deg, #2a2a2a, #1c1c1c)", color: "var(--text-primary)" }}
                        >
                        {row.files && row.files.length > 0 && (
                          <div className="flex flex-col gap-1.5 mb-2">
                            {row.files.map((f, fi) => (
                              <div
                                key={fi}
                                className="flex items-center gap-2 px-2.5 py-2 rounded-lg"
                                style={{ background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.08)" }}
                              >
                                <FileText size={14} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
                                <span className="text-[12px] truncate" style={{ color: "var(--text-secondary)", maxWidth: 180 }}>{f.filename}</span>
                                {f.ext && (
                                  <span className="text-[9px] font-medium px-1.5 py-0.5 rounded shrink-0 ml-auto" style={{ background: "rgba(255,255,255,0.08)", color: "var(--text-tertiary)" }}>{f.ext}</span>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                        {row.content}
                        </div>
                        {!morphAnim && rowIndex === 0 && row.id === "pending-user" ? (
                          userAvatarUrl ? (
                            <img key="avatar-pop" src={userAvatarUrl} alt="" className="w-9 h-9 rounded-full shrink-0 animate-avatar-pop" style={{ boxShadow: "0 2px 8px rgba(0,0,0,0.4), 0 0 0 0.5px rgba(255,255,255,0.25)" }} />
                          ) : (
                            <div key="avatar-pop" className="w-9 h-9 rounded-full flex items-center justify-center shrink-0 animate-avatar-pop" style={{ background: "rgba(59,130,246,0.15)", boxShadow: "0 2px 8px rgba(0,0,0,0.4), 0 0 0 0.5px rgba(255,255,255,0.25)" }}>
                              <User size={16} style={{ color: "var(--accent)" }} />
                            </div>
                          )
                        ) : (
                          userAvatarUrl ? (
                            <img src={userAvatarUrl} alt="" className="w-9 h-9 rounded-full shrink-0" style={{ boxShadow: "0 2px 8px rgba(0,0,0,0.4), 0 0 0 0.5px rgba(255,255,255,0.25)" }} />
                          ) : (
                            <div className="w-9 h-9 rounded-full flex items-center justify-center shrink-0" style={{ background: "rgba(59,130,246,0.15)", boxShadow: "0 2px 8px rgba(0,0,0,0.4), 0 0 0 0.5px rgba(255,255,255,0.25)" }}>
                              <User size={16} style={{ color: "var(--accent)" }} />
                            </div>
                          )
                        )}
                      </div>
                      {/* Action buttons -- visible on hover */}
                      <div className="flex items-center gap-1 opacity-0 group-hover/user:opacity-100 transition-opacity">
                        {onEditMessage && !isStreaming && (
                          <button
                            onClick={() => onEditMessage(row.id, row.content)}
                            className="p-1 rounded transition-colors hover:bg-white/10"
                            style={{ color: "var(--text-tertiary)" }}
                            title="Retry"
                          >
                            <RotateCcw size={13} />
                          </button>
                        )}
                        {onEditMessage && !isStreaming && (
                          <button
                            onClick={() => { setEditText(row.content); setEditingId(row.id) }}
                            className="p-1 rounded transition-colors hover:bg-white/10"
                            style={{ color: "var(--text-tertiary)" }}
                            title="Edit & resend"
                          >
                            <Pencil size={13} />
                          </button>
                        )}
                        <CopyButton text={row.content} />
                      </div>
                    </div>
                  )}
                </div>
              )
            }

            // -- Assistant group -- single avatar, unified block --
            const assistantRows = group.rows
            const firstRow = assistantRows[0].row
            const lastRowWithContent = [...assistantRows].reverse().find(r => r.row.content)
            // Collect all text content for the group-level copy button
            const allText = assistantRows.map(r => r.row.content).filter(Boolean).join("\n\n")
            // Find the last message ID for feedback (prefer a real DB id)
            const feedbackRow = lastRowWithContent?.row

            return (
              <div key={firstRow.id} className="flex gap-3">
                <img src="/logo.png" alt="Gideon" className="w-9 h-9 rounded-full shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  {assistantRows.map(({ row }) => (
                    // Flattened props (not the row object) so React.memo's
                    // shallow compare sees in-place row mutations — see the
                    // perf note in MessageRow.tsx. Historical rows skip
                    // re-rendering (and their ReactMarkdown parse) on every
                    // streaming tick; the live row's props change per frame.
                    <MessageRow
                      key={row.id}
                      id={row.id}
                      content={row.content}
                      streaming={row.streaming}
                      thinking={row.thinking}
                      thinkingStreaming={row.thinkingStreaming}
                      thinkingDuration={row.thinkingDuration}
                      toolCalls={row.toolCalls}
                      activeTools={row.activeTools}
                      drafts={row.drafts}
                      draftSent={row.draftSent}
                      codeImages={row.codeImages}
                      codeFiles={row.codeFiles}
                      charts={row.charts}
                      dataTables={row.dataTables}
                      previews={row.previews}
                      agentStatus={row.agentStatus}
                      onSend={onSend}
                    />
                  ))}

                  {/* Feedback buttons -- once at the end of the entire group */}
                  {feedbackRow && !feedbackRow.streaming && allText && (
                    <div className="flex items-center gap-0.5 mt-1">
                      <CopyButton text={allText} />
                      {!readOnly && feedbackRow.id && !feedbackRow.id.startsWith("streaming-") && !feedbackRow.id.startsWith("finalized-") && (
                        <FeedbackButtons messageId={feedbackRow.id} />
                      )}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        <div ref={bottomRef} />
      </div>
      </div>
    </div>
  )
}
