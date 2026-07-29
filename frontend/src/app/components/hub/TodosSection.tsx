"use client"

import { useState, useRef, useEffect, useMemo } from "react"
import { createPortal } from "react-dom"
import {
  CheckSquare,
  CheckCircle2,
  Plus,
  Sparkles,
  Trash2,
  X,
  Clock,
  ChevronRight,
  AlarmClockOff,
  Send,
  Inbox,
} from "lucide-react"
import { AnimatePresence, motion } from "framer-motion"
import toast from "react-hot-toast"
import type { Todo, TodoSuggestion, Delegatee } from "../../types"
import {
  sortTodos,
  formatSnoozeDate,
  requestNotificationPermission,
  fireCompletionNotification,
} from "./utils"
import { TodoItem, PriorityBarsIcon } from "./TodoItem"
import { DelegateModal, TodoTimeline } from "./TodoDelegation"
import { SourceIcon, SuggestionDetailCard } from "./SuggestionCard"

interface TodosSectionProps {
  todos: Todo[]
  delegatedTodos: Todo[]
  delegatedToMeTodos: Todo[]
  snoozedTodos: Todo[]
  suggestions: TodoSuggestion[]
  createTodo: (text: string) => Promise<any>
  createSubtask: (parentId: string, text: string) => Promise<any>
  updateTodo: (id: string, updates: any) => Promise<any>
  deleteTodo: (id: string) => Promise<any>
  snoozeTodo: (id: string, until: string) => Promise<any>
  unsnoozeTodo: (id: string) => Promise<any>
  assignToGideon: (id: string) => Promise<{ conversation_id: string }>
  delegateTodo: (id: string, delegatees: Delegatee[], message?: string, reminderCadence?: string, followUpAt?: string) => Promise<any>
  nudgeTodo: (id: string) => Promise<any>
  getTodoUpdates: (id: string) => Promise<any>
  addSuggestion: (id: string) => Promise<any>
  dismissSuggestion: (id: string) => Promise<any>
  onOpenConversation?: (conversationId: string) => void
  onRefreshConversations?: () => void
  onResumeConversation?: (conversationId: string, userMessage: string) => void
}

