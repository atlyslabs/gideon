"use client"

import { useState } from "react"
import {
  PartyPopper,
  Loader2,
  ExternalLink,
  ChevronRight,
  ChevronLeft,
  ChevronUp,
  Bold,
  Italic,
  Strikethrough,
  List,
  ListOrdered,
  Paperclip,
} from "lucide-react"
import { AnimatePresence, motion } from "framer-motion"
import { pingman } from "../../utils/pingman"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import type { EmailDraft } from "../../types"
import { getAvatarColor } from "./utils"

// ---------------------------------------------------------------------------
// Parse email reply chain into individual messages
// ---------------------------------------------------------------------------

interface ParsedMessage {
  name: string
  email: string
  date: string
  body: string
  avatar_url?: string | null
}

function parseEmailThread(emailBody: string, senderName: string, senderEmail: string, emailDate: string, senderAvatar?: string | null): ParsedMessage[] {
  // Pattern: "On Thu, Apr 9, 2026 at 12:58 PM Name <email@example.com> wrote:"
  const wrotePattern = /^On\s+.+?\s+<([^>]+)>\s+wrote:\s*$/m
  // Also match: "On Thu, Apr 9, 2026 at 12:58 PM Name Name <email> wrote:"
  const fullPattern = /On\s+(.+?)\s+([^<\n]+?)\s*<([^>]+)>\s+wrote:/g

  const messages: ParsedMessage[] = []

  // Try to split by "On ... wrote:" patterns
  const parts = emailBody.split(/\n(?=On\s+.+?<[^>]+>\s+wrote:)/g)

  if (parts.length <= 1) {
    // No reply chain detected -- return single message
    return [{
      name: senderName,
      email: senderEmail,
      date: emailDate,
      body: emailBody.trim(),
      avatar_url: senderAvatar,
    }]
  }

  // First part is the latest message (no "On...wrote:" header)
  const firstBody = parts[0].trim()
  if (firstBody) {
    messages.push({
      name: senderName,
      email: senderEmail,
      date: emailDate,
      body: firstBody,
      avatar_url: senderAvatar,
    })
  }

  // Remaining parts each start with "On ... wrote:"
  for (let i = 1; i < parts.length; i++) {
    const part = parts[i]
    // Capture everything between "On " and " <email> wrote:" as one group
    const headerMatch = part.match(/On\s+(.*?)\s*<([^>]+)>\s+wrote:\s*\n?([\s\S]*)/)
    if (headerMatch) {
      const prefix = headerMatch[1].trim() // e.g. "Thu, Apr 9, 2026 at 12:58 PM Alexishek Banerjee"
      const email = headerMatch[2].trim()
      let body = headerMatch[3]

      // Split prefix into date and name.
      // Google format: "Day, Mon DD, YYYY at HH:MM AM/PM Name Name"
      let name: string
      let dateStr: string
      const amPmSplit = prefix.match(/^(.+?(?:AM|PM|am|pm))\s+(.+)$/)
      if (amPmSplit) {
        dateStr = amPmSplit[1].trim()
        name = amPmSplit[2].trim()
      } else {
        // Fallback: name = last capitalized words
        const words = prefix.split(/\s+/)
        let nameStart = words.length
        for (let j = words.length - 1; j >= 0; j--) {
          if (/^[A-Z]/.test(words[j])) nameStart = j
          else break
        }
        name = words.slice(nameStart).join(" ") || prefix
        dateStr = words.slice(0, nameStart).join(" ")
      }

      // Strip leading ">" quote markers from the body
      body = body
        .split("\n")
        .map((line) => line.replace(/^>\s?/, ""))
        .join("\n")
        .trim()

      messages.push({ name, email, date: dateStr, body })
    }
  }

  return messages
}

function formatThreadDate(dateStr: string): string {
  try {
    const d = new Date(dateStr)
    if (isNaN(d.getTime())) return dateStr
    return `${d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}, ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", hour12: true })}`
  } catch {
    return dateStr
  }
}

function getInitials(name: string): string {
  return name?.trim().split(/\s+/).filter(Boolean).map(w => w[0]).join("").slice(0, 2).toUpperCase() || "?"
}

// ---------------------------------------------------------------------------
// Render email body with "Name <email>" → bold name + hover tooltip
// ---------------------------------------------------------------------------

