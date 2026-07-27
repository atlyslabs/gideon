"use client"

import { useState, useCallback, useEffect, useMemo, useRef } from "react"
import { useTokenStore } from "../hooks/useTokenStore"
import useUser from "../hooks/useUser"
import useConversations from "../hooks/useConversations"
import useMessages from "../hooks/useMessages"
import useChatStream, { markConversationRead } from "../hooks/useChatStream"
import useSuggestedReply from "../hooks/useSuggestedReply"
import { pingman } from "../utils/pingman"
import ChatSidebar from "./chat/ChatSidebar"
import ChatMessages from "./chat/ChatMessages"
import ChatInput from "./chat/ChatInput"
import RightPanel from "./RightPanel"
import RunningWorkflowsIndicator from "./RunningWorkflowsIndicator"
import useIntegrations from "../hooks/useIntegrations"
import useGideonUser from "../hooks/useGideonUser"
import useSettings from "../hooks/useSettings"
import IntegrationsPage from "./IntegrationsPage"
import SkillsPage from "./SkillsPage"
import CustomCommandsPage from "./CustomCommandsPage"
import SkillDetailPage from "./SkillDetailPage"
import WorkflowsPage from "./WorkflowsPage"
import WorkflowDetailPage from "./WorkflowDetailPage"
import WorkflowRunDetailPage from "./WorkflowRunDetailPage"
import type { Skill, Workflow } from "../types"
import SettingsPage from "./SettingsPage"
import MemoryGraphPage from "./MemoryGraphPage"
import FilesPage from "./FilesPage"
import ExplorePage from "./ExplorePage"
import UsagePage from "./UsagePage"
import SearchModal from "./SearchModal"
import SurveyModal from "./SurveyModal"
import SurveyBuilderPage from "./SurveyBuilderPage"
import UpdateAvailableBanner from "./UpdateAvailableBanner"
import useSurvey from "../hooks/useSurvey"
import useVersionCheck from "../hooks/useVersionCheck"
import ShareDialog from "./chat/ShareDialog"
import MobileBottomNav from "./MobileBottomNav"
import { Share2, CheckSquare, ChevronRight, ArrowLeft, MessageSquare } from "lucide-react"
import useIsMobile from "../hooks/useIsMobile"
import useAppRouter from "../hooks/useAppRouter"
import { isAdminEmail } from "../utils/isAdmin"

interface AppLayoutProps {
  initialConversationId?: string | null
}

