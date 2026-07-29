"use client"

import { useState, useEffect } from "react"
import { createPortal } from "react-dom"
import useSWR from "swr"
import {
  Sparkles,
  Users,
  Loader2,
  HelpCircle,
  Copy,
  Check,
  X,
  MessageSquarePlus,
} from "lucide-react"

export function MeetingVideoIcon({ size = 16, className, style }: { size?: number; className?: string; style?: React.CSSProperties }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" className={className} style={style}>
      <path d="M7.25 3C9.12557 3 10.675 4.37763 10.9517 6.17578L12.9395 4.18945C13.3685 3.76046 14.0137 3.63209 14.5742 3.86426C15.1346 4.09647 15.5 4.64338 15.5 5.25V11.25C15.5 11.8566 15.1346 12.4035 14.5742 12.6357C14.0137 12.8679 13.3685 12.7395 12.9395 12.3105L10.9517 10.3228C10.6756 12.1216 9.12607 13.5 7.25 13.5H4.25C2.17893 13.5 0.5 11.8211 0.5 9.75V6.75C0.5 4.67893 2.17893 3 4.25 3H7.25ZM3.5 9.75C3.5 10.1642 3.83579 10.5 4.25 10.5H7.25C7.66421 10.5 8 10.1642 8 9.75V6.75C8 6.33579 7.66421 6 7.25 6H4.25C3.83579 6 3.5 6.33579 3.5 6.75V9.75Z" fill="currentColor" />
      <path d="M3.5 9.75C3.5 10.1642 3.83579 10.5 4.25 10.5H7.25C7.66421 10.5 8 10.1642 8 9.75V6.75C8 6.33579 7.66421 6 7.25 6H4.25C3.83579 6 3.5 6.33579 3.5 6.75V9.75Z" fill="currentColor" />
    </svg>
  )
}
import { AnimatePresence, motion } from "framer-motion"
import { pingman } from "../../utils/pingman"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import type { HubMeeting, MeetingAttendee } from "../../types"
import { formatMeetingTime, getAvatarColor, MEETING_DOT_COLORS } from "./utils"

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getMeetingDotColor(index: number): string {
  return MEETING_DOT_COLORS[index % MEETING_DOT_COLORS.length]
}

function getInitials(attendee: MeetingAttendee): string {
  if (attendee.display_name) {
    const parts = attendee.display_name.trim().split(/\s+/)
    if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
    return parts[0].substring(0, 2).toUpperCase()
  }
  return attendee.email.split("@")[0].substring(0, 2).toUpperCase()
}

function RsvpIcon({ status }: { status: MeetingAttendee["response_status"] }) {
  switch (status) {
    case "accepted": return <Check size={12} strokeWidth={3} style={{ color: "#34d399" }} />
    case "declined": return <X size={12} strokeWidth={3} style={{ color: "#f87171" }} />
    case "tentative": return <HelpCircle size={12} style={{ color: "#fbbf24" }} />
    default: return <HelpCircle size={12} style={{ color: "rgba(255,255,255,0.4)" }} />
  }
}

/** Detect meeting platform from link */
function getMeetIcon(meetLink: string | null | undefined): React.ReactNode {
  if (!meetLink) return null
  if (meetLink.includes("zoom.us")) return getIntegrationIcon("zoom", 16)
  return getIntegrationIcon("google_meet", 16)
}

// ---------------------------------------------------------------------------
// Attendee avatar (for collapsed row)
// ---------------------------------------------------------------------------

