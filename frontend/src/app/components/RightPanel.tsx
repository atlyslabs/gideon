"use client"

import { memo, useState, useRef, useEffect, useCallback } from "react"
import useSWR from "swr"
import {
  ChevronRight,
  Terminal as LucideTerminal,
} from "lucide-react"

function TerminalIcon({ size = 16 }: { size?: number }) {
  return <LucideTerminal size={size} />
}

function TodoIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M14 2H6C3.794 2 2 3.794 2 6V14C2 16.206 3.794 18 6 18H14C16.206 18 18 16.206 18 14V6C18 3.794 16.206 2 14 2ZM14.284 7.621L9.534 13.621C9.351 13.852 9.076 13.991 8.781 14C8.771 14 8.76 14 8.75 14C8.467 14 8.196 13.88 8.007 13.669L5.757 11.169C5.388 10.758 5.421 10.126 5.831 9.757C6.242 9.387 6.873 9.421 7.243 9.831L8.701 11.451L12.716 6.38C13.059 5.948 13.687 5.874 14.121 6.216C14.554 6.559 14.627 7.188 14.284 7.621Z" fill="currentColor" />
    </svg>
  )
}

function MailIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M8.88,8.827c.074,.042,.166,.042,.24,0l7.777-4.283c-.314-1.173-1.376-2.044-2.647-2.044H3.75c-1.267,0-2.326,.865-2.643,2.033l7.773,4.293Z" fill="currentColor" />
      <path d="M9.845,10.14c-.264,.146-.554,.219-.844,.219s-.582-.073-.846-.22L1,6.188v6.562c0,1.517,1.233,2.75,2.75,2.75H14.25c1.517,0,2.75-1.233,2.75-2.75V6.2l-7.155,3.94Z" fill="currentColor" />
    </svg>
  )
}

function MeetingIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M7.25 3C9.12557 3 10.675 4.37763 10.9517 6.17578L12.9395 4.18945C13.3685 3.76046 14.0137 3.63209 14.5742 3.86426C15.1346 4.09647 15.5 4.64338 15.5 5.25V11.25C15.5 11.8566 15.1346 12.4035 14.5742 12.6357C14.0137 12.8679 13.3685 12.7395 12.9395 12.3105L10.9517 10.3228C10.6756 12.1216 9.12607 13.5 7.25 13.5H4.25C2.17893 13.5 0.5 11.8211 0.5 9.75V6.75C0.5 4.67893 2.17893 3 4.25 3H7.25ZM3.5 9.75C3.5 10.1642 3.83579 10.5 4.25 10.5H7.25C7.66421 10.5 8 10.1642 8 9.75V6.75C8 6.33579 7.66421 6 7.25 6H4.25C3.83579 6 3.5 6.33579 3.5 6.75V9.75Z" fill="currentColor" />
      <path d="M3.5 9.75C3.5 10.1642 3.83579 10.5 4.25 10.5H7.25C7.66421 10.5 8 10.1642 8 9.75V6.75C8 6.33579 7.66421 6 7.25 6H4.25C3.83579 6 3.5 6.33579 3.5 6.75V9.75Z" fill="currentColor" />
    </svg>
  )
}
import useWeather from "../hooks/useWeather"
import useHubData from "../hooks/useHubData"
import useSettings from "../hooks/useSettings"
import { useAvatarMap } from "../hooks/useAvatarMap"
import { pingman } from "../utils/pingman"
import type { EmailDraft } from "../types"
import {
  type Tab,
  getWeatherVideo,
  getWeatherGradient,
  DEFAULT_WIDTH,
  MIN_WIDTH,
  MAX_WIDTH,
  TAB_LABELS,
} from "./hub"
import { TodosSection, MeetingsSection, EmailsSection, DevConsoleSection } from "./hub"

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface RightPanelProps {
  open: boolean
  onClose: () => void
  width?: number
  onWidthChange?: (width: number) => void
  onDraggingChange?: (dragging: boolean) => void
  onOpenConversation?: (conversationId: string) => void
  onRefreshConversations?: () => void
  onHighlightConversation?: (conversationId: string) => void
  onResumeConversation?: (conversationId: string, userMessage: string) => void
  onStartMeetingChat?: (meetingTitle: string, prepContent: string) => void
  fullWidth?: boolean
}

// Master tab list. Each entry declares the feature flag it requires to be
// rendered (todo is always on). Tab order matches the canonical sequence:
// todo, console, emails, meetings. Disabled tabs are skipped — the row
// re-flows so there's no empty slot.
type TabSpec = {
  id: Tab
  icon: React.ComponentType<{ size?: number }>
  /** Feature flag key on UserSettings that controls visibility; undefined = always show. */
  flag?: "feature_dev_console" | "feature_email_drafts" | "feature_meeting_prep"
}

