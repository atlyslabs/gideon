"use client"

import { useState, useRef, useEffect } from "react"
import { Send, ExternalLink, Pencil, X, Check, ChevronDown, ChevronUp } from "lucide-react"
import ReactMarkdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"
import type { DraftData } from "../../types"
import { pingman } from "../../utils/pingman"
import { getIntegrationIcon } from "../../utils/integrationIcons"

// Inline-styled markdown components for the draft preview. Styles mirror
// `_EMAIL_TABLE_STYLE` / `_EMAIL_TH_STYLE` / `_EMAIL_TD_STYLE` in
// services/gideon/services/gideon_inbox_service.py so what users see here
// matches what Gmail renders after the draft is created.
//
// Colors are hardcoded (not theme tokens) on purpose — the surrounding
// `PREVIEW_SURFACE_STYLE` wraps the body in a white "paper" surface that
// mimics Gmail's white compose pane. Inheriting `var(--text-*)` would
// give us light text from the chat dark theme on a light cell background
// → invisible. Treating the preview as its own light-themed surface
// matches the actual rendering target and avoids dark/light collisions.
const PREVIEW_TEXT = "#1f2328"          // GitHub default body text on light bg
const PREVIEW_MUTED = "#57606a"         // Secondary text
const PREVIEW_LINK = "#0969da"          // Gmail-ish link blue
const PREVIEW_BORDER = "#d0d7de"
const PREVIEW_TH_BG = "#f6f8fa"

const PREVIEW_SURFACE_STYLE: React.CSSProperties = {
  background: "#ffffff",
  color: PREVIEW_TEXT,
  padding: "12px 14px",
  borderRadius: 8,
  fontSize: 13,
  lineHeight: 1.55,
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
}

const PREVIEW_MD_COMPONENTS: Components = {
  table: ({ children }) => (
    <table
      style={{
        borderCollapse: "collapse",
        margin: "8px 0",
        fontSize: 13,
        border: `1px solid ${PREVIEW_BORDER}`,
        color: PREVIEW_TEXT,
      }}
    >
      {children}
    </table>
  ),
  th: ({ children, style }) => (
    <th
      style={{
        border: `1px solid ${PREVIEW_BORDER}`,
        padding: "6px 10px",
        background: PREVIEW_TH_BG,
        textAlign: "left",
        fontWeight: 600,
        color: PREVIEW_TEXT,
        ...style,
      }}
    >
      {children}
    </th>
  ),
  td: ({ children, style }) => (
    <td
      style={{
        border: `1px solid ${PREVIEW_BORDER}`,
        padding: "6px 10px",
        verticalAlign: "top",
        color: PREVIEW_TEXT,
        ...style,
      }}
    >
      {children}
    </td>
  ),
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" style={{ color: PREVIEW_LINK }}>
      {children}
    </a>
  ),
  p: ({ children }) => <p style={{ margin: "0 0 8px 0", color: PREVIEW_TEXT }}>{children}</p>,
  ul: ({ children }) => <ul style={{ margin: "0 0 8px 0", paddingLeft: 20, color: PREVIEW_TEXT }}>{children}</ul>,
  ol: ({ children }) => <ol style={{ margin: "0 0 8px 0", paddingLeft: 20, color: PREVIEW_TEXT }}>{children}</ol>,
  li: ({ children }) => <li style={{ margin: "2px 0" }}>{children}</li>,
  code: ({ children }) => (
    <code style={{ background: PREVIEW_TH_BG, padding: "1px 4px", borderRadius: 3, fontSize: 12, color: PREVIEW_TEXT }}>
      {children}
    </code>
  ),
  blockquote: ({ children }) => (
    <blockquote style={{ borderLeft: `3px solid ${PREVIEW_BORDER}`, margin: "8px 0", padding: "2px 12px", color: PREVIEW_MUTED }}>
      {children}
    </blockquote>
  ),
}

interface EmailDraftCardProps {
  draft: DraftData
  onSendViaChat?: (message: string) => void
  alreadySent?: boolean
}

