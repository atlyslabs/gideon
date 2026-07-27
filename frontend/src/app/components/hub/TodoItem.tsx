"use client"

import { useState, useRef, useEffect } from "react"
import {
  CheckSquare,
  Plus,
  ArrowRight,
  Sparkles,
  Trash2,
  MoreHorizontal,
  Check,
  X,
  Loader2,
  Inbox,
  MessageCircle,
  ExternalLink,
  UserPlus,
} from "lucide-react"
import { AnimatePresence, motion } from "framer-motion"
import type { Todo } from "../../types"
import {
  PRIORITY_COLORS,
  PRIORITY_LABELS,
  PRIORITY_CYCLE,
} from "./utils"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import TodoDropdown from "./TodoDropdown"

// ---------------------------------------------------------------------------
// PriorityBarsIcon
// ---------------------------------------------------------------------------

export function PriorityBarsIcon({ priority, size = 14 }: { priority: Todo["priority"]; size?: number }) {
  if (priority === "none") return null
  const color = PRIORITY_COLORS[priority] || "rgba(255,255,255,0.4)"
  const levels: Record<string, number> = { low: 1, medium: 2, high: 3, urgent: 4 }
  const level = levels[priority] || 0
  const bars = [
    { x: 1, h: 4 },
    { x: 5, h: 7 },
    { x: 9, h: 10 },
    { x: 13, h: 14 },
  ]
  return (
    <svg width={size} height={size} viewBox="0 0 18 16" className="shrink-0">
      {bars.map((b, i) => (
        <rect key={i} x={b.x} y={16 - b.h} width={3} height={b.h} rx={0.8}
          fill={i < level ? color : "rgba(255,255,255,0.12)"} />
      ))}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// PriorityBadge
// ---------------------------------------------------------------------------

function PriorityBadge({ priority, onClick }: { priority: Todo["priority"]; onClick: () => void }) {
  if (priority === "none") return null
  const color = PRIORITY_COLORS[priority] || "rgba(255,255,255,0.4)"
  return (
    <button
      onClick={(e) => { e.stopPropagation(); onClick() }}
      className="inline-flex items-center gap-1.5 px-2 py-[3px] rounded-md mt-2 transition-colors hover:brightness-125"
      style={{ background: `${color}18`, border: `1px solid ${color}30` }}
      title="Click to change priority"
    >
      <PriorityBarsIcon priority={priority} size={12} />
      <span className="text-[11px] font-medium" style={{ color }}>{PRIORITY_LABELS[priority]}</span>
    </button>
  )
}

// ---------------------------------------------------------------------------
// SubtaskItem
// ---------------------------------------------------------------------------

function SubtaskItem({
  subtask,
  onToggle,
  onDelete,
  onEdit,
  onDelegate,
  onOpenTimeline,
}: {
  subtask: Todo
  onToggle: () => void
  onDelete: () => void
  onEdit: (text: string) => void
  onDelegate?: (e: React.MouseEvent) => void
  onOpenTimeline?: () => void
}) {
  const isDelegated = (subtask.delegated_to?.length ?? 0) > 0
  const [isEditing, setIsEditing] = useState(false)
  const [editText, setEditText] = useState(subtask.text)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!isEditing) setEditText(subtask.text)
  }, [subtask.text, isEditing])

  useEffect(() => {
    if (isEditing) {
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }, [isEditing])

  const commit = () => {
    const trimmed = editText.trim()
    if (trimmed && trimmed !== subtask.text) onEdit(trimmed)
    setIsEditing(false)
  }

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 4, transition: { duration: 0.15 } }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="group/sub flex items-center gap-2 py-1 pl-7"
    >
      <button
        onClick={(e) => { e.stopPropagation(); onToggle() }}
        className="w-[15px] h-[15px] rounded border flex items-center justify-center shrink-0 transition-all"
        style={{
          borderColor: subtask.completed ? "#3b82f6" : "rgba(255,255,255,0.25)",
          background: subtask.completed ? "#3b82f6" : "transparent",
        }}
      >
        {subtask.completed && <Check size={9} color="#fff" strokeWidth={3} />}
      </button>
      {isEditing ? (
        <input
          ref={inputRef}
          value={editText}
          onChange={(e) => setEditText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); commit() }
            if (e.key === "Escape") { setEditText(subtask.text); setIsEditing(false) }
          }}
          onBlur={commit}
          onClick={(e) => e.stopPropagation()}
          className="bg-transparent text-[12px] outline-none flex-1 min-w-0"
          style={{ color: "rgba(255,255,255,1)" }}
        />
      ) : (
        <span
          onClick={(e) => { e.stopPropagation(); if (!subtask.completed) setIsEditing(true) }}
          className={`text-[12px] leading-snug flex-1 min-w-0 cursor-text ${subtask.completed ? "line-through opacity-40" : ""}`}
          style={{ color: "rgba(255,255,255,1)" }}
        >
          {subtask.priority !== "none" && (
            <span className="inline-flex align-middle mr-1"><PriorityBarsIcon priority={subtask.priority} size={10} /></span>
          )}
          {subtask.text}
        </span>
      )}
      {isDelegated && (
        <div className="group/avatars flex items-center gap-1 shrink-0">
          <div className="relative flex items-center">
            {subtask.delegated_to!.map((d, i) => (
              <span
                key={d.email}
                className={`inline-flex items-center rounded-full overflow-hidden ${i > 0 ? "-ml-1" : ""}`}
                style={{ zIndex: subtask.delegated_to!.length - i }}
                title={d.name}
              >
                {d.avatar_url ? (
                  <img src={d.avatar_url} alt={d.name} className="w-4 h-4 rounded-full object-cover border border-[rgba(20,20,25,0.8)] shrink-0" />
                ) : (
                  <div className="w-4 h-4 rounded-full flex items-center justify-center border border-[rgba(20,20,25,0.8)] shrink-0" style={{ background: "rgba(255,255,255,0.2)" }}>
                    <span className="text-[7px] font-bold" style={{ color: "#fff" }}>{d.name?.[0]?.toUpperCase() || "?"}</span>
                  </div>
                )}
              </span>
            ))}
          </div>
          {onOpenTimeline && (
            <button
              onClick={(e) => { e.stopPropagation(); onOpenTimeline() }}
              title="Updates"
              className="w-6 h-6 flex items-center justify-center rounded hover:bg-white/10 transition-colors"
              style={{ color: "rgba(255,255,255,0.7)" }}
            >
              <MessageCircle size={11} />
            </button>
          )}
          {subtask.slack_channel_id && subtask.slack_thread_ts && (
            <a
              href={`slack://channel?team=T01S84FFGAW&id=${subtask.slack_channel_id}&message=${subtask.slack_thread_ts}`}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              title="Open Slack thread"
              className="w-6 h-6 flex items-center justify-center rounded hover:bg-white/10 transition-colors"
              style={{ color: "rgba(255,255,255,0.7)" }}
            >
              {getIntegrationIcon("slack", 11)}
            </a>
          )}
        </div>
      )}
      {onDelegate && !subtask.completed && (
        <button
          onClick={(e) => { e.stopPropagation(); onDelegate(e) }}
          title={isDelegated ? "Reassign this subtask" : "Delegate this subtask"}
          className="w-7 h-7 flex items-center justify-center rounded hover:bg-white/10 transition-all shrink-0 opacity-100 md:opacity-0 md:group-hover/sub:opacity-100"
        >
          <UserPlus size={12} style={{ color: isDelegated ? "rgba(59,130,246,0.9)" : "rgba(255,255,255,0.6)" }} />
        </button>
      )}
      <button
        onClick={(e) => { e.stopPropagation(); onDelete() }}
        className="w-7 h-7 flex items-center justify-center rounded hover:bg-white/10 transition-all opacity-100 md:opacity-0 md:group-hover/sub:opacity-100 shrink-0"
      >
        <Trash2 size={12} style={{ color: "rgba(255,255,255,0.6)" }} />
      </button>
    </motion.div>
  )
}