const ALL_TABS: TabSpec[] = [
  { id: "todo", icon: TodoIcon },
  { id: "console", icon: TerminalIcon, flag: "feature_dev_console" },
  { id: "emails", icon: MailIcon, flag: "feature_email_drafts" },
  { id: "meetings", icon: MeetingIcon, flag: "feature_meeting_prep" },
]

// Per-flag defaults match the Features settings card: dev_console defaults
// off, the other two default on so existing users don't lose their tabs
// before the settings request resolves.
const FLAG_DEFAULTS: Record<NonNullable<TabSpec["flag"]>, boolean> = {
  feature_dev_console: false,
  feature_email_drafts: true,
  feature_meeting_prep: true,
}

// ---------------------------------------------------------------------------
// Main component
//
// Memoized: Layout re-renders on every chat-streaming tick (30-60fps); with
// stable props the hub panel (todos/emails/meetings) skips all of them. Its
// own SWR/hub subscriptions still re-render it when hub data changes.
// ---------------------------------------------------------------------------

function RightPanel({
  open,
  onClose,
  width: controlledWidth,
  onWidthChange,
  onOpenConversation,
  onRefreshConversations,
  onHighlightConversation,
  onResumeConversation,
  onStartMeetingChat,
  onDraggingChange,
  fullWidth,
}: RightPanelProps) {
  const [activeTab, setActiveTab] = useState<Tab>("todo")
  const { settings } = useSettings()

  const TABS = ALL_TABS.filter(t => {
    if (!t.flag) return true
    const stored = settings?.[t.flag]
    return typeof stored === "boolean" ? stored : FLAG_DEFAULTS[t.flag]
  })

  // If the active tab gets disabled mid-session (user flipped a flag off in
  // Settings while the panel was open), bounce them back to To Do so the
  // panel doesn't render an empty body.
  useEffect(() => {
    if (!TABS.some(t => t.id === activeTab)) {
      setActiveTab("todo")
    }
  }, [TABS, activeTab])

  // Workspace member avatars — SWR-cached, shared with the chat banner's MeetingCard.
  const avatarMap = useAvatarMap()

  const { weather } = useWeather()
  const {
    todos,
    delegatedTodos,
    delegatedToMeTodos,
    snoozedTodos,
    suggestions,
    meetings,
    meetingsLoading,
    createTodo,
    createSubtask,
    updateTodo,
    deleteTodo,
    snoozeTodo,
    unsnoozeTodo,
    assignToGideon,
    delegateTodo,
    nudgeTodo,
    getTodoUpdates,
    addSuggestion,
    dismissSuggestion,
  } = useHubData()

  // Resize state
  const [internalWidth, setInternalWidth] = useState(DEFAULT_WIDTH)
  const [isDragging, setIsDragging] = useState(false)
  const panelWidth = controlledWidth ?? internalWidth
  const setWidth = onWidthChange ?? setInternalWidth

  // ---- Resize handle ----
  const dragCleanupRef = useRef<(() => void) | null>(null)

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      setIsDragging(true)
      onDraggingChange?.(true)
      const startX = e.clientX
      const startWidth = panelWidth

      const handleMouseMove = (e: MouseEvent) => {
        const delta = startX - e.clientX
        const newWidth = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startWidth + delta))
        setWidth(newWidth)
      }

      const handleMouseUp = () => {
        setIsDragging(false)
        onDraggingChange?.(false)
        document.removeEventListener("mousemove", handleMouseMove)
        document.removeEventListener("mouseup", handleMouseUp)
        document.body.style.cursor = ""
        document.body.style.userSelect = ""
        dragCleanupRef.current = null
      }

      // Store cleanup so unmount can call it
      dragCleanupRef.current = handleMouseUp

      document.body.style.cursor = "col-resize"
      document.body.style.userSelect = "none"
      document.addEventListener("mousemove", handleMouseMove)
      document.addEventListener("mouseup", handleMouseUp)
    },
    [panelWidth, setWidth, onDraggingChange]
  )

  // Clean up drag listeners if component unmounts mid-drag
  useEffect(() => {
    return () => { dragCleanupRef.current?.() }
  }, [])

  // Email auto-drafts
  const {
    data: emailDrafts,
    mutate: mutateDrafts,
  } = useSWR<EmailDraft[]>(
    "/api/gideon/v1/hub/email-drafts",
    async (url: string) => {
      const res = await pingman.get<{ items: EmailDraft[]; count: number }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data.items
    },
    { revalidateOnFocus: true, refreshInterval: 300_000 }
  )

  // ---- Render ----

  const videoSrc = getWeatherVideo(weather?.condition ?? "clear")

  return (
    <div
      className={`h-full flex flex-row ${fullWidth ? "flex-1" : "shrink-0"}`}
      style={{
        width: fullWidth ? "100%" : (open ? panelWidth : 0),
        transition: isDragging ? "none" : "width 300ms cubic-bezier(0.4, 0, 0.2, 1)",
        overflow: "hidden",
      }}
    >
      {/* Resize handle */}
      {open && !fullWidth && (
        <div
          onMouseDown={handleMouseDown}
          className="w-1 h-full shrink-0 cursor-col-resize group relative"
          style={{ background: "transparent" }}
        >
          <div
            className="absolute inset-y-0 left-0 w-1 transition-colors"
            style={{ background: isDragging ? "var(--accent)" : "transparent" }}
          />
          <div className="absolute inset-y-0 -left-1 w-3 group-hover:bg-[var(--accent)] transition-colors opacity-0 group-hover:opacity-30 rounded-full" />
        </div>
      )}

      <div
        className="flex-1 h-full flex flex-col rounded-xl overflow-hidden"
        style={{ position: "relative", minWidth: fullWidth ? undefined : MIN_WIDTH }}
      >
        {/* Weather backdrop. Defaults to a CSS gradient; set
            NEXT_PUBLIC_WEATHER_VIDEO_BASE to use your own video files. */}
        {videoSrc ? (
          <video
            key={videoSrc}
            className="absolute inset-0 h-full w-full object-cover pointer-events-none select-none"
            style={{ transition: "opacity 1000ms ease-in-out" }}
            src={videoSrc}
            autoPlay
            loop
            playsInline
            muted
            disablePictureInPicture
          />
        ) : (
          <div
            className="absolute inset-0 h-full w-full pointer-events-none select-none"
            style={{
              background: getWeatherGradient(weather?.condition ?? "clear"),
              transition: "background 1000ms ease-in-out",
            }}
          />
        )}

        {/* Dark overlay */}
        <div
          className="pointer-events-none absolute inset-0 select-none"
          style={{
            background: "rgba(0,0,0,0.10)",
          }}
        />

        {/* Header */}
        <div className="relative px-5 pt-5 pb-4 z-10" style={{ minHeight: 100 }}>
          <div className="flex items-start justify-between">
            <div>
              <p className="text-[12px] font-medium" style={{ color: "rgba(255,255,255,1)" }}>
                {weather ? `${weather.city} ${weather.temp}\u00B0` : ""}
              </p>
              <h2 className="text-[26px] font-bold text-white mt-0.5 leading-tight">
                {TAB_LABELS[activeTab]}
              </h2>
            </div>

            {/* Tab icon row */}
            <div
              className="relative flex items-center gap-0.5 rounded-full p-1"
              style={{ background: "rgba(0,0,0,0.35)", backdropFilter: "blur(12px)" }}
            >
              {/* Sliding indicator */}
              <div
                className="absolute rounded-full"
                style={{
                  width: 32,
                  height: 32,
                  background: "rgba(255,255,255,0.2)",
                  left: 4 + TABS.findIndex(t => t.id === activeTab) * 34,
                  transition: "left 250ms cubic-bezier(0.4, 0, 0.2, 1)",
                }}
              />
              {TABS.map((tab) => {
                const active = activeTab === tab.id
                return (
                  <button
                    key={tab.id}
                    onClick={() => setActiveTab(tab.id)}
                    aria-label={TAB_LABELS[tab.id]}
                    aria-pressed={active}
                    className="relative z-10 flex items-center justify-center w-8 h-8 rounded-full transition-colors"
                    style={{
                      color: active ? "#fff" : "rgba(255,255,255,0.6)",
                    }}
                  >
                    <tab.icon size={16} />
                  </button>
                )
              })}
            </div>
          </div>
        </div>

        {/* Content area */}
        <div className="flex-1 overflow-y-auto relative z-10">
          {activeTab === "todo" && (
            <TodosSection
              todos={todos}
              delegatedTodos={delegatedTodos}
              delegatedToMeTodos={delegatedToMeTodos}
              snoozedTodos={snoozedTodos}
              suggestions={suggestions}
              createTodo={createTodo}
              createSubtask={createSubtask}
              updateTodo={updateTodo}
              deleteTodo={deleteTodo}
              snoozeTodo={snoozeTodo}
              unsnoozeTodo={unsnoozeTodo}
              assignToGideon={assignToGideon}
              delegateTodo={delegateTodo}
              nudgeTodo={nudgeTodo}
              getTodoUpdates={getTodoUpdates}
              addSuggestion={addSuggestion}
              dismissSuggestion={dismissSuggestion}
              onOpenConversation={onOpenConversation}
              onRefreshConversations={onRefreshConversations}
              onResumeConversation={onResumeConversation}
            />
          )}

          {activeTab === "emails" && (
            <EmailsSection emailDrafts={emailDrafts} mutateDrafts={mutateDrafts} avatarMap={avatarMap} />
          )}

          {activeTab === "meetings" && (
            <MeetingsSection meetings={meetings} meetingsLoading={meetingsLoading} avatarMap={avatarMap} onStartMeetingChat={onStartMeetingChat} />
          )}

          {activeTab === "console" && (
            <DevConsoleSection />
          )}
        </div>

      </div>
    </div>
  )
}

export default memo(RightPanel)