function EmailBody({ text }: { text: string }) {
  // Split on "Name <email@domain>" patterns
  const parts: { type: "text" | "contact"; value: string; name?: string; email?: string }[] = []
  const pattern = /([A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]*)*)\s*<([^>]+@[^>]+)>/g
  let lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ type: "text", value: text.slice(lastIndex, match.index) })
    }
    parts.push({ type: "contact", value: match[0], name: match[1].trim(), email: match[2].trim() })
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) {
    parts.push({ type: "text", value: text.slice(lastIndex) })
  }

  if (parts.length === 0) return <>{text}</>

  return (
    <>
      {parts.map((p, i) =>
        p.type === "contact" ? (
          <span key={i} className="relative group/contact inline">
            <span className="font-semibold" style={{ color: "rgba(255,255,255,0.85)" }}>{p.name}</span>
            <span
              className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 px-2 py-1 rounded-md text-[10px] whitespace-nowrap opacity-0 group-hover/contact:opacity-100 pointer-events-none transition-opacity duration-150"
              style={{ background: "rgba(0,0,0,0.85)", color: "rgba(255,255,255,0.8)", border: "1px solid rgba(255,255,255,0.1)" }}
            >
              {p.email}
            </span>
          </span>
        ) : (
          <span key={i}>{p.value}</span>
        )
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Thread message card
// ---------------------------------------------------------------------------

function ThreadMessage({ msg }: { msg: ParsedMessage }) {
  const colors = getAvatarColor(msg.email)
  return (
    <div className="px-4 py-3">
      <div className="flex items-start gap-3">
        {msg.avatar_url ? (
          <img src={msg.avatar_url} alt="" className="w-8 h-8 rounded-full shrink-0 object-cover" />
        ) : (
          <div
            className="w-8 h-8 rounded-full shrink-0 flex items-center justify-center"
            style={{ background: colors.bg }}
          >
            <span className="text-[11px] font-semibold" style={{ color: "#fff" }}>
              {getInitials(msg.name)}
            </span>
          </div>
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between mb-0.5">
            <span className="text-[13px] font-semibold" style={{ color: "#fff" }}>{msg.name}</span>
            <span className="text-[10px] shrink-0 ml-2" style={{ color: "rgba(255,255,255,0.35)" }}>
              {formatThreadDate(msg.date)}
            </span>
          </div>
          <p className="text-[11px] mb-2" style={{ color: "rgba(255,255,255,0.3)" }}>{msg.email}</p>
          <p className="text-[13px] leading-relaxed whitespace-pre-wrap" style={{ color: "rgba(255,255,255,0.65)" }}>
            <EmailBody text={msg.body} />
          </p>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function EmailDraftsTab({
  emailDrafts,
  mutateDrafts,
  avatarMap = {},
}: {
  emailDrafts: EmailDraft[] | undefined
  mutateDrafts: (updater?: (prev?: EmailDraft[]) => EmailDraft[] | undefined, opts?: { revalidate: boolean }) => void
  avatarMap?: Record<string, string>
}) {
  const [currentDraftIndex, setCurrentDraftIndex] = useState(0)
  const [editedDrafts, setEditedDrafts] = useState<Record<string, string>>({})
  const [sendingDraftId, setSendingDraftId] = useState<string | null>(null)
  const [skippingDraftId, setSkippingDraftId] = useState<string | null>(null)
  const [threadExpanded, setThreadExpanded] = useState(true)

  const currentDraft = emailDrafts && emailDrafts.length > 0 ? emailDrafts[Math.min(currentDraftIndex, emailDrafts.length - 1)] : null
  const getEditedBody = (draft: EmailDraft) => editedDrafts[draft.id] ?? draft.draft_body

  const handleDraftSend = async (draft: EmailDraft) => {
    setSendingDraftId(draft.id)
    try {
      const body = getEditedBody(draft)
      if (editedDrafts[draft.id]) {
        await pingman.patch(`/api/gideon/v1/hub/email-drafts/${draft.id}`, { draft_body: body })
      }
      await pingman.post(`/api/gideon/v1/hub/email-drafts/${draft.id}/send`, { draft_body: body })
      mutateDrafts((prev) => {
        const filtered = prev?.filter((d) => d.id !== draft.id)
        if (filtered && currentDraftIndex >= filtered.length) {
          setCurrentDraftIndex(Math.max(0, filtered.length - 1))
        }
        return filtered
      }, { revalidate: false })
      setEditedDrafts((prev) => { const next = { ...prev }; delete next[draft.id]; return next })
    } finally {
      setSendingDraftId(null)
    }
  }

  const handleDraftSkip = async (draft: EmailDraft) => {
    setSkippingDraftId(draft.id)
    try {
      await pingman.post(`/api/gideon/v1/hub/email-drafts/${draft.id}/skip`)
      mutateDrafts((prev) => {
        const filtered = prev?.filter((d) => d.id !== draft.id)
        if (filtered && currentDraftIndex >= filtered.length) {
          setCurrentDraftIndex(Math.max(0, filtered.length - 1))
        }
        return filtered
      }, { revalidate: false })
      setEditedDrafts((prev) => { const next = { ...prev }; delete next[draft.id]; return next })
    } finally {
      setSkippingDraftId(null)
    }
  }

  if (!emailDrafts || emailDrafts.length === 0) {
    return (
      <div className="px-5 py-2">
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          {getIntegrationIcon("gmail", 32)}
          <p className="text-[15px] font-semibold" style={{ color: "#fff" }}>All done!</p>
          <p className="text-[13px]" style={{ color: "rgba(255,255,255,0.6)" }}>No pending email drafts.</p>
        </div>
      </div>
    )
  }

  // Parse the thread for the current draft, enrich with workspace avatars
  const threadMessages = currentDraft
    ? parseEmailThread(currentDraft.email_body, currentDraft.sender_name, currentDraft.sender_email, currentDraft.email_date, currentDraft.sender_avatar_url)
        .map((msg) => ({
          ...msg,
          avatar_url: msg.avatar_url || avatarMap[msg.email.toLowerCase()] || null,
        }))
    : []

  return (
    <div className="px-4 py-2">
      {/* Pagination */}
      <div className="flex items-center justify-between mb-3">
        <span className="text-[10px] font-semibold tracking-widest uppercase" style={{ color: "rgba(255,255,255,0.4)" }}>
          Email {currentDraftIndex + 1} of {emailDrafts.length}
        </span>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setCurrentDraftIndex((p) => Math.max(0, p - 1))}
            disabled={currentDraftIndex === 0}
            className="w-6 h-6 flex items-center justify-center rounded-full"
            style={{ color: currentDraftIndex === 0 ? "rgba(255,255,255,0.15)" : "rgba(255,255,255,0.6)" }}
          >
            <ChevronLeft size={15} />
          </button>
          <button
            onClick={() => setCurrentDraftIndex((p) => Math.min(emailDrafts.length - 1, p + 1))}
            disabled={currentDraftIndex >= emailDrafts.length - 1}
            className="w-6 h-6 flex items-center justify-center rounded-full"
            style={{ color: currentDraftIndex >= emailDrafts.length - 1 ? "rgba(255,255,255,0.15)" : "rgba(255,255,255,0.6)" }}
          >
            <ChevronRight size={15} />
          </button>
        </div>
      </div>

      <AnimatePresence mode="wait">
        {currentDraft && (
          <motion.div
            key={currentDraft.id}
            initial={{ opacity: 0, x: 40 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -40 }}
            transition={{ duration: 0.2 }}
          >
            {/* === MAIN CARD === */}
            <div
              className="rounded-2xl overflow-hidden"
              style={{
                background: "rgba(8,8,8,0.72)",
                backdropFilter: "blur(48px) saturate(1.8)",
                WebkitBackdropFilter: "blur(48px) saturate(1.8)",
                border: "1px solid rgba(255,255,255,0.06)",
                boxShadow: "0 12px 40px rgba(0,0,0,0.5), inset 0 0.5px 0 rgba(255,255,255,0.05)",
              }}
            >
              {/* Header: Gmail icon + Subject + Open in Gmail */}
              <div className="flex items-center justify-between px-4 pt-4 pb-2">
                <div className="flex items-center gap-2 min-w-0">
                  {getIntegrationIcon("gmail", 18)}
                  <span className="text-[13px] font-semibold truncate" style={{ color: "#fff" }}>
                    {currentDraft.subject || "No subject"}
                  </span>
                </div>
                <a
                  href={`https://mail.google.com/mail/u/0/#inbox/${currentDraft.email_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1 text-[11px] hover:opacity-80 shrink-0 ml-2"
                  style={{ color: "rgba(255,255,255,0.5)" }}
                >
                  Open in Gmail <ExternalLink size={10} />
                </a>
              </div>

              {/* Conversation thread toggle */}
              <button
                onClick={() => setThreadExpanded(!threadExpanded)}
                className="flex items-center gap-1.5 px-4 py-2 w-full text-left hover:bg-white/[0.03] transition-colors"
              >
                <ChevronUp
                  size={12}
                  style={{
                    color: "rgba(255,255,255,0.4)",
                    transform: threadExpanded ? undefined : "rotate(180deg)",
                    transition: "transform 0.15s",
                  }}
                />
                <span className="text-[10px] font-semibold tracking-widest uppercase" style={{ color: "rgba(255,255,255,0.4)" }}>
                  Conversation ({threadMessages.length} {threadMessages.length === 1 ? "message" : "messages"})
                </span>
              </button>

              {/* Thread messages */}
              {threadExpanded && (
                <div>
                  {threadMessages.map((msg, i) => (
                    <div key={i}>
                      {i > 0 && (
                        <div className="mx-4" style={{ borderTop: "1px solid rgba(255,255,255,0.06)" }} />
                      )}
                      <ThreadMessage msg={msg} />
                    </div>
                  ))}
                </div>
              )}

              {/* Divider before reply */}
              <div className="mx-4" style={{ borderTop: "1px solid rgba(255,255,255,0.08)" }} />

              {/* REPLY section */}
              <div className="px-4 pt-3 pb-4">
                <span className="text-[10px] font-bold tracking-widest block mb-2" style={{ color: "rgba(255,255,255,0.4)" }}>REPLY</span>
                <div
                  className="rounded-xl p-3"
                  style={{
                    background: "rgba(255,255,255,0.06)",
                    borderLeft: "3px solid rgba(255,255,255,0.15)",
                  }}
                >
                  <textarea
                    value={getEditedBody(currentDraft)}
                    onChange={(e) => setEditedDrafts((prev) => ({ ...prev, [currentDraft.id]: e.target.value }))}
                    onKeyDown={(e) => {
                      if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !sendingDraftId && !skippingDraftId) {
                        e.preventDefault()
                        handleDraftSend(currentDraft)
                      }
                    }}
                    className="w-full resize-none bg-transparent text-[13px] leading-relaxed outline-none"
                    style={{ color: "#fff", minHeight: 100 }}
                    rows={5}
                  />
                </div>
              </div>
            </div>

            {/* Toolbar + actions BELOW the card */}
            <div className="flex items-center gap-0.5 mt-3 px-1">
              <button className="w-8 h-8 flex items-center justify-center rounded-full hover:bg-white/10" style={{ color: "rgba(255,255,255,0.3)" }}>
                <Paperclip size={15} />
              </button>
              <div className="w-px h-4 mx-1" style={{ background: "rgba(255,255,255,0.1)" }} />
              {[Bold, Italic, Strikethrough, ListOrdered, List].map((Icon, i) => (
                <button key={i} className="w-8 h-8 flex items-center justify-center rounded-full hover:bg-white/10" style={{ color: "rgba(255,255,255,0.3)" }}>
                  <Icon size={15} />
                </button>
              ))}
            </div>

            {/* Skip / Send */}
            <div className="flex items-center justify-center gap-3 mt-3 pb-2">
              <button
                onClick={() => handleDraftSkip(currentDraft)}
                disabled={!!skippingDraftId || !!sendingDraftId}
                className="px-5 py-2 rounded-full text-[13px] font-medium hover:bg-white/10 transition-colors"
                style={{ color: "rgba(255,255,255,0.6)" }}
              >
                {skippingDraftId === currentDraft.id ? <Loader2 size={14} className="animate-spin" /> : "Skip"}
              </button>
              <button
                onClick={() => handleDraftSend(currentDraft)}
                disabled={!!sendingDraftId || !!skippingDraftId}
                className="px-5 py-2 rounded-full text-[13px] font-semibold flex items-center gap-1.5 hover:brightness-110 transition-all"
                style={{ background: "rgba(30,30,35,0.9)", color: "#fff", border: "1px solid rgba(255,255,255,0.12)" }}
              >
                {sendingDraftId === currentDraft.id ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <>
                    Send
                    <kbd className="text-[10px] px-1 py-0.5 rounded" style={{ background: "rgba(255,255,255,0.1)" }}>&#8984;</kbd>
                    <kbd className="text-[10px] px-1 py-0.5 rounded" style={{ background: "rgba(255,255,255,0.1)" }}>&#9166;</kbd>
                  </>
                )}
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