export default function AppLayout({ initialConversationId }: AppLayoutProps = {}) {
  const isMobile = useIsMobile()
  const router = useAppRouter()
  const activePage = router.page
  const { user } = useUser()
  const { gideonUser } = useGideonUser()
  const authToken = useTokenStore((s) => s.authToken)
  const { integrations } = useIntegrations()
  const connectedProviders = useMemo(
    () => integrations.filter((i) => i.is_active).map((i) => i.provider),
    [integrations]
  )
  const integrationErrorCount = integrations.filter((i) => i.error_count > 0 || !!i.last_error).length
  const [sidebarMode, setSidebarMode] = useState<"expanded" | "icons" | "hidden">(isMobile ? "icons" : "expanded")
  const [rightPanelOpen, setRightPanelOpen] = useState(!isMobile)
  const [rightPanelWidth, setRightPanelWidth] = useState(320)
  const [rightPanelDragging, setRightPanelDragging] = useState(false)
  const [activeConversationId, setActiveConversationIdRaw] = useState<string | null>(initialConversationId || router.conversationId || null)
  const [shareDialogOpen, setShareDialogOpen] = useState(false)
  const [isCreatingSkill, setIsCreatingSkill] = useState(false)
  const [isCreatingWorkflow, setIsCreatingWorkflow] = useState(false)
  const [sidebarWidth, setSidebarWidth] = useState(240)
  const [searchOpen, setSearchOpen] = useState(false)
  const [highlightConversationId, setHighlightConversationId] = useState<string | null>(null)
  const [autoMode, setAutoMode] = useState(() => {
    if (typeof window !== "undefined") return localStorage.getItem("gideon_auto_mode") === "1"
    return false
  })
  const handleAutoModeChange = useCallback((on: boolean) => {
    setAutoMode(on)
    localStorage.setItem("gideon_auto_mode", on ? "1" : "0")
  }, [])
  const bottomInputRef = useRef<HTMLDivElement>(null)
  const tzSent = useRef(false)

  // Derive skill/workflow sub-IDs from URL
  const selectedSkillId = activePage === "skills" ? router.subId : null
  const selectedWorkflowId = activePage === "workflows" ? router.subId : null
  // /workflows/{id}/runs/{run_id} — only valid when both ids are present.
  const selectedWorkflowRunId =
    activePage === "workflows" && selectedWorkflowId ? router.tertiaryId : null

  // Wrap setActiveConversationId to also update URL
  const setActiveConversationId = useCallback((id: string | null) => {
    setActiveConversationIdRaw(id)
    router.replaceConversation(id)
    if (id) markConversationRead(id)
  }, [router])

  // Sync sidebar/panel state with screen size
  useEffect(() => {
    if (isMobile) {
      setSidebarMode("icons")
      setRightPanelOpen(false)
    }
  }, [isMobile])


  // Pending survey (blocks chat until answered or skipped)
  const { survey: pendingSurvey, submit: submitSurvey } = useSurvey()

  // Detect when a new backend has rolled out so we can offer a refresh.
  const { outdated: newVersionAvailable } = useVersionCheck()

  // Bounce non-admins off /surveys (admin-only page). Wait for gideonUser to
  // load so we don't redirect during the SWR flicker.
  useEffect(() => {
    if (activePage !== "surveys") return
    if (gideonUser === undefined) return  // still loading
    if (gideonUser?.is_survey_admin) return
    router.navigateToPage("chat")
  }, [activePage, gideonUser, router])

  // Auto-detect and send browser timezone only if not already set
  const { settings: userSettings } = useSettings()
  useEffect(() => {
    if (tzSent.current || !authToken || !userSettings) return
    if (userSettings.timezone && userSettings.timezone !== "UTC") return
    tzSent.current = true
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
    if (tz) {
      pingman.patch("/api/gideon/v1/users/me/settings", { timezone: tz }).catch(() => {})
    }
  }, [authToken, userSettings])

  // Sync conversation ID from URL when browser navigates back to a chat
  useEffect(() => {
    if (router.page === "chat" && router.conversationId && router.conversationId !== activeConversationId) {
      setActiveConversationIdRaw(router.conversationId)
      markConversationRead(router.conversationId)
      resumeStream(router.conversationId)
    }
  }, [router.page, router.conversationId])

  const toggleSidebar = useCallback(() => {
    if (isMobile) return
    setSidebarMode((prev) => (prev === "expanded" ? "icons" : "expanded"))
  }, [isMobile])

  const { conversations, deleteConversation, togglePin, mutate: mutateConversations } = useConversations()
  const { messages, mutate: mutateMessages } = useMessages(activeConversationId)

  // Stable identity — passed to the memoized ChatSidebar / useChatStream.
  // An inline lambda here would invalidate handleSendMessage (and everything
  // downstream) on every streaming render tick.
  const handleTitleUpdate = useCallback(
    (conversationId: string, title: string) => {
      mutateConversations((convs) =>
        convs?.map((c) =>
          c.id === conversationId ? { ...c, title } : c
        ),
        { revalidate: false }
      )
    },
    [mutateConversations]
  )

  // Browser tab title
  useEffect(() => {
    const pageLabels: Record<string, string> = { skills: "Skills", workflows: "Workflows", settings: "Settings", integrations: "Integrations", memory: "Memory", files: "Files", usage: "Usage", commands: "Commands", explore: "Explore", todo: "To Do" }
    if (activePage !== "chat" && pageLabels[activePage]) {
      document.title = `${pageLabels[activePage]} - Gideon`
    } else {
      const chatTitle = activeConversationId ? conversations.find((c) => c.id === activeConversationId)?.title : null
      document.title = chatTitle ? `${chatTitle} - Gideon` : "Gideon - AI Work Assistant"
    }
  }, [activeConversationId, conversations, activePage])

  // Chat streaming hook
  const {
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
  } = useChatStream({
    authToken,
    activeConversationId,
    onConversationCreated: setActiveConversationId,
    onTitleUpdate: handleTitleUpdate,
    mutateMessages,
    mutateConversations,
  })

  // Watch SWR messages and sync into rowsRef
  useEffect(() => {
    if (messages.length > 0) syncDbMessages(messages)
  }, [messages, syncDbMessages])

  // Predicted next-reply ghost text — fetched once each time streaming
  // finishes for the active conversation. Tab in ChatInput accepts it.
  const { suggestion: replySuggestion, clearSuggestion: clearReplySuggestion } =
    useSuggestedReply(activeConversationId, isStreaming)

  // Auto-attach to an in-flight background turn when the user opens a
  // conversation whose producer is still generating (page refresh /
  // cross-device / multi-tab). Side effect: the Redis tail replays from
  // the start so the user sees the turn unfold exactly as the original
  // tab would have.
  useEffect(() => {
    if (!activeConversationId) return
    if (isStreaming) return
    const conv = conversations.find((c) => c.id === activeConversationId)
    if (!conv?.active_turn_id) return
    handleAttachToTurn(conv.active_turn_id, activeConversationId)
  }, [activeConversationId, conversations, handleAttachToTurn, isStreaming])

  const handleNewChat = useCallback(() => {
    setActiveConversationId(null)
    resetChat()
    setMobileShowConversations(false)
  }, [resetChat])

  const handleRenameConversation = useCallback(
    async (id: string, title: string) => {
      try {
        await pingman.patch(`/api/gideon/v1/chat/conversations/${id}`, { title })
        mutateConversations()
      } catch {}
    },
    [mutateConversations]
  )

  const handleDeleteConversation = useCallback(
    async (id: string) => {
      await deleteConversation(id)
      // Wipe the streamStore entry for this conv so its leftover rows don't
      // bleed into the next view (otherwise the deleted chat's last user
      // message can render above the empty/home state).
      clearConversation(id)
      if (activeConversationId === id) {
        setActiveConversationId(null)
        resetChat()
      }
    },
    [deleteConversation, clearConversation, activeConversationId, resetChat]
  )

  // Resume conversation (assign-to-Gideon)
  const handleResumeConversation = useCallback(
    (conversationId: string, _userMessage: string) => {
      setActiveConversationId(conversationId)
      if (router.page !== "chat") router.navigateToPage("chat")
      handleSendMessage("", undefined, { resume: true, conversationId })
    },
    [setActiveConversationId, router, handleSendMessage]
  )

  // Start new chat from meeting prep
  const handleStartMeetingChat = useCallback(
    (meetingTitle: string, prepContent: string) => {
      const prompt = `Help me prepare for my meeting: "${meetingTitle}"\n\nHere's what I have so far:\n\n${prepContent}\n\nWhat questions should I be ready for? Key talking points I should bring up?`
      resetChat()
      setActiveConversationId(null)
      setMobileShowConversations(false)
      if (router.page !== "chat") router.navigateToPage("chat")
      handleSendMessage(prompt, undefined, { newConversation: true })
    },
    [resetChat, handleSendMessage, router]
  )

  // Auto-send prompt from URL query param (e.g. /chat?prompt=hello)
  const promptSentRef = useRef(false)
  useEffect(() => {
    if (promptSentRef.current) return
    const params = new URLSearchParams(window.location.search)
    const prompt = params.get("prompt")
    if (prompt) {
      promptSentRef.current = true
      window.history.replaceState(null, "", window.location.pathname)
      resetChat()
      setActiveConversationId(null)
      handleSendMessage(prompt, undefined, { newConversation: true })
    }
  }, [handleSendMessage, resetChat])

  // Cmd+K to open search, Cmd+/ to toggle panel
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault()
        setSearchOpen((prev) => !prev)
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "/") {
        e.preventDefault()
        setRightPanelOpen((prev) => !prev)
      }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [])

  const handleSelectConversation = useCallback(
    (id: string) => {
      if (id !== activeConversationId) {
        setActiveConversationId(id)
        resumeStream(id)  // triggers SWR refresh; live stream rows already in store
      }
      if (router.page !== "chat") router.navigateToPage("chat")
      setMobileShowConversations(false)
    },
    [activeConversationId, resumeStream, router]
  )

  // Mobile: track whether we're viewing the conversation list vs active chat
  const [mobileShowConversations, setMobileShowConversations] = useState(false)

  const handleNavigate = useCallback((page: string) => {
    if (page === "search") {
      setSearchOpen(true)
      return
    }
    if (page === "todo") {
      if (isMobile) {
        router.navigateToPage("todo")
      } else {
        setRightPanelOpen(true)
        router.navigateToPage("chat")
      }
      return
    }
    if (page === "chat" && isMobile) {
      if (activePage === "chat" && !mobileShowConversations && activeConversationId) {
        setMobileShowConversations(true)
      } else {
        setMobileShowConversations(false)
        router.navigateToPage("chat")
      }
      return
    }
    router.navigateToPage(page as any)
    setMobileShowConversations(false)
    if (page === "skills") {
      setIsCreatingSkill(false)
    }
    if (page === "workflows") {
      setIsCreatingWorkflow(false)
    }
  }, [isMobile, activePage, mobileShowConversations, activeConversationId, router])

  const firstName = user?.first_name || user?.email?.split("@")[0] || ""

  const highlightAndClear = useCallback((id: string) => {
    setHighlightConversationId(id)
    setTimeout(() => setHighlightConversationId(null), 2000)
  }, [])

  // Stable callbacks for the memoized ChatSidebar / RightPanel. Inline
  // lambdas in their JSX would defeat React.memo on every Layout render
  // (which happens 30-60x/s during chat streaming).
  const handleCreateChat = useCallback(() => {
    handleNewChat()
    router.navigateToPage("chat")
  }, [handleNewChat, router])

  const handleTogglePin = useCallback(
    (id: string, next: boolean) => { togglePin(id, next) },
    [togglePin]
  )

  const closeRightPanel = useCallback(() => setRightPanelOpen(false), [])

  const handleCloseTodoPage = useCallback(() => router.navigateToPage("chat"), [router])

  const handleOpenConversationFromTodo = useCallback(
    (id: string) => {
      // handleSelectConversation already navigates to /chat when not there;
      // a second navigateToPage("chat") pushed a redundant history entry.
      handleSelectConversation(id)
    },
    [handleSelectConversation]
  )

  // Mobile conversation list component
  const mobileConversationList = (
    <div className="flex-1 flex flex-col min-w-0 overflow-hidden" style={{ background: "var(--surface)" }}>
      <div className="flex items-center justify-between px-4 py-3 shrink-0" style={{ borderBottom: "1px solid var(--border)" }}>
        <h1 className="text-[18px] font-semibold" style={{ color: "var(--text-primary)" }}>Chats</h1>
        <button
          onClick={() => { handleNewChat(); setMobileShowConversations(false) }}
          className="p-2 rounded-lg"
          style={{ color: "#a78bfa" }}
        >
          <MessageSquare size={20} />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto">
        {conversations.length === 0 ? (
          <p className="text-[13px] text-center mt-8 px-4" style={{ color: "var(--text-tertiary)" }}>
            No conversations yet. Start a new chat!
          </p>
        ) : (
          conversations.map((conv) => (
            <button
              key={conv.id}
              onClick={() => handleSelectConversation(conv.id)}
              className="w-full flex items-center gap-3 px-4 py-3 text-left transition-colors active:bg-white/5"
              style={{
                background: conv.id === activeConversationId ? "var(--surface-hover)" : "transparent",
                borderBottom: "1px solid var(--border)",
              }}
            >
              <MessageSquare size={18} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
              <span className="truncate text-[14px]" style={{ color: "var(--text-primary)" }}>
                {(conv.title || "New conversation").replace(/^#\s*/, "")}
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  )

  return (
    <div className="h-screen flex" style={{ background: "#000000" }}>
      {/* Desktop sidebar */}
      {!isMobile && (
        <ChatSidebar
          conversations={conversations}
          activeId={activeConversationId}
          onSelect={handleSelectConversation}
          onCreate={handleCreateChat}
          onDelete={handleDeleteConversation}
          onRename={handleRenameConversation}
          onTogglePin={handleTogglePin}
          mode={sidebarMode}
          sidebarWidth={sidebarWidth}
          onWidthChange={setSidebarWidth}
          onToggle={toggleSidebar}
          onNavigate={handleNavigate}
          isMobile={false}
          integrationErrorCount={integrationErrorCount}
          highlightConversationId={highlightConversationId}
        />
      )}

      <div className={`flex-1 flex ${isMobile ? "flex-col" : "gap-2 p-2 pl-0"} min-w-0`} style={isMobile ? { paddingBottom: "calc(56px + env(safe-area-inset-bottom, 8px))" } : undefined}>
        {/* Pages that overlay chat -- rendered when active */}
        {activePage === "todo" && isMobile && (
          <RightPanel
            open={true}
            fullWidth
            onClose={handleCloseTodoPage}
            onOpenConversation={handleOpenConversationFromTodo}
            onRefreshConversations={mutateConversations}
            onHighlightConversation={highlightAndClear}
            onResumeConversation={handleResumeConversation}
            onStartMeetingChat={handleStartMeetingChat}
          />
        )}
        {activePage === "settings" && (
          <SettingsPage onBack={() => router.navigateToPage("chat")} />
        )}
        {activePage === "usage" && isAdminEmail(user?.email) && (
          <UsagePage onBack={() => router.navigateToPage("chat")} />
        )}
        {activePage === "memory" && (
          <MemoryGraphPage onBack={() => router.navigateToPage("chat")} />
        )}
        {activePage === "files" && (
          <FilesPage onBack={() => router.navigateToPage("chat")} onOpenConversation={handleSelectConversation} />
        )}
        {activePage === "commands" && (
          <CustomCommandsPage />
        )}
        {activePage === "surveys" && gideonUser?.is_survey_admin && (
          <SurveyBuilderPage onBack={() => router.navigateToPage("chat")} />
        )}
        {activePage === "explore" && (
          <ExplorePage
            onBack={() => router.navigateToPage("chat")}
            onStartPrompt={(prompt) => {
              router.navigate(`/?prompt=${encodeURIComponent(prompt)}`)
            }}
            onOpenWorkflows={() => router.navigateToPage("workflows")}
          />
        )}
        {activePage === "integrations" && (
          <IntegrationsPage onBack={() => router.navigateToPage("chat")} />
        )}
        {activePage === "skills" && (selectedSkillId || isCreatingSkill) && (
          <SkillDetailPage
            skillId={selectedSkillId}
            onBack={() => { router.navigate("/skills"); setIsCreatingSkill(false) }}
          />
        )}
        {activePage === "skills" && !(selectedSkillId || isCreatingSkill) && (
          <SkillsPage
            onBack={() => router.navigateToPage("chat")}
            onSelectSkill={(skill: Skill) => router.navigate(`/skills/${skill.id}`)}
            onCreateSkill={() => { setIsCreatingSkill(true); router.navigate("/skills") }}
          />
        )}
        {activePage === "workflows" && selectedWorkflowId && selectedWorkflowRunId && (
          <WorkflowRunDetailPage
            workflowId={selectedWorkflowId}
            runId={selectedWorkflowRunId}
            onBack={() => router.navigate(`/workflows/${selectedWorkflowId}`)}
          />
        )}
        {activePage === "workflows" && (selectedWorkflowId || isCreatingWorkflow) && !selectedWorkflowRunId && (
          <WorkflowDetailPage
            workflowId={selectedWorkflowId}
            onBack={() => { router.navigate("/workflows"); setIsCreatingWorkflow(false) }}
          />
        )}
        {activePage === "workflows" && !(selectedWorkflowId || isCreatingWorkflow) && (
          <WorkflowsPage
            onBack={() => router.navigateToPage("chat")}
            onSelectWorkflow={(workflow: Workflow) => router.navigate(`/workflows/${workflow.id}`)}
            onCreateWorkflow={() => { setIsCreatingWorkflow(true); router.navigate("/workflows") }}
          />
        )}
        {activePage === "chat" && isMobile && mobileShowConversations && mobileConversationList}

        {/* Chat — always mounted so streaming persists across page switches.
            Uses display:contents when visible (children participate in parent flex directly)
            and display:none when hidden (removed from layout). */}
        <div className={activePage !== "chat" || (isMobile && mobileShowConversations) ? "hidden" : "contents"}>
          <div
            className={`relative flex-1 flex flex-col min-w-0 overflow-hidden ${isMobile ? "" : "rounded-2xl"}`}
            style={{ background: "var(--surface)", border: isMobile ? "none" : "1px solid rgba(255,255,255,0.06)" }}
          >
            {/* Running-workflows indicator — anchored to the chat pane, bottom-right.
                Gated on chat view so it unmounts (and stops polling) elsewhere. */}
            {activePage === "chat" && !isMobile && <RunningWorkflowsIndicator />}
            {/* Chat header */}
            {isMobile && (
              <div className="flex items-center justify-between px-3 py-2 shrink-0">
                <button
                  onClick={() => setMobileShowConversations(true)}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[13px] font-medium transition-colors active:bg-white/5"
                  style={{ color: "var(--text-secondary)" }}
                >
                  <MessageSquare size={16} />
                  Chats
                </button>
                {rows.length > 0 && (
                  <button
                    onClick={() => setShareDialogOpen(true)}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs transition-colors active:bg-white/5"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    <Share2 size={13} />
                    Share
                  </button>
                )}
              </div>
            )}
            {!isMobile && rows.length > 0 && (
              <div className="flex items-center justify-between px-4 py-2 shrink-0">
                <span className="text-sm truncate flex-1" style={{ color: "var(--text-tertiary)", transition: "opacity 0.3s ease" }}>
                  {(activeConversationId && conversations.find((c) => c.id === activeConversationId)?.title) || "New Chat"}
                </span>
                <button
                  onClick={() => setShareDialogOpen(true)}
                  className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs transition-colors hover:bg-white/10 shrink-0 mr-12"
                  style={{ color: "var(--text-secondary)" }}
                >
                  <Share2 size={13} />
                  Share
                </button>
              </div>
            )}

            {(() => {
              const conv = activeConversationId
                ? conversations.find((c) => c.id === activeConversationId)
                : null
              if (!conv?.workflow_run_id || !conv?.workflow_id) return null
              return (
                <div
                  className="mx-4 mb-2 flex items-center justify-between gap-3 px-3 py-2 rounded-lg text-[12px] shrink-0"
                  style={{
                    background: "rgba(80, 87, 234, 0.08)",
                    border: "1px solid rgba(80, 87, 234, 0.2)",
                    color: "var(--text-secondary)",
                  }}
                >
                  <span className="truncate">
                    Continuing from a workflow run — full context preserved.
                  </span>
                  <button
                    onClick={() => router.navigate(`/workflows/${conv.workflow_id}/runs/${conv.workflow_run_id}`)}
                    className="shrink-0 px-2 py-0.5 rounded-md transition-colors hover:bg-white/10"
                    style={{ color: "#a5a9f5" }}
                  >
                    Open run →
                  </button>
                </div>
              )
            })()}

            <ChatMessages
              rows={rows}
              isStreaming={isStreaming}
              userName={firstName}
              userAvatarUrl={gideonUser?.avatar_url || undefined}
              onSend={handleSendMessage}
              onEditMessage={handleEditMessage}
              onStop={handleStop}
              sendDisabled={isStreaming}
              connectedProviders={connectedProviders}
              bottomInputRef={bottomInputRef}
              autoMode={autoMode}
              onAutoModeChange={handleAutoModeChange}
            />

            <div
              ref={bottomInputRef}
              {...(!(rows.length > 0 || isStreaming) && { inert: true })}
              style={{
                opacity: rows.length > 0 || isStreaming ? 1 : 0,
                pointerEvents: rows.length > 0 || isStreaming ? "auto" : "none",
                transition: rows.length > 0 || isStreaming ? "opacity 0.1s ease-out 0.6s" : "none",
              }}
            >
              <ChatInput onSend={handleSendMessage} onStop={handleStop} disabled={isStreaming} connectedProviders={connectedProviders} conversationId={activeConversationId} autoMode={autoMode} onAutoModeChange={handleAutoModeChange} suggestion={replySuggestion} onSuggestionConsumed={clearReplySuggestion} />
            </div>
          </div>

          {!isMobile && (
            <>
              {/* Collapse/expand button at panel edge */}
              <div
                className="fixed top-5 z-30 group"
                style={{
                  right: rightPanelOpen ? rightPanelWidth - 5 : 28,
                  transition: rightPanelDragging ? "none" : "right 300ms cubic-bezier(0.4, 0, 0.2, 1)",
                }}
              >
                <button
                  onClick={() => setRightPanelOpen(prev => !prev)}
                  className="flex items-center justify-center w-10 h-10 rounded-2xl transition-all hover:scale-105 active:scale-95"
                  style={{
                    background: "rgba(0,0,0,0.85)",
                    backdropFilter: "blur(12px)",
                    border: "1px solid rgba(255,255,255,0.1)",
                    color: "rgba(255,255,255,0.9)",
                    boxShadow: "0 4px 20px rgba(0,0,0,0.4), 0 2px 8px rgba(0,0,0,0.3)",
                  }}
                >
                  {rightPanelOpen ? <ChevronRight size={18} /> : <CheckSquare size={18} />}
                </button>
                {/* Tooltip */}
                <div
                  className={`absolute top-full mt-2 flex items-center gap-2 px-3 py-1.5 rounded-full whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity ${rightPanelOpen ? "left-1/2 -translate-x-1/2" : "right-0"}`}
                  style={{
                    background: "rgba(0,0,0,0.85)",
                    border: "1px solid rgba(255,255,255,0.1)",
                    boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
                  }}
                >
                  <span className="text-[12px]" style={{ color: "rgba(255,255,255,0.8)" }}>
                    {rightPanelOpen ? "Collapse" : "Quick Access"}
                  </span>
                  <span className="flex items-center gap-1">
                    <kbd className="flex items-center justify-center w-5 h-5 rounded text-[10px] font-medium" style={{ background: "rgba(255,255,255,0.1)", color: "rgba(255,255,255,0.6)" }}>&#8984;</kbd>
                    <kbd className="flex items-center justify-center w-5 h-5 rounded text-[10px] font-medium" style={{ background: "rgba(255,255,255,0.1)", color: "rgba(255,255,255,0.6)" }}>/</kbd>
                  </span>
                </div>
              </div>
              <RightPanel
                open={rightPanelOpen}
                onClose={closeRightPanel}
                width={rightPanelWidth}
                onWidthChange={setRightPanelWidth}
                onDraggingChange={setRightPanelDragging}
                onOpenConversation={handleSelectConversation}
                onRefreshConversations={mutateConversations}
                onHighlightConversation={highlightAndClear}
                onResumeConversation={handleResumeConversation}
                onStartMeetingChat={handleStartMeetingChat}
              />
            </>
          )}

          {shareDialogOpen && activeConversationId && (
            <ShareDialog
              conversationId={activeConversationId}
              onClose={() => setShareDialogOpen(false)}
            />
          )}
        </div>
      </div>

      {/* Mobile bottom navigation */}
      {isMobile && (
        <MobileBottomNav
          activePage={mobileShowConversations ? "conversations" : activePage}
          onNavigate={handleNavigate}
          isAdmin={isAdminEmail(user?.email)}
          integrationErrorCount={integrationErrorCount}
        />
      )}

      <SearchModal
        open={searchOpen}
        onClose={() => setSearchOpen(false)}
        conversations={conversations}
        onNewChat={() => { handleNewChat(); router.navigateToPage("chat"); setMobileShowConversations(false) }}
        onSelectConversation={handleSelectConversation}
        onNavigate={handleNavigate}
      />

      {pendingSurvey && activePage !== "surveys" && (
        <SurveyModal
          survey={pendingSurvey}
          onSubmit={submitSurvey}
        />
      )}

      {newVersionAvailable && <UpdateAvailableBanner />}
    </div>
  )
}