function AttendeeAvatar({ attendee, index, total, avatarUrl }: { attendee: MeetingAttendee; index: number; total: number; avatarUrl?: string }) {
  const colors = getAvatarColor(attendee.email)
  return avatarUrl ? (
    <img
      src={avatarUrl}
      alt=""
      className={`rounded-full shrink-0 object-cover ${index > 0 ? "-ml-1.5" : ""}`}
      style={{ width: 26, height: 26, border: "1.5px solid rgba(0,0,0,0.5)", zIndex: total - index }}
      title={attendee.display_name || attendee.email}
    />
  ) : (
    <div
      className={`flex items-center justify-center rounded-full shrink-0 ${index > 0 ? "-ml-1.5" : ""}`}
      style={{
        width: 26, height: 26,
        background: colors.bg,
        border: "1.5px solid rgba(0,0,0,0.5)",
        zIndex: total - index,
      }}
      title={attendee.display_name || attendee.email}
    >
      <span className="text-[9px] font-semibold" style={{ color: "#fff" }}>{getInitials(attendee)}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Attendee row (for expanded section)
// ---------------------------------------------------------------------------

function AttendeeRow({ attendee, avatarUrl }: { attendee: MeetingAttendee; avatarUrl?: string }) {
  const colors = getAvatarColor(attendee.email)
  return (
    <div className="flex items-center gap-2.5 py-1.5">
      {avatarUrl ? (
        <img src={avatarUrl} alt="" className="w-7 h-7 rounded-full shrink-0 object-cover" />
      ) : (
        <div
          className="w-7 h-7 rounded-full shrink-0 flex items-center justify-center"
          style={{ background: colors.bg }}
        >
          <span className="text-[9px] font-semibold" style={{ color: "#fff" }}>{getInitials(attendee)}</span>
        </div>
      )}
      <span className="flex-1 min-w-0 text-[12px] truncate" style={{ color: "rgba(255,255,255,0.8)" }}>
        {attendee.display_name || attendee.email}
      </span>
      <span className="shrink-0" title={attendee.response_status}>
        <RsvpIcon status={attendee.response_status} />
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// MeetingCard
// ---------------------------------------------------------------------------

function MeetingCardExpandedBody({
  meeting,
  prepData,
  onOpenPrep,
  avatarMap,
  otherAttendees,
}: {
  meeting: HubMeeting
  prepData: { status: string; summary: string | null; prep_content: string | null } | null | undefined
  onOpenPrep: (eventId: string, title: string) => void
  avatarMap: Record<string, string>
  otherAttendees: MeetingAttendee[]
}) {
  return (
    <>
      {/* Meeting Summary */}
      <div className="mb-3">
        <span className="text-[10px] font-bold tracking-widest uppercase block mb-1.5" style={{ color: "rgba(255,255,255,0.4)" }}>
          Meeting Summary
        </span>
        <p className="text-[12px] leading-relaxed" style={{ color: prepData?.summary ? "rgba(255,255,255,0.7)" : "rgba(255,255,255,0.3)", fontStyle: prepData?.summary ? "normal" : "italic" }}>
          {prepData?.summary || "Summary will appear once meeting prep is generated."}
        </p>
      </div>

      <div className="mb-3" style={{ borderTop: "1px solid rgba(255,255,255,0.06)" }} />

      {/* Meeting Prep */}
      <div className="flex items-center justify-between mb-3">
        <span className="text-[10px] font-bold tracking-widest uppercase" style={{ color: "rgba(255,255,255,0.4)" }}>
          Meeting Prep
        </span>
        <button
          className="px-2.5 py-1 rounded-md text-[10px] font-semibold transition-colors hover:brightness-110"
          style={{
            background: prepData?.prep_content ? "rgba(129,140,248,0.15)" : "rgba(255,255,255,0.06)",
            color: prepData?.prep_content ? "rgba(129,140,248,0.9)" : "rgba(255,255,255,0.4)",
            border: prepData?.prep_content ? "1px solid rgba(129,140,248,0.2)" : "1px solid rgba(255,255,255,0.08)",
          }}
          onClick={(e) => { e.stopPropagation(); onOpenPrep(meeting.id, meeting.title) }}
        >
          {prepData?.status === "generating" ? "Generating..." : "Open"}
        </button>
      </div>

      <div className="mb-2" style={{ borderTop: "1px solid rgba(255,255,255,0.06)" }} />

      {/* Attendees */}
      {otherAttendees.length > 0 && (
        <div>
          <span className="text-[10px] font-bold tracking-widest uppercase block mb-1.5" style={{ color: "rgba(255,255,255,0.4)" }}>
            Attendees
          </span>
          {otherAttendees.map((a) => (
            <AttendeeRow key={a.email} attendee={a} avatarUrl={avatarMap[a.email.toLowerCase()]} />
          ))}
        </div>
      )}
    </>
  )
}

export function MeetingCard({
  meeting,
  index,
  expanded,
  onToggleExpand,
  onOpenPrep,
  avatarMap = {},
  expansionMode = "inline",
}: {
  meeting: HubMeeting
  index: number
  expanded: boolean
  onToggleExpand: () => void
  onOpenPrep: (eventId: string, title: string) => void
  avatarMap?: Record<string, string>
  expansionMode?: "inline" | "modal"
}) {
  const dotColor = getMeetingDotColor(index)
  const startStr = formatMeetingTime(meeting.start_time)
  const endStr = formatMeetingTime(meeting.end_time)
  const otherAttendees = (meeting.attendees || []).filter((a) => !a.self)
  const visibleAttendees = otherAttendees.slice(0, 3)
  const extraCount = otherAttendees.length - visibleAttendees.length

  // Fetch prep when expanded
  const { data: prepData } = useSWR<{ status: string; summary: string | null; prep_content: string | null } | null>(
    expanded ? `/api/gideon/v1/hub/meetings/${meeting.id}/prep` : null,
    async (url: string) => {
      const res = await pingman.get<{ status: string; summary: string | null; prep_content: string | null }>(url)
      if (!res.ok) return null
      return res.data
    },
    { revalidateOnFocus: false }
  )

  return (
    <>
    <motion.div
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.2, delay: index * 0.04 }}
      className="mb-1"
    >
      {/* ── Collapsed row ─────────────────────────────────────────── */}
      <div
        className="group/meeting flex items-center gap-3 py-2.5 px-3 rounded-xl cursor-pointer transition-colors hover:bg-white/[0.04]"
        style={{
          background: "rgba(8,8,8,0.72)",
          backdropFilter: "blur(48px) saturate(1.8)",
          WebkitBackdropFilter: "blur(48px) saturate(1.8)",
          border: "1px solid rgba(255,255,255,0.06)",
        }}
        onClick={onToggleExpand}
      >
        {/* Colored dot / video icon */}
        <MeetingVideoIcon size={16} className="shrink-0" style={{ color: dotColor }} />

        {/* Title + time */}
        <div className="flex-1 min-w-0">
          <p className="text-[13px] font-medium leading-snug truncate" style={{ color: "#fff" }}>
            {meeting.title}
          </p>
          <p className="text-[11px] mt-0.5" style={{ color: "rgba(255,255,255,0.45)" }}>
            {startStr} &ndash; {endStr}
          </p>
        </div>

        {/* Right side: avatars (desktop default) / Join button (on hover desktop, always on touch) */}
        {meeting.meet_link ? (
          <>
            {/* Attendee avatars - hide on hover on desktop. Hidden entirely on touch so Join is always visible. */}
            <div className="hidden md:flex items-center shrink-0 group-hover/meeting:hidden">
              {otherAttendees.length > 0 ? (
                <>
                  {visibleAttendees.map((a, i) => (
                    <AttendeeAvatar key={a.email} attendee={a} index={i} total={visibleAttendees.length} avatarUrl={avatarMap[a.email.toLowerCase()]} />
                  ))}
                  {extraCount > 0 && (
                    <div className="flex items-center justify-center rounded-full -ml-1.5 shrink-0"
                      style={{ width: 26, height: 26, background: "rgba(255,255,255,0.08)", border: "1.5px solid rgba(0,0,0,0.5)" }}>
                      <span className="text-[9px] font-medium" style={{ color: "rgba(255,255,255,0.6)" }}>+{extraCount}</span>
                    </div>
                  )}
                </>
              ) : (
                <span className="text-[11px] flex items-center gap-1" style={{ color: "rgba(255,255,255,0.35)" }}>
                  <Users size={11} /> {meeting.attendees_count}
                </span>
              )}
            </div>

            {/* Join button - hover-revealed on desktop, always-visible on mobile/touch */}
            <a
              href={meeting.meet_link}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="flex md:hidden items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-semibold shrink-0 transition-all hover:brightness-110"
              style={{
                background: "rgba(16,185,129,0.15)",
                color: "#34d399",
                border: "1px solid rgba(16,185,129,0.2)",
              }}
            >
              {getMeetIcon(meeting.meet_link)}
              Join
            </a>
            <a
              href={meeting.meet_link}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="hidden md:group-hover/meeting:flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-semibold shrink-0 transition-all hover:brightness-110"
              style={{
                background: "rgba(16,185,129,0.15)",
                color: "#34d399",
                border: "1px solid rgba(16,185,129,0.2)",
              }}
            >
              {getMeetIcon(meeting.meet_link)}
              Join
            </a>
          </>
        ) : (
          /* No meet link - always show avatars */
          <div className="flex items-center shrink-0">
            {otherAttendees.length > 0 ? (
              <>
                {visibleAttendees.map((a, i) => (
                  <AttendeeAvatar key={a.email} attendee={a} index={i} total={visibleAttendees.length} avatarUrl={avatarMap[a.email.toLowerCase()]} />
                ))}
                {extraCount > 0 && (
                  <div className="flex items-center justify-center rounded-full -ml-1.5 shrink-0"
                    style={{ width: 26, height: 26, background: "rgba(255,255,255,0.08)", border: "1.5px solid rgba(0,0,0,0.5)" }}>
                    <span className="text-[9px] font-medium" style={{ color: "rgba(255,255,255,0.6)" }}>+{extraCount}</span>
                  </div>
                )}
              </>
            ) : (
              <span className="text-[11px] flex items-center gap-1" style={{ color: "rgba(255,255,255,0.35)" }}>
                <Users size={11} /> {meeting.attendees_count}
              </span>
            )}
          </div>
        )}
      </div>

      {/* ── Expanded content (inline) ─────────────────────────────── */}
      {expansionMode === "inline" && (
        <AnimatePresence>
          {expanded && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2, ease: "easeOut" }}
              className="overflow-hidden"
            >
              <div
                className="rounded-xl px-3.5 py-3 mt-1 mb-2"
                style={{
                  background: "rgba(8,8,8,0.72)",
                  backdropFilter: "blur(48px) saturate(1.8)",
                  WebkitBackdropFilter: "blur(48px) saturate(1.8)",
                  border: "1px solid rgba(255,255,255,0.06)",
                }}
              >
                <MeetingCardExpandedBody
                  meeting={meeting}
                  prepData={prepData}
                  onOpenPrep={onOpenPrep}
                  avatarMap={avatarMap}
                  otherAttendees={otherAttendees}
                />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      )}
    </motion.div>

    {/* ── Expanded content (modal — banner surface) ─────────────── */}
    {expansionMode === "modal" && (
      <AnimatePresence>
        {expanded && (
          <MeetingDetailModal
            meeting={meeting}
            prepData={prepData}
            onOpenPrep={onOpenPrep}
            avatarMap={avatarMap}
            otherAttendees={otherAttendees}
            onClose={onToggleExpand}
          />
        )}
      </AnimatePresence>
    )}
    </>
  )
}

function MeetingDetailModal({
  meeting,
  prepData,
  onOpenPrep,
  avatarMap,
  otherAttendees,
  onClose,
}: {
  meeting: HubMeeting
  prepData: { status: string; summary: string | null; prep_content: string | null } | null | undefined
  onOpenPrep: (eventId: string, title: string) => void
  avatarMap: Record<string, string>
  otherAttendees: MeetingAttendee[]
  onClose: () => void
}) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose() }
    document.addEventListener("keydown", handler)
    return () => document.removeEventListener("keydown", handler)
  }, [onClose])

  return createPortal(
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2 }}
      className="fixed inset-0 z-[9999] flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.5)" }}
      onClick={onClose}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 12 }}
        transition={{ duration: 0.25, ease: [0.4, 0, 0.2, 1] }}
        className="relative flex flex-col"
        style={{
          width: "calc(100% - 24px)", maxWidth: 520, maxHeight: "80vh",
          background: "rgba(8,8,8,0.72)",
          backdropFilter: "blur(48px) saturate(1.8)",
          WebkitBackdropFilter: "blur(48px) saturate(1.8)",
          border: "1px solid rgba(255,255,255,0.06)",
          borderRadius: 16,
          boxShadow: "0 24px 64px rgba(0,0,0,0.5), inset 0 0.5px 0 rgba(255,255,255,0.05)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 shrink-0" style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
          <div className="flex items-center gap-2.5 min-w-0">
            <MeetingVideoIcon size={16} style={{ color: "rgba(129,140,248,0.8)" }} />
            <div className="min-w-0">
              <h2 className="text-[14px] font-semibold truncate" style={{ color: "#fff" }}>{meeting.title}</h2>
              <p className="text-[11px] truncate mt-0.5" style={{ color: "rgba(255,255,255,0.45)" }}>
                {formatMeetingTime(meeting.start_time)} &ndash; {formatMeetingTime(meeting.end_time)}
              </p>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg transition-colors hover:bg-white/[0.06]" style={{ color: "rgba(255,255,255,0.5)" }}>
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          <MeetingCardExpandedBody
            meeting={meeting}
            prepData={prepData}
            onOpenPrep={onOpenPrep}
            avatarMap={avatarMap}
            otherAttendees={otherAttendees}
          />
        </div>
      </motion.div>
    </motion.div>,
    document.body,
  )
}

