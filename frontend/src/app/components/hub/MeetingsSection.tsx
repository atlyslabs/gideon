"use client"

import { useState } from "react"
import { Loader2 } from "lucide-react"
import { AnimatePresence } from "framer-motion"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import type { HubMeeting } from "../../types"
import { MeetingCard, MeetingPrepModal } from "./MeetingCard"

interface MeetingsSectionProps {
  meetings: HubMeeting[]
  meetingsLoading: boolean
  avatarMap: Record<string, string>
  onStartMeetingChat?: (meetingTitle: string, prepContent: string) => void
}

export default function MeetingsSection({
  meetings,
  meetingsLoading,
  avatarMap,
  onStartMeetingChat,
}: MeetingsSectionProps) {
  const [expandedMeetingId, setExpandedMeetingId] = useState<string | null>(null)
  const [meetingPrepOpen, setMeetingPrepOpen] = useState<{ eventId: string; title: string } | null>(null)

  return (
    <>
      <div className="px-4 py-2">
        {meetingsLoading ? (
          <div className="flex flex-col items-center justify-center py-16 gap-3">
            <Loader2 size={24} className="animate-spin" style={{ color: "rgba(255,255,255,1)" }} />
            <p className="text-[13px]" style={{ color: "rgba(255,255,255,1)" }}>
              Loading meetings...
            </p>
          </div>
        ) : (!Array.isArray(meetings) || meetings.length === 0) ? (
          <div className="flex flex-col items-center justify-center py-16 gap-3">
            {getIntegrationIcon("google_calendar", 32)}
            <p className="text-[15px] font-semibold" style={{ color: "rgba(255,255,255,0.7)" }}>
              No meetings today
            </p>
            <p className="text-[12px] text-center max-w-[200px]" style={{ color: "rgba(255,255,255,1)" }}>
              Your calendar is clear. Enjoy the focus time.
            </p>
          </div>
        ) : (
          <AnimatePresence mode="popLayout">
            {(Array.isArray(meetings) ? meetings : []).map((meeting, i) => (
              <MeetingCard
                key={meeting.id}
                meeting={meeting}
                index={i}
                expanded={expandedMeetingId === meeting.id}
                avatarMap={avatarMap}
                onToggleExpand={() =>
                  setExpandedMeetingId(
                    expandedMeetingId === meeting.id ? null : meeting.id
                  )
                }
                onOpenPrep={(eventId, title) =>
                  setMeetingPrepOpen({ eventId, title })
                }
              />
            ))}
          </AnimatePresence>
        )}
      </div>

      <AnimatePresence>
        {meetingPrepOpen && (
          <MeetingPrepModal
            eventId={meetingPrepOpen.eventId}
            meetingTitle={meetingPrepOpen.title}
            onClose={() => setMeetingPrepOpen(null)}
            onStartChat={onStartMeetingChat}
          />
        )}
      </AnimatePresence>
    </>
  )
}
