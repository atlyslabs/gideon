"use client"

import { memo, useState, useRef, useCallback, useEffect } from "react"
import {
  MessageSquarePlus,
  Trash2,
  MessageSquare,
  Search,
  Link,
  Workflow,
  Sparkles,
  FolderOpen,
  BarChart3,
  CheckSquare,
  ClipboardList,
  Compass,
  Brain,
  Slash,
  Settings,
  PanelLeftClose,
  Pin,
  PinOff,
} from "lucide-react"
import type { Conversation } from "../../types"
import useUser from "../../hooks/useUser"
import useGideonUser from "../../hooks/useGideonUser"
import { useStreamingStatus, markConversationRead } from "../../hooks/useChatStream"
import { pingman } from "../../utils/pingman"
import { isAdminEmail } from "../../utils/isAdmin"

type SidebarMode = "expanded" | "icons" | "hidden"

interface ChatSidebarProps {
  conversations: Conversation[]
  activeId: string | null
  onSelect: (id: string) => void
  onCreate: () => void
  onDelete: (id: string) => void
  onRename?: (id: string, title: string) => void
  onTogglePin?: (id: string, nextPinned: boolean) => void
  mode: SidebarMode
  sidebarWidth?: number
  onWidthChange?: (width: number) => void
  onToggle: () => void
  onNavigate?: (page: string) => void
  isMobile?: boolean
  integrationErrorCount?: number
  highlightConversationId?: string | null
}

const NAV_ITEMS: Array<{
  icon: typeof MessageSquarePlus
  label: string
  id: string
  shortcut?: string
  adminOnly?: boolean
  mobileOnly?: boolean
  surveyAdminOnly?: boolean
}> = [
  { icon: MessageSquarePlus, label: "New Chat", id: "new-chat" },
  { icon: Search, label: "Search", id: "search", shortcut: "⌘ K" },
  { icon: CheckSquare, label: "To Do", id: "todo", mobileOnly: true },
  { icon: Compass, label: "Explore", id: "explore" },
  { icon: Link, label: "Integrations", id: "integrations" },
  { icon: Workflow, label: "Workflows", id: "workflows" },
  { icon: ClipboardList, label: "Surveys", id: "surveys", surveyAdminOnly: true },
  { icon: Sparkles, label: "Skills", id: "skills" },
  { icon: Slash, label: "Commands", id: "commands" },
  { icon: FolderOpen, label: "Files", id: "files" },
  { icon: Brain, label: "Memory", id: "memory" },
  { icon: BarChart3, label: "Usage", id: "usage", adminOnly: true },
]
// ---------------------------------------------------------------------------
// Main Sidebar
//
// Memoized: Layout re-renders on every chat-streaming tick (30-60fps); with
// stable props the sidebar (full conversation list) skips all of them. It
// still re-renders on its own subscriptions (useStreamingStatus, SWR hooks)
// and whenever conversations/activeId/mode/etc. actually change.
// ---------------------------------------------------------------------------