export default function EmailDraftCard({ draft, onSendViaChat, alreadySent = false }: EmailDraftCardProps) {
  const [editing, setEditing] = useState(false)
  const [expanded, setExpanded] = useState(true)
  const [sending, setSending] = useState(false)
  const [sent, setSent] = useState(alreadySent)

  // React to prop updates (e.g., send_draft tool result arrives after initial render)
  useEffect(() => {
    if (alreadySent) setSent(true)
  }, [alreadySent])

  const [to, setTo] = useState(draft.to.match(/<([^>]+)>/) ? draft.to.match(/<([^>]+)>/)![1] : draft.to)
  const [subject, setSubject] = useState(draft.subject)
  const [body, setBody] = useState(draft.body)
  const [cc, setCc] = useState(draft.cc || "")

  const bodyRef = useRef<HTMLTextAreaElement>(null)

  // Extract clean email from "Name <email>" format
  const extractEmail = (raw: string) => {
    const match = raw.match(/<([^>]+)>/)
    return match ? match[1] : raw
  }
  const extractName = (raw: string) => {
    const match = raw.match(/^(.+?)\s*</)
    return match ? match[1].trim() : ""
  }
  const displayTo = extractName(draft.to)
    ? `${extractName(draft.to)} (${extractEmail(draft.to)})`
    : extractEmail(draft.to)

  // Auto-resize textarea
  useEffect(() => {
    if (bodyRef.current) {
      bodyRef.current.style.height = "auto"
      bodyRef.current.style.height = bodyRef.current.scrollHeight + "px"
    }
  }, [body, editing])

  const handleSend = () => {
    if (sending || sent) return
    if (!onSendViaChat) return
    setSending(true)
    if (editing) {
      onSendViaChat(`Update the draft and send it. New details — To: ${to}, Subject: ${subject}, Body: ${body}${cc ? `, CC: ${cc}` : ""}`)
      setEditing(false)
    } else {
      onSendViaChat("Send it")
    }
    setTimeout(() => { setSending(false); setSent(true) }, 2000)
  }

  const handleOpenInGmail = () => {
    window.open(`https://mail.google.com/mail/u/0/#drafts`, "_blank")
  }

  // When sent via auto-mode (alreadySent), keep card visible but non-editable
  // When sent via manual click, also show sent state
  const isSent = sent

  return (
    <div
      className="rounded-xl overflow-hidden mb-2"
      style={{
        border: "1px solid var(--border)",
        background: "var(--surface-elevated)",
      }}
    >
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-2.5 cursor-pointer"
        style={{ borderBottom: expanded ? "1px solid var(--border)" : "none" }}
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2">
          <div
            className="w-6 h-6 rounded-md flex items-center justify-center"
            style={{ background: "rgba(234, 67, 53, 0.12)" }}
          >
            {getIntegrationIcon("gmail", 14)}
          </div>
          <span className="text-[13px] font-medium" style={{ color: "var(--text-primary)" }}>
            Email Draft
          </span>
          <span className="text-[12px] truncate max-w-[250px]" style={{ color: "var(--text-tertiary)" }}>
            to {extractEmail(to)}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {expanded ? (
            <ChevronUp size={14} style={{ color: "var(--text-tertiary)" }} />
          ) : (
            <ChevronDown size={14} style={{ color: "var(--text-tertiary)" }} />
          )}
        </div>
      </div>

      {expanded && (
        <>
          {/* Fields */}
          <div className="px-4 py-3 space-y-2.5">
            {/* To */}
            <div className="flex items-center gap-2">
              <span className="text-[12px] font-medium w-12 shrink-0" style={{ color: "var(--text-tertiary)" }}>To</span>
              {editing ? (
                <input
                  value={to}
                  onChange={(e) => setTo(e.target.value)}
                  className="flex-1 text-[13px] px-2.5 py-1.5 rounded-md outline-none"
                  style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
                  placeholder="recipient@example.com"
                />
              ) : (
                <span className="text-[13px]" style={{ color: "var(--text-primary)" }}>{displayTo}</span>
              )}
            </div>

            {/* CC (only show if has value or editing) */}
            {(cc || editing) && (
              <div className="flex items-center gap-2">
                <span className="text-[12px] font-medium w-12 shrink-0" style={{ color: "var(--text-tertiary)" }}>Cc</span>
                {editing ? (
                  <input
                    value={cc}
                    onChange={(e) => setCc(e.target.value)}
                    className="flex-1 text-[13px] px-2.5 py-1.5 rounded-md outline-none"
                    style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
                    placeholder="Add CC..."
                  />
                ) : (
                  <span className="text-[13px]" style={{ color: "var(--text-primary)" }}>{cc}</span>
                )}
              </div>
            )}

            {/* Subject */}
            <div className="flex items-start gap-2">
              <span className="text-[12px] font-medium w-12 shrink-0 pt-0.5" style={{ color: "var(--text-tertiary)" }}>Subj</span>
              {editing ? (
                <input
                  value={subject}
                  onChange={(e) => setSubject(e.target.value)}
                  className="flex-1 text-[13px] px-2.5 py-1.5 rounded-md outline-none font-medium"
                  style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
                />
              ) : (
                <span className="text-[13px] font-medium break-words" style={{ color: "var(--text-primary)" }}>{subject}</span>
              )}
            </div>

            {/* Body */}
            <div
              className="mt-2 rounded-lg"
              style={{
                background: editing ? "var(--surface)" : "transparent",
                border: editing ? "1px solid var(--border)" : "none",
              }}
            >
              {editing ? (
                <textarea
                  ref={bodyRef}
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  className="w-full text-[13px] leading-relaxed px-3 py-2.5 rounded-lg outline-none resize-none"
                  style={{ background: "transparent", color: "var(--text-primary)", minHeight: 80 }}
                />
              ) : (
                // Render markdown so tables / lists / bold render the same
                // way Gmail will after the draft is created (see
                // markdown_to_html in services/gideon_inbox_service.py).
                // Wrapped in a white "paper" surface so it mimics Gmail's
                // compose pane visually — also avoids dark-mode collision
                // where light-themed table cells would inherit near-white
                // text from the chat theme.
                // Edit mode keeps the textarea with raw markdown so users
                // can edit the source.
                <div style={PREVIEW_SURFACE_STYLE}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={PREVIEW_MD_COMPONENTS}>
                    {body}
                  </ReactMarkdown>
                </div>
              )}
            </div>
          </div>

          {/* Actions */}
          <div
            className="flex items-center justify-between px-4 py-2.5"
            style={{ borderTop: "1px solid var(--border)" }}
          >
            <div className="flex items-center gap-2">
              {isSent ? (
                <button
                  onClick={handleOpenInGmail}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors"
                  style={{ color: "var(--text-secondary)", border: "1px solid var(--border)" }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface)" }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
                >
                  <ExternalLink size={12} />
                  Open in Gmail
                </button>
              ) : editing ? (
                <button
                  onClick={() => {
                    setEditing(false)
                    setTo(draft.to.match(/<([^>]+)>/) ? draft.to.match(/<([^>]+)>/)![1] : draft.to)
                    setSubject(draft.subject)
                    setBody(draft.body)
                    setCc(draft.cc || "")
                  }}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors"
                  style={{ color: "var(--text-secondary)", border: "1px solid var(--border)" }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface)" }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
                >
                  <X size={12} />
                  Cancel
                </button>
              ) : (
                <>
                  <button
                    onClick={() => setEditing(true)}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors"
                    style={{ color: "var(--text-secondary)", border: "1px solid var(--border)" }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface)" }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
                  >
                    <Pencil size={12} />
                    Edit
                  </button>
                  <button
                    onClick={handleOpenInGmail}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors"
                    style={{ color: "var(--text-secondary)", border: "1px solid var(--border)" }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface)" }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
                  >
                    <ExternalLink size={12} />
                    Open in Gmail
                  </button>
                </>
              )}
            </div>

            {isSent ? (
              <div
                className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-[12px] font-medium"
                style={{ background: "rgba(16,185,129,0.12)", color: "#22c55e" }}
              >
                <Check size={12} />
                Sent
              </div>
            ) : (
              <button
                onClick={handleSend}
                disabled={sending}
                className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-[12px] font-medium transition-all"
                style={{
                  background: "var(--accent)",
                  color: "#fff",
                  opacity: sending ? 0.6 : 1,
                }}
                onMouseEnter={(e) => { if (!sending) e.currentTarget.style.opacity = "0.9" }}
                onMouseLeave={(e) => { if (!sending) e.currentTarget.style.opacity = "1" }}
              >
                <Send size={12} />
                {sending ? "Sending..." : editing ? "Update & Send" : "Send"}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  )
}
