"use client"

import { useState } from "react"
import { AnimatePresence } from "framer-motion"
import { useNextMeeting } from "../../hooks/useNextMeeting"
import { useAvatarMap } from "../../hooks/useAvatarMap"
import { MeetingCard, MeetingPrepModal } from "../hub/MeetingCard"

/**
 * Banner below the chat input showing the user's next upcoming meeting.
 * Renders a `MeetingCard` in "modal" expansion mode so clicking it opens a popup
 * with the same detail the hub's Meetings tab shows inline.
 *
 * All meeting-card logic (title/time/attendees/prep/join) lives in `MeetingCard` —
 * this file is only the "upcoming" framing + data source.
 */
export default function UpcomingMeetingBanner() {
  const nextMeeting = useNextMeeting()
  const avatarMap = useAvatarMap()
  const [expanded, setExpanded] = useState(false)
  const [prepOpen, setPrepOpen] = useState<{ eventId: string; title: string } | null>(null)

  if (!nextMeeting) return null

  return (
    <>
      <div className="w-full max-w-2xl mt-6">
        <p
          className="text-[10px] font-semibold tracking-widest uppercase mb-3"
          style={{ color: "rgba(255,255,255,0.35)" }}
        >
          Upcoming Meeting
        </p>

        <MeetingCard
          meeting={nextMeeting}
          index={0}
          expanded={expanded}
          onToggleExpand={() => setExpanded((v) => !v)}
          onOpenPrep={(eventId, title) => setPrepOpen({ eventId, title })}
          avatarMap={avatarMap}
          expansionMode="modal"
        />
      </div>

      <AnimatePresence>
        {prepOpen && (
          <MeetingPrepModal
            eventId={prepOpen.eventId}
            meetingTitle={prepOpen.title}
            onClose={() => setPrepOpen(null)}
          />
        )}
      </AnimatePresence>
    </>
  )
}