// ---------------------------------------------------------------------------
// TodoItem
// ---------------------------------------------------------------------------

export function TodoItem({
  todo,
  editing,
  onStartEdit,
  onToggle,
  onUpdate,
  onDelete,
  onAssign,
  onDelegate,
  onSnooze,
  onOpenThread,
  onOpenTimeline,
  onCreateSubtask,
  onToggleSubtask,
  onDeleteSubtask,
  onEditSubtask,
  onDelegateSubtask,
  onOpenSubtaskTimeline,
  isAssigning,
  isFlying,
  index = 0,
  readOnly = false,
}: {
  todo: Todo
  editing: boolean
  onStartEdit?: () => void
  onToggle?: () => void
  onUpdate?: (updates: { text?: string; priority?: Todo["priority"]; source_detail?: string | null }) => void
  onDelete?: () => void
  onAssign?: () => void
  onDelegate?: (e?: React.MouseEvent) => void
  onSnooze?: (until: string) => void
  onOpenThread: () => void
  onOpenTimeline: () => void
  onCreateSubtask?: (text: string) => void
  onToggleSubtask?: (subtaskId: string, completed: boolean) => void
  onDeleteSubtask?: (subtaskId: string) => void
  onEditSubtask?: (subtaskId: string, text: string) => void
  onDelegateSubtask?: (subtaskId: string, e: React.MouseEvent) => void
  onOpenSubtaskTimeline?: (subtask: Todo) => void
  isAssigning: boolean
  isFlying?: boolean
  index?: number
  readOnly?: boolean
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [addingSubtask, setAddingSubtask] = useState(false)
  const [newSubtaskText, setNewSubtaskText] = useState("")
  const subtaskInputRef = useRef<HTMLInputElement>(null)
  const [editText, setEditText] = useState(todo.text)
  const [editDetail, setEditDetail] = useState(todo.source_detail || "")
  const titleRef = useRef<HTMLInputElement>(null)
  const detailRef = useRef<HTMLTextAreaElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [showCompletedSubtasks, setShowCompletedSubtasks] = useState(false)

  const subtasks = todo.subtasks || []
  const activeSubtasks = subtasks.filter((s) => !s.completed)
  const completedSubtasks = subtasks.filter((s) => s.completed)

  const showAgentStatus = todo.assigned_to_gideon
  const isCompleted = showAgentStatus && todo.agent_status === "completed"
  const isFailed = showAgentStatus && todo.agent_status === "failed"

  const cyclePriority = () => {
    if (!onUpdate) return
    const idx = PRIORITY_CYCLE.indexOf(todo.priority)
    const next = PRIORITY_CYCLE[(idx + 1) % PRIORITY_CYCLE.length]
    onUpdate({ priority: next })
  }

  // Sync local state when todo prop changes
  useEffect(() => {
    if (!editing) {
      setEditText(todo.text)
      setEditDetail(todo.source_detail || "")
    }
  }, [todo.text, todo.source_detail, editing])

  // Focus title input and auto-size detail when entering edit mode
  useEffect(() => {
    if (editing) {
      titleRef.current?.focus()
      if (detailRef.current) {
        detailRef.current.style.height = "auto"
        detailRef.current.style.height = detailRef.current.scrollHeight + "px"
      }
    }
  }, [editing])

  // Focus subtask input when adding
  useEffect(() => {
    if (addingSubtask) subtaskInputRef.current?.focus()
  }, [addingSubtask])

  const handleCreateSubtask = () => {
    if (!onCreateSubtask) return
    const text = newSubtaskText.trim()
    if (!text) return
    onCreateSubtask(text)
    setNewSubtaskText("")
    setAddingSubtask(false)
  }

  const commitEdit = () => {
    if (!onUpdate) return
    const trimmedText = editText.trim()
    if (!trimmedText) return
    const updates: { text?: string; source_detail?: string | null } = {}
    if (trimmedText !== todo.text) updates.text = trimmedText
    const trimmedDetail = editDetail.trim()
    if (trimmedDetail !== (todo.source_detail || "")) updates.source_detail = trimmedDetail || null
    if (Object.keys(updates).length > 0) onUpdate(updates)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { setEditText(todo.text); setEditDetail(todo.source_detail || ""); onStartEdit?.() }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); commitEdit(); onStartEdit?.() }
  }

  return (
    <div>
    <motion.div
      data-todo-id={todo.id}
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -8, transition: { duration: 0.15 } }}
      transition={{ duration: 0.2, delay: index * 0.04 }}
      className={`group flex items-start gap-2.5 py-1 px-1.5 -mx-1.5 rounded-lg relative transition-colors ${readOnly ? "cursor-default" : "cursor-pointer"}`}
      style={{ background: editing ? "rgba(255,255,255,0.04)" : "transparent" }}
      onClick={() => { if (!editing && !readOnly) onStartEdit?.() }}
      onMouseEnter={(e) => { if (!editing && !readOnly) (e.currentTarget as HTMLDivElement).style.background = "rgba(255,255,255,0.03)" }}
      onMouseLeave={(e) => { if (!editing && !readOnly) (e.currentTarget as HTMLDivElement).style.background = "transparent" }}
    >
      {/* Content */}
      <div className="flex-1 min-w-0">
        {editing ? (
          <div
            ref={containerRef}
            onClick={(e) => e.stopPropagation()}
            onBlur={(e) => {
              if (!containerRef.current?.contains(e.relatedTarget as Node)) {
                commitEdit()
                onStartEdit?.()
              }
            }}
          >
            <input
              ref={titleRef}
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              onKeyDown={handleKeyDown}
              className="w-full bg-transparent text-[13px] leading-none outline-none"
              style={{ color: "rgba(255,255,255,1)" }}
              placeholder="Todo title..."
            />
            <textarea
              ref={detailRef}
              value={editDetail}
              onChange={(e) => {
                setEditDetail(e.target.value)
                e.target.style.height = "auto"
                e.target.style.height = e.target.scrollHeight + "px"
              }}
              onKeyDown={handleKeyDown}
              rows={1}
              className="w-full bg-transparent text-[11px] mt-1 leading-snug outline-none resize-none placeholder:text-white/25"
              style={{ color: "rgba(255,255,255,1)" }}
              placeholder="Add a description..."
            />
            <PriorityBadge priority={todo.priority} onClick={cyclePriority} />
          </div>
        ) : (
          <>
            <div className="flex gap-2" style={{ alignItems: "first baseline" }}>
              <button
                onClick={(e) => { e.stopPropagation(); onToggle?.() }}
                disabled={!onToggle}
                className={`w-[15px] h-[15px] rounded-[4px] border inline-flex items-center justify-center shrink-0 transition-all ${!onToggle ? "cursor-default opacity-50" : "group-hover:border-white/50"}`}
                style={{
                  borderColor: todo.completed ? "#3b82f6" : "rgba(255,255,255,0.3)",
                  background: todo.completed ? "#3b82f6" : "transparent",
                  transform: "translateY(2px)",
                }}
              >
                {todo.completed && <Check size={9} color="#fff" strokeWidth={3} />}
              </button>
              <div className="flex-1 min-w-0" style={{ lineHeight: 1.2 }}>
                <span
                  className={todo.completed ? "line-through opacity-40" : ""}
                  style={{ fontSize: "var(--font-size-panel)", color: "rgba(255,255,255,1)" }}
                >
                  {todo.priority !== "none" && (
                    <span className="inline-flex align-middle mr-1"><PriorityBarsIcon priority={todo.priority} size={13} /></span>
                  )}
                  {!readOnly && (
                    <span className="float-right flex items-center gap-0.5 ml-1" onClick={(e) => e.stopPropagation()}>
                      {isAssigning && (
                        <span className="w-6 h-6 inline-flex items-center justify-center" title="Assigning to Gideon...">
                          <Loader2 size={14} className="animate-spin" style={{ color: "rgba(255,255,255,0.6)" }} />
                        </span>
                      )}
                      <span className="flex items-center gap-0.5 opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity">
                        {!todo.assigned_to_gideon && !isAssigning && (
                          <button
                            onClick={(e) => { e.stopPropagation(); onAssign?.() }}
                            className="w-6 h-6 flex items-center justify-center rounded hover:bg-white/10 transition-colors"
                            title="Assign to Gideon"
                          >
                            <Sparkles size={13} style={{ color: "rgba(255,255,255,0.6)" }} />
                          </button>
                        )}
                        {showAgentStatus && (
                          <button
                            onClick={(e) => { e.stopPropagation(); onOpenThread() }}
                            className="w-6 h-6 flex items-center justify-center rounded hover:bg-white/10 transition-colors"
                            title="Open thread"
                          >
                            <ArrowRight size={14} style={{ color: "rgba(255,255,255,0.6)" }} />
                          </button>
                        )}
                      </span>
                      <button
                        onClick={(e) => { e.stopPropagation(); setMenuOpen(!menuOpen) }}
                        className="w-6 h-6 flex items-center justify-center rounded hover:bg-white/10 transition-colors"
                      >
                        <MoreHorizontal size={14} style={{ color: "rgba(255,255,255,0.4)" }} />
                      </button>
                    </span>
                  )}
                  {todo.text}
                </span>
                {todo.source_detail && (
                  <p
                    className="text-[11px] mt-0.5 leading-snug"
                    style={{
                      color: "rgba(255,255,255,1)",
                      display: "-webkit-box",
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: "vertical" as const,
                      overflow: "hidden",
                    }}
                  >
                    {todo.source_detail}
                  </p>
                )}
                {todo.delegated_to && todo.delegated_to.length > 0 && (
                  <div className="group/avatars flex items-center gap-1.5 mt-1.5 flex-wrap">
                    <div className="relative flex items-center">
                      {todo.delegated_to.map((d, i) => (
                        <span
                          key={d.email}
                          className={`inline-flex items-center rounded-full overflow-hidden transition-all duration-200 ease-out group-hover/avatars:pl-1 group-hover/avatars:pr-2 group-hover/avatars:gap-1 group-hover/avatars:!ml-1 group-hover/avatars:border group-hover/avatars:border-[rgba(255,255,255,0.15)] group-hover/avatars:bg-[rgba(255,255,255,0.1)] ${i > 0 ? "-ml-1.5" : ""}`}
                          style={{ zIndex: todo.delegated_to!.length - i }}
                        >
                          {d.avatar_url ? (
                            <img src={d.avatar_url} alt={d.name} className="w-5 h-5 rounded-full object-cover border border-[rgba(20,20,25,0.8)] group-hover/avatars:w-4 group-hover/avatars:h-4 group-hover/avatars:border-0 transition-all duration-200 shrink-0" />
                          ) : (
                            <div className="w-5 h-5 rounded-full flex items-center justify-center border border-[rgba(20,20,25,0.8)] group-hover/avatars:w-4 group-hover/avatars:h-4 group-hover/avatars:border-0 transition-all duration-200 shrink-0" style={{ background: "rgba(255,255,255,0.2)" }}>
                              <span className="text-[8px] group-hover/avatars:text-[7px] font-bold transition-all duration-200" style={{ color: "#fff" }}>{d.name?.[0]?.toUpperCase() || "?"}</span>
                            </div>
                          )}
                          <span className="text-[10px] font-medium max-w-0 opacity-0 group-hover/avatars:max-w-[80px] group-hover/avatars:opacity-100 transition-all duration-200 overflow-hidden whitespace-nowrap" style={{ color: "#fff" }}>
                            {d.name.split(" ")[0]}
                          </span>
                        </span>
                      ))}
                    </div>
                    <button
                      onClick={(e) => { e.stopPropagation(); onOpenTimeline() }}
                      className="inline-flex items-center gap-1 px-2 py-[3px] rounded-md transition-colors hover:bg-white/10"
                      style={{ color: "rgba(255,255,255,0.7)" }}
                    >
                      <MessageCircle size={10} />
                      <span className="text-[10px] font-medium">Updates</span>
                    </button>
                    {onDelegate && !todo.completed && (
                      <button
                        onClick={(e) => { e.stopPropagation(); onDelegate(e) }}
                        title="Reassign this todo"
                        className="inline-flex items-center gap-1 px-2 py-[3px] rounded-md transition-colors hover:bg-white/10"
                        style={{ color: "rgba(59,130,246,0.9)" }}
                      >
                        <UserPlus size={10} />
                        <span className="text-[10px] font-medium">Reassign</span>
                      </button>
                    )}
                    {todo.slack_channel_id && todo.slack_thread_ts && (
                      <a
                        href={`slack://channel?team=T01S84FFGAW&id=${todo.slack_channel_id}&message=${todo.slack_thread_ts}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()}
                        className="inline-flex items-center gap-1 px-2 py-[3px] rounded-md transition-colors hover:bg-white/10"
                        style={{ color: "rgba(255,255,255,0.7)" }}
                      >
                        {getIntegrationIcon("slack", 10)}
                        <ExternalLink size={8} />
                      </a>
                    )}
                  </div>
                )}
                {todo.delegated_from_user_name && (
                  <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                    <span
                      className="inline-flex items-center gap-1.5 px-2 py-[3px] rounded-md"
                      style={{ background: "rgba(255,255,255,0.1)", border: "1px solid rgba(255,255,255,0.15)" }}
                    >
                      <Inbox size={10} style={{ color: "#fff" }} />
                      <span className="text-[10px] font-medium" style={{ color: "#fff" }}>
                        From {todo.delegated_from_user_name.split(" ")[0]}
                      </span>
                    </span>
                    <button
                      onClick={(e) => { e.stopPropagation(); onOpenTimeline() }}
                      className="inline-flex items-center gap-1 px-2 py-[3px] rounded-md transition-colors hover:bg-white/10"
                      style={{ color: "rgba(255,255,255,0.7)" }}
                    >
                      <MessageCircle size={10} />
                      <span className="text-[10px] font-medium">Updates</span>
                    </button>
                    {todo.slack_channel_id && todo.slack_thread_ts && (
                      <a
                        href={`slack://channel?team=T01S84FFGAW&id=${todo.slack_channel_id}&message=${todo.slack_thread_ts}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()}
                        className="inline-flex items-center gap-1 px-2 py-[3px] rounded-md transition-colors hover:bg-white/10"
                        style={{ color: "rgba(255,255,255,0.7)" }}
                      >
                        {getIntegrationIcon("slack", 10)}
                        <ExternalLink size={8} />
                      </a>
                    )}
                  </div>
                )}
              </div>
            </div>
          </>
        )}
      </div>

      {menuOpen && !readOnly && onAssign && onDelete && onSnooze && onUpdate && (
        <TodoDropdown
          todo={todo}
          onAssign={onAssign}
          onDelegate={onDelegate}
          onPriority={(p) => onUpdate({ priority: p })}
          onDelete={onDelete}
          onSnooze={onSnooze}
          onAddSubtask={() => setAddingSubtask(true)}
          onClose={() => setMenuOpen(false)}
          isAssigning={isAssigning}
        />
      )}

    </motion.div>

    {/* Subtasks — always show the "Add subtask" row for discoverability (hidden when readOnly) */}
    {!readOnly && (
    <div className="ml-1">
      <AnimatePresence mode="popLayout">
        {activeSubtasks.map((sub) => (
          <SubtaskItem
            key={sub.id}
            subtask={sub}
            onToggle={() => onToggleSubtask?.(sub.id, !sub.completed)}
            onDelete={() => onDeleteSubtask?.(sub.id)}
            onEdit={(text) => onEditSubtask?.(sub.id, text)}
            onDelegate={onDelegateSubtask ? (e) => onDelegateSubtask(sub.id, e) : undefined}
            onOpenTimeline={onOpenSubtaskTimeline ? () => onOpenSubtaskTimeline(sub) : undefined}
          />
        ))}
        {showCompletedSubtasks && completedSubtasks.map((sub) => (
          <SubtaskItem
            key={sub.id}
            subtask={sub}
            onToggle={() => onToggleSubtask?.(sub.id, !sub.completed)}
            onDelete={() => onDeleteSubtask?.(sub.id)}
            onEdit={(text) => onEditSubtask?.(sub.id, text)}
            onDelegate={onDelegateSubtask ? (e) => onDelegateSubtask(sub.id, e) : undefined}
            onOpenTimeline={onOpenSubtaskTimeline ? () => onOpenSubtaskTimeline(sub) : undefined}
          />
        ))}
      </AnimatePresence>

      {completedSubtasks.length > 0 && (
        <button
          onMouseDown={(e) => e.preventDefault()}
          onClick={(e) => { e.stopPropagation(); setShowCompletedSubtasks((v) => !v) }}
          className="flex items-center gap-1.5 py-1 pl-7 text-[11px] transition-colors hover:text-white/60"
          style={{ color: "rgba(255,255,255,0.35)" }}
        >
          {showCompletedSubtasks
            ? `Hide ${completedSubtasks.length} completed`
            : `Show ${completedSubtasks.length} completed`}
        </button>
      )}

        {addingSubtask ? (
          <div className="flex items-center gap-2 py-1 pl-7">
            <div
              className="w-[15px] h-[15px] rounded border shrink-0"
              style={{ borderColor: "rgba(255,255,255,0.15)" }}
            />
            <input
              ref={subtaskInputRef}
              value={newSubtaskText}
              onChange={(e) => setNewSubtaskText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleCreateSubtask()
                if (e.key === "Escape") { setAddingSubtask(false); setNewSubtaskText("") }
              }}
              onBlur={() => {
                if (!newSubtaskText.trim()) { setAddingSubtask(false); setNewSubtaskText("") }
              }}
              placeholder="Add subtask..."
              className="bg-transparent text-[12px] outline-none placeholder:text-white/25 flex-1"
              style={{ color: "rgba(255,255,255,1)" }}
            />
            <button
              onClick={() => { setAddingSubtask(false); setNewSubtaskText("") }}
              className="w-5 h-5 flex items-center justify-center rounded hover:bg-white/10 transition-colors shrink-0"
            >
              <X size={11} style={{ color: "rgba(255,255,255,1)" }} />
            </button>
          </div>
        ) : (
          <button
            onMouseDown={(e) => e.preventDefault()}
            onClick={(e) => { e.stopPropagation(); setAddingSubtask(true) }}
            className="flex items-center gap-1.5 py-1 pl-7 text-[11px] transition-colors hover:text-white/50"
            style={{ color: "rgba(255,255,255,0.25)" }}
          >
            <Plus size={11} />
            Add subtask
          </button>
        )}
      </div>
    )}
    </div>
  )
}