function ChatSidebar({
  conversations,
  activeId,
  onSelect,
  onCreate,
  onDelete,
  onRename,
  onTogglePin,
  mode,
  sidebarWidth,
  onWidthChange,
  onToggle,
  onNavigate,
  isMobile,
  integrationErrorCount,
  highlightConversationId,
}: ChatSidebarProps) {
  const { user } = useUser()
  const { gideonUser } = useGideonUser()
  const isAdmin = isAdminEmail(user?.email)
  const isSurveyAdmin = gideonUser?.is_survey_admin === true
  const { streamingIds: localStreamingIds, readyIds } = useStreamingStatus()
  // Merge local (this tab) + backend (other tabs / devices) streaming state
  // so the sparkle lights up for any conversation that has an active
  // background producer — regardless of which tab started it.
  const streamingIds = (() => {
    const s = new Set(localStreamingIds)
    for (const c of conversations) {
      if (c.active_turn_id) s.add(c.id)
    }
    return s
  })()

  const defaultWidth = mode === "expanded" ? 240 : mode === "icons" ? 64 : 0
  const width = mode === "expanded" ? (sidebarWidth || 240) : defaultWidth
  const showLabels = mode === "expanded"

  const [tooltip, setTooltip] = useState<{ label: string; top: number; left: number } | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingTitle, setEditingTitle] = useState("")
  const editInputRef = useRef<HTMLInputElement>(null)
  const [isResizing, setIsResizing] = useState(false)
  const resizeRef = useRef<{ startX: number; startWidth: number } | null>(null)

  // Drag resize handler
  useEffect(() => {
    if (!isResizing) return
    const handleMouseMove = (e: MouseEvent) => {
      if (!resizeRef.current) return
      const delta = e.clientX - resizeRef.current.startX
      const raw = resizeRef.current.startWidth + delta
      if (raw < 120) {
        // Snap to icons mode if dragged too narrow
        setIsResizing(false)
        onToggle()
        return
      }
      const newWidth = Math.max(180, Math.min(400, raw))
      onWidthChange?.(newWidth)
    }
    const handleMouseUp = () => setIsResizing(false)
    document.addEventListener("mousemove", handleMouseMove)
    document.addEventListener("mouseup", handleMouseUp)
    document.body.style.cursor = "col-resize"
    document.body.style.userSelect = "none"
    return () => {
      document.removeEventListener("mousemove", handleMouseMove)
      document.removeEventListener("mouseup", handleMouseUp)
      document.body.style.cursor = ""
      document.body.style.userSelect = ""
    }
  }, [isResizing, onWidthChange])

  const showTooltip = useCallback((e: React.MouseEvent, label: string) => {
    if (showLabels) return
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
    setTooltip({ label, top: rect.top + rect.height / 2, left: rect.right + 8 })
  }, [showLabels])

  const hideTooltip = useCallback(() => setTooltip(null), [])

  return (
    <div
      className="h-full shrink-0 relative"
      style={{
        width,
        background: "#000000",
        transition: isResizing ? "none" : "width 300ms cubic-bezier(0.4, 0, 0.2, 1)",
      }}
    >
      <div className="h-full flex flex-col overflow-hidden">
      <div className="flex flex-col h-full" style={{ minWidth: mode === "icons" ? 64 : undefined }}>
        {/* Top: Logo (hover reveals toggle in icons mode) + toggle button in expanded */}
        <div className={`flex items-center ${showLabels ? "justify-between" : "justify-center"} px-3 pt-3 pb-1`}>
          <div className="relative group">
            <img
              src="/logo.png"
              alt="Gideon"
              className={`w-8 h-8 rounded-lg cursor-pointer transition-opacity ${!showLabels ? "group-hover:opacity-0" : ""}`}
              onClick={!showLabels ? onToggle : undefined}
            />
            {!showLabels && (
              <button
                onClick={onToggle}
                className="absolute inset-0 w-8 h-8 rounded-lg flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                style={{ background: "var(--surface-elevated)", color: "var(--text-primary)" }}
              >
                <PanelLeftClose size={16} />
              </button>
            )}
          </div>
          {showLabels && (
            <button
              onClick={onToggle}
              className="p-1.5 rounded-lg transition-colors"
              style={{ color: "var(--text-tertiary)" }}
            >
              <PanelLeftClose size={16} />
            </button>
          )}
        </div>

        {/* Nav items */}
        <nav className="px-2 py-2 space-y-0.5">
          {NAV_ITEMS.filter((item) =>
            (!item.adminOnly || isAdmin) &&
            (!item.surveyAdminOnly || isSurveyAdmin) &&
            (!item.mobileOnly || isMobile)
          ).map((item) => (
            <button
              key={item.id}
              onClick={() => {
                if (item.id === "new-chat") onCreate()
                else onNavigate?.(item.id)
              }}
              className={`w-full flex items-center ${showLabels ? "gap-3 px-3" : "justify-center"} py-2 rounded-lg text-[13px] transition-colors`}
              style={{ color: "var(--text-primary)" }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "var(--surface-elevated)"
                showTooltip(e, item.label)
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "transparent"
                hideTooltip()
              }}
            >
              <div className="relative">
                <item.icon size={18} />
                {item.id === "integrations" && integrationErrorCount != null && integrationErrorCount > 0 && (
                  <span
                    className="absolute -top-1 -right-1.5 w-2 h-2 rounded-full"
                    style={{ background: "#EF4444" }}
                  />
                )}
              </div>
              {showLabels && (
                <span className="flex-1 text-left flex items-center gap-2">
                  {item.label}
                  {item.id === "integrations" && integrationErrorCount != null && integrationErrorCount > 0 && (
                    <span
                      className="min-w-[18px] h-[18px] flex items-center justify-center rounded-full text-[10px] font-bold px-1"
                      style={{ background: "#EF4444", color: "#fff" }}
                    >
                      {integrationErrorCount > 9 ? "9+" : integrationErrorCount}
                    </span>
                  )}
                </span>
              )}
              {showLabels && item.shortcut && (
                <span
                  className="text-[10px] flex items-center gap-0.5 px-1.5 py-0.5 rounded"
                  style={{ background: "var(--surface-elevated)", color: "var(--text-tertiary)" }}
                >
                  {item.shortcut}
                </span>
              )}
            </button>
          ))}
        </nav>

        {/* Divider + recent chats — only in expanded */}
        {showLabels && (
          <>
            <div className="mx-4 my-3" style={{ borderTop: "1px solid var(--border)" }} />
            <div className="flex-1 overflow-y-auto px-2 pb-2">
              {conversations.length === 0 ? (
                <p className="text-[11px] text-center mt-4 px-4" style={{ color: "var(--text-tertiary)" }}>
                  No conversations yet
                </p>
              ) : (
                conversations.map((conv, idx) => {
                  // Group-heading injection: API returns pinned-first, so the boundary is where
                  // a pinned row is immediately followed by an unpinned row.
                  const prev = idx > 0 ? conversations[idx - 1] : null
                  const showPinnedHeader = idx === 0 && conv.is_pinned
                  const showRecentHeader = conv.is_pinned === false && (prev?.is_pinned === true || idx === 0)
                  return (
                  <div key={conv.id + "-group"}>
                  {showPinnedHeader && (
                    <p className="text-[10px] font-semibold tracking-widest uppercase mt-1 mb-1 px-3" style={{ color: "var(--text-tertiary)" }}>Pinned</p>
                  )}
                  {showRecentHeader && idx > 0 && (
                    <p className="text-[10px] font-semibold tracking-widest uppercase mt-3 mb-1 px-3" style={{ color: "var(--text-tertiary)" }}>Recent</p>
                  )}
                  <div
                    data-conversation-id={conv.id}
                    onClick={() => {
                      if (editingId !== conv.id) {
                        markConversationRead(conv.id)
                        onSelect(conv.id)
                      }
                    }}
                    className="group flex items-center gap-2 px-3 py-2 rounded-lg mb-0.5 cursor-pointer transition-colors"
                    style={{
                      background: conv.id === activeId ? "var(--surface-hover)" : "transparent",
                      borderLeft: conv.id === activeId ? "2px solid var(--accent)" : "2px solid transparent",
                      animation: conv.id === highlightConversationId ? "conv-blink 1.5s ease-in-out" : undefined,
                    }}
                    onMouseEnter={(e) => {
                      if (conv.id !== activeId) e.currentTarget.style.background = "var(--surface-elevated)"
                    }}
                    onMouseLeave={(e) => {
                      if (conv.id !== activeId) e.currentTarget.style.background = "transparent"
                    }}
                  >
                    {/* Streaming / ready indicator */}
                    {streamingIds.has(conv.id) ? (
                      <svg
                        width={14}
                        height={14}
                        viewBox="0 0 24 24"
                        fill="none"
                        className="shrink-0 streaming-sparkle"
                        aria-hidden
                      >
                        <path
                          d="M12 2L14.5 9.5L22 12L14.5 14.5L12 22L9.5 14.5L2 12L9.5 9.5L12 2Z"
                          fill="var(--accent)"
                        />
                      </svg>
                    ) : readyIds.has(conv.id) ? (
                      <span className="shrink-0 w-2 h-2 rounded-full" style={{ background: "#3b82f6" }} />
                    ) : null}
                    {editingId === conv.id ? (
                      <input
                        ref={editInputRef}
                        value={editingTitle}
                        onChange={(e) => setEditingTitle(e.target.value)}
                        onBlur={() => {
                          if (editingTitle.trim() && editingTitle.trim() !== (conv.title || "").replace(/^#\s*/, "")) {
                            onRename?.(conv.id, editingTitle.trim())
                          }
                          setEditingId(null)
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") (e.target as HTMLInputElement).blur()
                          if (e.key === "Escape") setEditingId(null)
                        }}
                        onClick={(e) => e.stopPropagation()}
                        className="flex-1 bg-transparent outline-none"
                        style={{ fontSize: "var(--font-size-sidebar)", color: "var(--text-primary)", caretColor: "var(--accent)" }}
                        autoFocus
                      />
                    ) : (
                      <p
                        className="truncate flex-1"
                        style={{ fontSize: "var(--font-size-sidebar)", color: conv.id === activeId ? "var(--text-primary)" : "var(--text-secondary)" }}
                        onDoubleClick={(e) => {
                          e.stopPropagation()
                          setEditingId(conv.id)
                          setEditingTitle((conv.title || "New conversation").replace(/^#\s*/, ""))
                          setTimeout(() => editInputRef.current?.select(), 50)
                        }}
                      >
                        {(conv.title || "New conversation").replace(/^#\s*/, "")}
                      </p>
                    )}
                    {onTogglePin && (
                      <button
                        onClick={(e) => { e.stopPropagation(); onTogglePin(conv.id, !conv.is_pinned) }}
                        className={`group/pin p-1 rounded transition-opacity ${
                          conv.is_pinned
                            ? "opacity-100"
                            : "opacity-100 md:opacity-0 md:group-hover:opacity-100"
                        }`}
                        style={{ color: conv.is_pinned ? "var(--accent)" : "var(--text-tertiary)" }}
                        title={conv.is_pinned ? "Unpin" : "Pin"}
                      >
                        {conv.is_pinned ? (
                          <>
                            <Pin size={12} className="group-hover/pin:hidden" />
                            <PinOff size={12} className="hidden group-hover/pin:block" />
                          </>
                        ) : (
                          <Pin size={12} />
                        )}
                      </button>
                    )}
                    <button
                      onClick={(e) => { e.stopPropagation(); onDelete(conv.id) }}
                      className="opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity p-1 rounded"
                      style={{ color: "var(--text-tertiary)" }}
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                  </div>
                  )
                })
              )}
            </div>
          </>
        )}

        {/* Spacer for icons mode */}
        {!showLabels && <div className="flex-1" />}

        {/* Settings at bottom */}
        <div className={`${showLabels ? "px-2" : "px-2"} pb-3 mt-auto`}>
          <button
            onClick={() => onNavigate?.("settings")}
            className={`w-full flex items-center ${showLabels ? "gap-3 px-3" : "justify-center"} py-2 rounded-lg text-[13px] transition-colors`}
            style={{ color: "var(--text-secondary)" }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = "var(--surface-elevated)"
              showTooltip(e, "Settings")
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "transparent"
              hideTooltip()
            }}
          >
            <Settings size={18} />
            {showLabels && <span>Settings</span>}
          </button>
        </div>
      </div>

      {/* Fixed tooltip portal — renders outside overflow-hidden */}
      {tooltip && (
        <div
          className="fixed px-2.5 py-1.5 rounded-lg text-[12px] font-medium whitespace-nowrap pointer-events-none z-[9999]"
          style={{
            top: tooltip.top,
            left: tooltip.left,
            transform: "translateY(-50%)",
            background: "var(--surface-elevated)",
            color: "var(--text-primary)",
            border: "1px solid var(--border)",
            boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
          }}
        >
          {tooltip.label}
        </div>
      )}

      {/* Close inner flex container */}
      </div>

      {/* Resize drag handle — right edge */}
      {mode === "expanded" && (
        <div
          className="absolute top-0 right-0 w-1 h-full cursor-col-resize z-10 group"
          onMouseDown={(e) => {
            e.preventDefault()
            resizeRef.current = { startX: e.clientX, startWidth: width }
            setIsResizing(true)
          }}
        >
          <div
            className="w-full h-full transition-colors"
            style={{ background: isResizing ? "var(--accent)" : "transparent" }}
            onMouseEnter={(e) => { if (!isResizing) (e.currentTarget as HTMLElement).style.background = "var(--border)" }}
            onMouseLeave={(e) => { if (!isResizing) (e.currentTarget as HTMLElement).style.background = "transparent" }}
          />
        </div>
      )}
    </div>
  )
}

export default memo(ChatSidebar)