export default function TodosSection({
  todos,
  delegatedTodos,
  delegatedToMeTodos,
  snoozedTodos,
  suggestions,
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
  onOpenConversation,
  onRefreshConversations,
  onResumeConversation,
}: TodosSectionProps) {
  const [addingTodo, setAddingTodo] = useState(false)
  const [newTodoText, setNewTodoText] = useState("")
  const [expandedSuggestion, setExpandedSuggestion] = useState<string | null>(null)
  const [dismissingIds, setDismissingIds] = useState<Set<string>>(new Set())
  const [assigningIds, setAssigningIds] = useState<Set<string>>(new Set())
  const [editingTodoId, setEditingTodoId] = useState<string | null>(null)
  const [delegatingTodoId, setDelegatingTodoId] = useState<string | null>(null)
  const [delegateOriginRect, setDelegateOriginRect] = useState<DOMRect | null>(null)
  const [timelineTodo, setTimelineTodo] = useState<Todo | null>(null)
  const [snoozedExpanded, setSnoozedExpanded] = useState(false)
  const [todosExpanded, setTodosExpanded] = useState(true)
  const [delegatedExpanded, setDelegatedExpanded] = useState(true)
  const [delegatedToMeExpanded, setDelegatedToMeExpanded] = useState(true)
  const [suggestionsExpanded, setSuggestionsExpanded] = useState(true)
  const [completedExpanded, setCompletedExpanded] = useState(false)
  const [clearingCompleted, setClearingCompleted] = useState(false)
  const [flyingTodo, setFlyingTodo] = useState<{ text: string; rect: DOMRect; landed: boolean; targetTop: number; targetLeft: number } | null>(null)
  const newTodoInputRef = useRef<HTMLInputElement>(null)
  const prevTodosRef = useRef<Todo[]>([])

  const sortedTodos = useMemo(() => sortTodos(todos), [todos])
  const activeTodos = useMemo(() => sortedTodos.filter((t) => !t.completed), [sortedTodos])
  const completedTodos = useMemo(() => sortedTodos.filter((t) => t.completed), [sortedTodos])

  // Detect agent_status transitions from "working" to "completed" and fire notification
  useEffect(() => {
    const prevTodos = prevTodosRef.current
    if (prevTodos.length > 0) {
      for (const todo of todos) {
        const prev = prevTodos.find((t) => t.id === todo.id)
        if (prev && prev.agent_status === "working" && todo.agent_status === "completed") {
          fireCompletionNotification(todo.text)
        }
      }
    }
    prevTodosRef.current = todos
  }, [todos])

  // Focus input when adding todo
  useEffect(() => {
    if (addingTodo && newTodoInputRef.current) {
      newTodoInputRef.current.focus()
    }
  }, [addingTodo])

  // ---- Todo handlers ----
  const handleCreateTodo = async () => {
    const text = newTodoText.trim()
    if (!text) return
    setNewTodoText("")
    setAddingTodo(false)
    try {
      await createTodo(text)
    } catch {
      // Silently fail -- SWR will revalidate
    }
  }

  const handleToggleTodo = async (todo: Todo) => {
    try {
      await updateTodo(todo.id, { completed: !todo.completed })
    } catch {
      // noop
    }
  }

  const handleUpdateTodo = async (id: string, updates: { text?: string; priority?: Todo["priority"]; source_detail?: string | null }) => {
    try {
      await updateTodo(id, updates)
    } catch {
      // noop
    }
  }

  const handleDeleteTodo = async (id: string) => {
    try {
      await deleteTodo(id)
    } catch {
      // noop
    }
  }

  const handleClearCompleted = async () => {
    if (completedTodos.length === 0 || clearingCompleted) return
    setClearingCompleted(true)
    try {
      await Promise.all(completedTodos.map((t) => deleteTodo(t.id).catch(() => undefined)))
    } finally {
      setClearingCompleted(false)
    }
  }

  const assignTimersRef = useRef<ReturnType<typeof setTimeout>[]>([])
  useEffect(() => {
    return () => { assignTimersRef.current.forEach(clearTimeout) }
  }, [])

  const handleAssignToGideon = async (todoId: string) => {
    requestNotificationPermission()
    assignTimersRef.current.forEach(clearTimeout)
    assignTimersRef.current = []

    const el = document.querySelector(`[data-todo-id="${todoId}"]`)
    const rect = el?.getBoundingClientRect()

    const todo = [...todos, ...delegatedTodos, ...delegatedToMeTodos].find(t => t.id === todoId)

    setAssigningIds((prev) => new Set(prev).add(todoId))
    try {
      const { conversation_id } = await assignToGideon(todoId)
      onRefreshConversations?.()

      const userMessage = todo
        ? todo.source_detail ? `${todo.text}\n\n---\nContext:\n${todo.source_detail}` : todo.text
        : ""

      if (rect && todo) {
        setFlyingTodo({ text: todo.text, rect, landed: false, targetTop: 200, targetLeft: 80 })
        assignTimersRef.current.push(setTimeout(() => {
          const convEl = document.querySelector(`[data-conversation-id="${conversation_id}"]`)
          const targetRect = convEl?.getBoundingClientRect()
          setFlyingTodo(prev => prev ? {
            ...prev,
            landed: true,
            targetTop: targetRect ? targetRect.top : 200,
            targetLeft: targetRect ? targetRect.left : 80,
          } : null)
        }, 50))
        assignTimersRef.current.push(setTimeout(() => {
          setFlyingTodo(null)
          onResumeConversation?.(conversation_id, userMessage)
        }, 1100))
      } else {
        onResumeConversation?.(conversation_id, userMessage)
      }
    } catch {
      // noop
    } finally {
      setAssigningIds((prev) => {
        const next = new Set(prev)
        next.delete(todoId)
        return next
      })
    }
  }

  const handleOpenThread = (todo: Todo) => {
    if (todo.assigned_conversation_id && onOpenConversation) {
      onOpenConversation(todo.assigned_conversation_id)
    }
  }

  const handleSnoozeTodo = async (id: string, until: string) => {
    try {
      await snoozeTodo(id, until)
    } catch {
      // noop
    }
  }

  const handleUnsnoozeTodo = async (id: string) => {
    try {
      await unsnoozeTodo(id)
    } catch {
      // noop
    }
  }

  const handleCreateSubtask = async (parentId: string, text: string) => {
    try {
      await createSubtask(parentId, text)
    } catch {
      // noop
    }
  }

  const handleToggleSubtask = async (subtaskId: string, completed: boolean) => {
    try {
      await updateTodo(subtaskId, { completed })
    } catch {
      // noop
    }
  }

  const handleDeleteSubtask = async (subtaskId: string) => {
    try {
      await deleteTodo(subtaskId)
    } catch {
      // noop
    }
  }

  const handleEditSubtask = async (subtaskId: string, text: string) => {
    try {
      await updateTodo(subtaskId, { text })
    } catch {
      // noop
    }
  }

  const handleDelegate = async (todoId: string, delegatees: Delegatee[], message?: string, reminderCadence?: string, followUpAt?: string) => {
    try {
      const result = await delegateTodo(todoId, delegatees, message, reminderCadence, followUpAt)
      setDelegatingTodoId(null)
      // Warn about delegatees who got the Slack DM but have no Gideon account
      // yet — they won't see this in their Gideon hub.
      const missing = (result as { not_on_gideon?: Delegatee[] } | undefined)?.not_on_gideon ?? []
      if (missing.length > 0) {
        const names = missing.map((m) => m.name.split(" ")[0]).join(", ")
        toast(
          `${names} ${missing.length === 1 ? "isn't" : "aren't"} on Gideon yet — they got the Slack message but no Gideon todo.`,
          { icon: "⚠️", duration: 6000 },
        )
      }
    } catch (err) {
      // Keep the modal open so the user can fix input or retry. Surface the
      // server detail (e.g. "Slack not connected", "Invalid follow_up_at format")
      // so failure is visible rather than silently swallowed.
      const msg = err instanceof Error ? err.message : "Failed to delegate"
      toast.error(msg)
      throw err
    }
  }

  const handleNudge = async (todoId: string) => {
    try {
      await nudgeTodo(todoId)
    } catch {
      // noop
    }
  }

  // ---- Suggestion handlers ----
  const handleAddSuggestion = async (id: string) => {
    try {
      await addSuggestion(id)
    } catch {
      // noop
    }
  }

  const handleDismissSuggestion = async (id: string) => {
    setDismissingIds((prev) => new Set(prev).add(id))
    try {
      await dismissSuggestion(id)
    } catch {
      // noop
    } finally {
      setDismissingIds((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    }
  }

  const renderTodoList = (
    list: Todo[],
    { showDelegate = true, readOnly = false, allowToggle = false }: { showDelegate?: boolean; readOnly?: boolean; allowToggle?: boolean } = {},
  ) => (
    <AnimatePresence mode="popLayout">
      {sortTodos(list).map((todo, i) => (
        <TodoItem
          key={todo.id}
          todo={todo}
          index={i}
          editing={readOnly ? false : editingTodoId === todo.id}
          onStartEdit={readOnly ? undefined : () => setEditingTodoId(editingTodoId === todo.id ? null : todo.id)}
          onToggle={(!readOnly || allowToggle) ? () => handleToggleTodo(todo) : undefined}
          onUpdate={readOnly ? undefined : (u) => handleUpdateTodo(todo.id, u)}
          onDelete={readOnly ? undefined : () => handleDeleteTodo(todo.id)}
          onAssign={readOnly ? undefined : () => handleAssignToGideon(todo.id)}
          onDelegate={!readOnly && showDelegate ? (e?: React.MouseEvent) => {
            if (e) setDelegateOriginRect((e.currentTarget as HTMLElement).getBoundingClientRect())
            setDelegatingTodoId(todo.id)
          } : undefined}
          onSnooze={readOnly ? undefined : (until) => handleSnoozeTodo(todo.id, until)}
          onOpenThread={() => handleOpenThread(todo)}
          onOpenTimeline={() => setTimelineTodo(todo)}
          onCreateSubtask={readOnly ? undefined : (text) => handleCreateSubtask(todo.id, text)}
          onToggleSubtask={readOnly ? undefined : handleToggleSubtask}
          onDeleteSubtask={readOnly ? undefined : handleDeleteSubtask}
          onEditSubtask={readOnly ? undefined : handleEditSubtask}
          onDelegateSubtask={!readOnly && showDelegate ? (subtaskId, e) => {
            setDelegateOriginRect((e.currentTarget as HTMLElement).getBoundingClientRect())
            setDelegatingTodoId(subtaskId)
          } : undefined}
          onOpenSubtaskTimeline={(sub) => setTimelineTodo(sub)}
          isAssigning={assigningIds.has(todo.id)}
          readOnly={readOnly}
        />
      ))}
    </AnimatePresence>
  )

  return (
    <>
      <div className="px-5 py-2">
        {/* MY TODOS */}
        <button
          onClick={() => setTodosExpanded(!todosExpanded)}
          className="flex items-center gap-1.5 mb-2 pb-2 w-full"
          style={{ borderBottom: "1px solid rgba(255,255,255,0.10)" }}
        >
          <ChevronRight
            size={13}
            style={{
              color: "rgba(255,255,255,1)",
              transform: todosExpanded ? "rotate(90deg)" : undefined,
              transition: "transform 0.15s",
            }}
          />
          <CheckSquare size={13} style={{ color: "rgba(255,255,255,1)" }} />
          <span
            className="text-[10px] font-semibold tracking-widest uppercase"
            style={{ color: "rgba(255,255,255,1)" }}
          >
            My Todos
          </span>
          <span
            className="text-[10px] font-medium ml-auto"
            style={{ color: "rgba(255,255,255,1)" }}
          >
            {activeTodos.length}
          </span>
        </button>

        {todosExpanded && (
          <>
            {renderTodoList(activeTodos)}

            {/* Add new todo row */}
            {addingTodo ? (
              <div className="flex items-center gap-2.5 py-2">
                <div
                  className="w-[18px] h-[18px] rounded-md border shrink-0"
                  style={{ borderColor: "rgba(255,255,255,0.2)" }}
                />
                <input
                  ref={newTodoInputRef}
                  value={newTodoText}
                  onChange={(e) => setNewTodoText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleCreateTodo()
                    if (e.key === "Escape") {
                      setAddingTodo(false)
                      setNewTodoText("")
                    }
                  }}
                  onBlur={() => {
                    if (!newTodoText.trim()) {
                      setAddingTodo(false)
                      setNewTodoText("")
                    }
                  }}
                  placeholder="What needs to be done?"
                  className="bg-transparent text-[13px] outline-none placeholder:text-white/30 flex-1"
                  style={{ color: "rgba(255,255,255,1)" }}
                />
                <button
                  onClick={() => { setAddingTodo(false); setNewTodoText("") }}
                  className="w-6 h-6 flex items-center justify-center rounded hover:bg-white/10 transition-colors shrink-0"
                >
                  <X size={14} style={{ color: "rgba(255,255,255,1)" }} />
                </button>
              </div>
            ) : (
              <button
                onClick={() => setAddingTodo(true)}
                className="flex items-center gap-2.5 py-2 w-full text-left group/add transition-opacity"
                style={{ opacity: 0.35 }}
                onMouseEnter={(e) => { e.currentTarget.style.opacity = "0.7" }}
                onMouseLeave={(e) => { e.currentTarget.style.opacity = "0.35" }}
              >
                <div
                  className="w-[18px] h-[18px] rounded-md border shrink-0"
                  style={{ borderColor: "rgba(255,255,255,0.15)" }}
                />
                <span
                  className="text-[13px] leading-[18px]"
                  style={{ color: "rgba(255,255,255,0.75)" }}
                >
                  Add new to do
                </span>
              </button>
            )}
          </>
        )}

        {/* DELEGATED TO ME */}
        {delegatedToMeTodos.length > 0 && (
          <div className="mt-6">
            <button
              onClick={() => setDelegatedToMeExpanded(!delegatedToMeExpanded)}
              className="flex items-center gap-1.5 mb-2 pb-2 w-full"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.10)" }}
            >
              <ChevronRight
                size={13}
                style={{
                  color: "rgba(255,255,255,1)",
                  transform: delegatedToMeExpanded ? "rotate(90deg)" : undefined,
                  transition: "transform 0.15s",
                }}
              />
              <Inbox size={13} style={{ color: "rgba(255,255,255,1)" }} />
              <span className="text-[10px] font-semibold tracking-widest uppercase" style={{ color: "rgba(255,255,255,1)" }}>
                Delegated to me
              </span>
              <span className="text-[10px] font-medium ml-auto" style={{ color: "rgba(255,255,255,1)" }}>
                {delegatedToMeTodos.length}
              </span>
            </button>
            {delegatedToMeExpanded && renderTodoList(delegatedToMeTodos, { showDelegate: false, readOnly: true, allowToggle: true })}
          </div>
        )}

        {/* DELEGATED (I delegated to others) */}
        {delegatedTodos.length > 0 && (
          <div className="mt-6">
            <button
              onClick={() => setDelegatedExpanded(!delegatedExpanded)}
              className="flex items-center gap-1.5 mb-2 pb-2 w-full"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.10)" }}
            >
              <ChevronRight
                size={13}
                style={{
                  color: "rgba(255,255,255,1)",
                  transform: delegatedExpanded ? "rotate(90deg)" : undefined,
                  transition: "transform 0.15s",
                }}
              />
              <Send size={13} style={{ color: "rgba(255,255,255,1)" }} />
              <span className="text-[10px] font-semibold tracking-widest uppercase" style={{ color: "rgba(255,255,255,1)" }}>
                Delegated by me
              </span>
              <span className="text-[10px] font-medium ml-auto" style={{ color: "rgba(255,255,255,1)" }}>
                {delegatedTodos.length}
              </span>
            </button>
            {delegatedExpanded && renderTodoList(delegatedTodos)}
          </div>
        )}

        {/* SUGGESTIONS */}
        {suggestions.length > 0 && (
          <div className="mt-6">
            <button
              onClick={() => setSuggestionsExpanded(!suggestionsExpanded)}
              className="flex items-center gap-1.5 mb-3 pb-2 w-full"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.10)" }}
            >
              <ChevronRight
                size={13}
                style={{
                  color: "rgba(255,255,255,1)",
                  transform: suggestionsExpanded ? "rotate(90deg)" : undefined,
                  transition: "transform 0.15s",
                }}
              />
              <Sparkles size={13} style={{ color: "rgba(255,255,255,1)" }} />
              <span
                className="text-[10px] font-semibold tracking-widest uppercase"
                style={{ color: "rgba(255,255,255,1)" }}
              >
                Suggestions
              </span>
              <span
                className="text-[10px] font-medium ml-auto"
                style={{ color: "rgba(255,255,255,1)" }}
              >
                {suggestions.length}
              </span>
            </button>

            {suggestionsExpanded && <>
            {expandedSuggestion && (
              <div className="mb-3">
                <SuggestionDetailCard
                  suggestion={
                    suggestions.find((s) => s.id === expandedSuggestion) || {
                      id: "",
                      text: "",
                      detail: null,
                      source_integration: null,
                      source_url: null,
                      created_at: "",
                    }
                  }
                  onClose={() => setExpandedSuggestion(null)}
                />
              </div>
            )}

            {suggestions.map((s, i) => (
              <motion.div
                key={s.id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: dismissingIds.has(s.id) ? 0 : 1, x: 0 }}
                transition={{ duration: 0.2, delay: i * 0.04 }}
                className="group/sug relative flex items-start gap-2.5 w-full py-2.5 px-1.5 -mx-1.5 rounded-lg cursor-pointer transition-[background]"
                onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(255,255,255,0.03)" }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = "transparent" }}
              >
                <button
                  onClick={() => handleAddSuggestion(s.id)}
                  className="mt-0.5 shrink-0 w-5 h-5 flex items-center justify-center rounded hover:bg-white/10 transition-colors"
                >
                  <Plus size={14} style={{ color: "rgba(255,255,255,1)" }} />
                </button>

                <span
                  className="flex-1 min-w-0 leading-snug cursor-default"
                  style={{ fontSize: "var(--font-size-panel)", color: "rgba(255,255,255,1)" }}
                >
                  {s.text}
                </span>

                <button
                  onClick={() => handleDismissSuggestion(s.id)}
                  className="mt-0.5 shrink-0 w-5 h-5 flex items-center justify-center rounded hover:bg-white/10 transition-all opacity-0 group-hover/sug:opacity-100"
                >
                  <Trash2 size={13} style={{ color: "rgba(255,255,255,1)" }} />
                </button>

                {s.detail && (
                  <div
                    className="absolute left-0 right-0 bottom-full mb-1 rounded-xl p-3.5 shadow-2xl opacity-0 pointer-events-none group-hover/sug:opacity-100 group-hover/sug:pointer-events-auto transition-opacity duration-150 z-20"
                    style={{
                      transitionDelay: "300ms",
                      background: "rgba(25,25,30,0.97)",
                      backdropFilter: "blur(16px)",
                      border: "1px solid rgba(255,255,255,0.1)",
                    }}
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <SourceIcon source={s.source_integration} />
                      <span
                        className="text-[10px] font-semibold tracking-wider uppercase"
                        style={{ color: "rgba(255,255,255,1)" }}
                      >
                        Suggestion Details
                      </span>
                    </div>
                    <p className="text-[12px] leading-relaxed" style={{ color: "rgba(255,255,255,1)" }}>
                      {s.detail}
                    </p>
                    <div
                      className="absolute left-1/2 -translate-x-1/2 -bottom-1.5 w-3 h-3 rotate-45"
                      style={{ background: "rgba(25,25,30,0.97)", borderRight: "1px solid rgba(255,255,255,0.1)", borderBottom: "1px solid rgba(255,255,255,0.1)" }}
                    />
                  </div>
                )}
              </motion.div>
            ))}
            </>}
          </div>
        )}

        {/* SNOOZED */}
        {snoozedTodos.length > 0 && (
          <div className="mt-6">
            <button
              onClick={() => setSnoozedExpanded(!snoozedExpanded)}
              className="flex items-center gap-1.5 mb-2 pb-2 w-full"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.10)" }}
            >
              <ChevronRight
                size={13}
                style={{
                  color: "rgba(255,255,255,1)",
                  transform: snoozedExpanded ? "rotate(90deg)" : undefined,
                  transition: "transform 0.15s",
                }}
              />
              <Clock size={13} style={{ color: "rgba(255,255,255,1)" }} />
              <span
                className="text-[10px] font-semibold tracking-widest uppercase"
                style={{ color: "rgba(255,255,255,1)" }}
              >
                Snoozed
              </span>
              <span
                className="text-[10px] font-medium ml-auto"
                style={{ color: "rgba(255,255,255,1)" }}
              >
                {snoozedTodos.length}
              </span>
            </button>

            <AnimatePresence>
              {snoozedExpanded && snoozedTodos.map((todo, i) => (
                <motion.div
                  key={todo.id}
                  initial={{ opacity: 0, x: -8 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: -8, transition: { duration: 0.15 } }}
                  transition={{ duration: 0.2, delay: i * 0.04 }}
                  className="group/snz flex items-center gap-2.5 py-2 px-1.5 -mx-1.5 rounded-lg hover:bg-white/[0.03] transition-colors"
                >
                  {todo.priority !== "none" && (
                    <span className="shrink-0"><PriorityBarsIcon priority={todo.priority} size={12} /></span>
                  )}
                  <div className="flex-1 min-w-0">
                    <span
                      className="text-[13px] leading-snug"
                      style={{ color: "rgba(255,255,255,1)" }}
                    >
                      {todo.text}
                    </span>
                    {todo.snoozed_until && (
                      <p className="text-[10px] mt-0.5 flex items-center gap-1" style={{ color: "rgba(129,140,248,0.7)" }}>
                        <Clock size={9} />
                        {formatSnoozeDate(todo.snoozed_until)}
                      </p>
                    )}
                  </div>
                  <button
                    onClick={() => handleUnsnoozeTodo(todo.id)}
                    className="shrink-0 flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium opacity-0 group-hover/snz:opacity-100 transition-all hover:bg-white/10"
                    style={{ color: "rgba(255,255,255,1)", border: "1px solid rgba(255,255,255,0.1)" }}
                  >
                    <AlarmClockOff size={11} />
                    Unsnooze
                  </button>
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
        )}

        {/* COMPLETED */}
        {completedTodos.length > 0 && (
          <div className="mt-6">
            <div
              className="flex items-center gap-1.5 mb-2 pb-2"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.10)" }}
            >
              <button
                onClick={() => setCompletedExpanded(!completedExpanded)}
                className="flex items-center gap-1.5 flex-1"
              >
                <ChevronRight
                  size={13}
                  style={{
                    color: "rgba(255,255,255,1)",
                    transform: completedExpanded ? "rotate(90deg)" : undefined,
                    transition: "transform 0.15s",
                  }}
                />
                <CheckCircle2 size={13} style={{ color: "rgba(255,255,255,1)" }} />
                <span
                  className="text-[10px] font-semibold tracking-widest uppercase"
                  style={{ color: "rgba(255,255,255,1)" }}
                >
                  Completed
                </span>
                <span
                  className="text-[10px] font-medium ml-auto"
                  style={{ color: "rgba(255,255,255,1)" }}
                >
                  {completedTodos.length}
                </span>
              </button>
              <button
                onClick={handleClearCompleted}
                disabled={clearingCompleted}
                className="ml-2 flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium tracking-wider uppercase transition-all hover:bg-white/10 disabled:opacity-40"
                style={{ color: "rgba(255,255,255,0.7)", border: "1px solid rgba(255,255,255,0.1)" }}
                title="Delete all completed todos"
              >
                <Trash2 size={10} />
                {clearingCompleted ? "Clearing" : "Clear"}
              </button>
            </div>
            {completedExpanded && renderTodoList(completedTodos)}
          </div>
        )}
      </div>

      {/* Delegate Modal */}
      {delegatingTodoId && (() => {
        // Locate the target todo across every section + their subtasks so we
        // can pre-fill the modal for reassignment.
        const allTodos = [...todos, ...delegatedTodos, ...delegatedToMeTodos, ...snoozedTodos]
        let target: Todo | undefined = allTodos.find((t) => t.id === delegatingTodoId)
        if (!target) {
          for (const t of allTodos) {
            const sub = t.subtasks?.find((s) => s.id === delegatingTodoId)
            if (sub) { target = sub; break }
          }
        }
        return (
          <DelegateModal
            // Force a fresh mount when the target todo changes. The modal
            // snapshots `existingDelegatees`/`existingCadence` once at mount,
            // so without this key React would reuse Todo A's local state when
            // the same modal slot re-renders with Todo B's props.
            key={delegatingTodoId}
            onDelegate={(delegatees, message, reminderCadence, followUpAt) => handleDelegate(delegatingTodoId, delegatees, message, reminderCadence, followUpAt)}
            onClose={() => { setDelegatingTodoId(null); setDelegateOriginRect(null) }}
            originRect={delegateOriginRect}
            existingDelegatees={target?.delegated_to ?? undefined}
            existingCadence={target?.reminder_cadence ?? undefined}
          />
        )
      })()}

      {/* Todo Timeline Modal */}
      {timelineTodo && timelineTodo.delegated_to && (
        <TodoTimeline
          todoId={timelineTodo.id}
          todoText={timelineTodo.text}
          delegatedTo={timelineTodo.delegated_to}
          getTodoUpdates={getTodoUpdates}
          onNudge={() => handleNudge(timelineTodo.id)}
          onClose={() => setTimelineTodo(null)}
        />
      )}

      {/* Flying todo animation (portal over everything) */}
      {flyingTodo && createPortal(
        <div
          className="fixed z-[9999] pointer-events-none rounded-lg px-3 py-2"
          style={{
            top: flyingTodo.landed ? flyingTodo.targetTop : flyingTodo.rect.top,
            left: flyingTodo.landed ? flyingTodo.targetLeft : flyingTodo.rect.left,
            width: flyingTodo.landed ? 180 : flyingTodo.rect.width,
            opacity: flyingTodo.landed ? 0.15 : 1,
            transform: flyingTodo.landed ? "scale(0.5)" : "scale(1)",
            transition: "top 1000ms cubic-bezier(0.4, 0, 0.2, 1), left 1000ms cubic-bezier(0.4, 0, 0.2, 1), width 1000ms ease, transform 1000ms ease, opacity 300ms ease 700ms",
            background: "rgba(255,255,255,0.1)",
            border: "1px solid rgba(255,255,255,0.2)",
            backdropFilter: "blur(12px)",
          }}
        >
          <p className="text-[13px] font-medium truncate" style={{ color: "#fff" }}>
            {flyingTodo.text}
          </p>
        </div>,
        document.body
      )}
    </>
  )
}