// ---------------------------------------------------------------------------
// Meeting Prep Modal
// ---------------------------------------------------------------------------

interface MeetingPrepData {
  id: string
  event_id: string
  title: string
  summary: string | null
  prep_content: string | null
  status: "pending" | "generating" | "completed" | "failed"
}

function escapeHtml(str: string): string {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;")
}

function renderPrepMarkdown(text: string): string {
  const safe = escapeHtml(text)
  return safe
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/^### (.+)$/gm, '<h3 style="font-size:15px;font-weight:600;margin:18px 0 8px;color:rgba(255,255,255,1)">$1</h3>')
    .replace(/^## (.+)$/gm, '<h2 style="font-size:17px;font-weight:700;margin:20px 0 10px;color:rgba(255,255,255,1)">$1</h2>')
    .replace(/^# (.+)$/gm, '<h1 style="font-size:20px;font-weight:700;margin:24px 0 12px;color:rgba(255,255,255,1)">$1</h1>')
    .replace(/^- (.+)$/gm, '<div style="padding-left:12px;margin:4px 0;display:flex;gap:8px"><span style="color:rgba(129,140,248,0.7)">-</span><span>$1</span></div>')
    .replace(/\n/g, "<br/>")
    .replace(/<br\/><br\/>/g, '<div style="height:10px"></div>')
}

export function MeetingPrepModal({
  eventId,
  meetingTitle,
  onClose,
  onStartChat,
}: {
  eventId: string
  meetingTitle: string
  onClose: () => void
  onStartChat?: (meetingTitle: string, prepContent: string) => void
}) {
  const { data, error, isLoading } = useSWR<MeetingPrepData>(
    eventId ? `/api/gideon/v1/hub/meetings/${eventId}/prep` : null,
    async (url: string) => {
      const res = await pingman.get<MeetingPrepData>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    { revalidateOnFocus: false }
  )

  const [copied, setCopied] = useState(false)
  const handleCopy = () => {
    navigator.clipboard.writeText(data?.prep_content || "")
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose() }
    document.addEventListener("keydown", handler)
    return () => document.removeEventListener("keydown", handler)
  }, [onClose])

  return createPortal(
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2 }}
      className="fixed inset-0 z-[9999] flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.5)" }}
      onClick={onClose}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 12 }}
        transition={{ duration: 0.25, ease: [0.4, 0, 0.2, 1] }}
        className="relative flex flex-col"
        style={{
          width: "calc(100% - 24px)", maxWidth: 600, maxHeight: "80vh",
          background: "rgba(8,8,8,0.72)",
          backdropFilter: "blur(48px) saturate(1.8)",
          WebkitBackdropFilter: "blur(48px) saturate(1.8)",
          border: "1px solid rgba(255,255,255,0.06)",
          borderRadius: 16,
          boxShadow: "0 24px 64px rgba(0,0,0,0.5), inset 0 0.5px 0 rgba(255,255,255,0.05)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 shrink-0" style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
          <div className="flex items-center gap-2.5 min-w-0">
            <Sparkles size={16} style={{ color: "rgba(129,140,248,0.8)" }} />
            <div className="min-w-0">
              <h2 className="text-[15px] font-semibold truncate" style={{ color: "#fff" }}>Meeting Prep</h2>
              <p className="text-[11px] truncate mt-0.5" style={{ color: "rgba(255,255,255,0.45)" }}>{meetingTitle}</p>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg transition-colors hover:bg-white/[0.06]" style={{ color: "rgba(255,255,255,0.5)" }}>
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5" style={{ minHeight: 200 }}>
          {isLoading ? (
            <div className="flex flex-col items-center justify-center py-16 gap-3">
              <Loader2 size={24} className="animate-spin" style={{ color: "rgba(129,140,248,0.7)" }} />
              <p className="text-[13px]" style={{ color: "rgba(255,255,255,0.5)" }}>Loading meeting prep...</p>
            </div>
          ) : error ? (
            <div className="flex flex-col items-center justify-center py-16 gap-3">
              <Sparkles size={28} style={{ color: "rgba(255,255,255,0.15)" }} />
              <p className="text-[14px] font-medium" style={{ color: "rgba(255,255,255,0.6)" }}>Meeting prep not available yet</p>
              <p className="text-[12px] text-center max-w-[280px]" style={{ color: "rgba(255,255,255,0.35)" }}>
                AI-generated meeting prep will appear here once generated.
              </p>
            </div>
          ) : data?.status === "generating" ? (
            <div className="flex flex-col items-center justify-center py-16 gap-3">
              <Loader2 size={24} className="animate-spin" style={{ color: "rgba(129,140,248,0.7)" }} />
              <p className="text-[13px]" style={{ color: "rgba(255,255,255,0.5)" }}>Generating meeting prep...</p>
            </div>
          ) : data?.prep_content ? (
            <div className="text-[13px] leading-relaxed" style={{ color: "rgba(255,255,255,0.85)" }}
              dangerouslySetInnerHTML={{ __html: renderPrepMarkdown(data.prep_content) }} />
          ) : (
            <div className="flex flex-col items-center justify-center py-16 gap-3">
              <Sparkles size={28} style={{ color: "rgba(255,255,255,0.15)" }} />
              <p className="text-[14px] font-medium" style={{ color: "rgba(255,255,255,0.6)" }}>No prep content yet</p>
              <p className="text-[12px] text-center max-w-[280px]" style={{ color: "rgba(255,255,255,0.35)" }}>Meeting prep is being generated. Check back shortly.</p>
            </div>
          )}
        </div>

        {data?.prep_content && (
          <div className="flex items-center justify-end gap-2 px-6 py-3 shrink-0" style={{ borderTop: "1px solid rgba(255,255,255,0.06)" }}>
            {onStartChat && (
              <button
                onClick={() => { onStartChat(meetingTitle, data.prep_content!); onClose() }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-semibold transition-all hover:brightness-110"
                style={{
                  background: "rgba(129,140,248,0.15)",
                  color: "rgba(129,140,248,0.9)",
                  border: "1px solid rgba(129,140,248,0.2)",
                }}
              >
                <MessageSquarePlus size={12} />
                Discuss in Chat
              </button>
            )}
            <button onClick={handleCopy}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-semibold transition-all hover:brightness-110"
              style={{
                background: copied ? "rgba(16,185,129,0.18)" : "rgba(255,255,255,0.06)",
                color: copied ? "#34d399" : "rgba(255,255,255,0.6)",
                border: copied ? "1px solid rgba(16,185,129,0.25)" : "1px solid rgba(255,255,255,0.08)",
              }}>
              {copied ? <Check size={12} /> : <Copy size={12} />}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        )}
      </motion.div>
    </motion.div>,
    document.body
  )
}
