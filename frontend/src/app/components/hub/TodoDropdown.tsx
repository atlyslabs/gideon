"use client"

import { useState, useRef, useEffect } from "react"
import { createPortal } from "react-dom"
import {
  BarChart3,
  Check,
  ChevronRight,
  Clock,
  ListTree,
  Trash2,
  UserPlus,
  Zap,
} from "lucide-react"
import type { Todo } from "../../types"
import { PriorityBarsIcon } from "./TodoItem"
import { getSnoozeOptions } from "./utils"

// ---------------------------------------------------------------------------
// SnoozeCustomDate (inline in submenu)
// ---------------------------------------------------------------------------

function SnoozeCustomDate({ onSnooze, onClose }: { onSnooze: (until: string) => void; onClose: () => void }) {
  const [value, setValue] = useState("")
  return (
    <div className="px-3 py-2" style={{ borderTop: "1px solid rgba(255,255,255,0.08)" }}>
      <input
        type="datetime-local"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="w-full bg-transparent text-[11px] outline-none rounded px-2 py-1.5"
        style={{
          color: "rgba(255,255,255,1)",
          border: "1px solid rgba(255,255,255,0.12)",
          colorScheme: "dark",
        }}
      />
      {value && (
        <button
          onClick={() => { onSnooze(new Date(value).toISOString()); onClose() }}
          className="w-full mt-1.5 px-2 py-1.5 rounded text-[11px] font-medium transition-colors"
          style={{ background: "var(--accent)", color: "#fff" }}
        >
          Confirm
        </button>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// SubmenuTrigger
// ---------------------------------------------------------------------------

function SubmenuTrigger({
  label,
  icon,
  open,
  onHover,
  children,
}: {
  label: string
  icon: React.ReactNode
  open: boolean
  onHover: () => void
  children: React.ReactNode
}) {
  const triggerRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null)

  useEffect(() => {
    if (open && triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect()
      setPos({
        top: rect.top,
        right: window.innerWidth - rect.left + 4,
      })
    }
  }, [open])

  return (
    <div ref={triggerRef} onMouseEnter={onHover} onClick={(e) => { e.stopPropagation(); onHover() }}>
      <div
        className="flex items-center justify-between w-full px-3 py-2 text-[13px] text-left hover:bg-white/10 transition-colors cursor-pointer"
        style={{ color: "rgba(255,255,255,1)" }}
      >
        <span className="flex items-center gap-2">
          {icon} {label}
        </span>
        <ChevronRight size={12} style={{ color: "rgba(255,255,255,1)" }} />
      </div>

      {open && pos && createPortal(
        <div
          className="fixed rounded-lg py-1 animate-[fadeIn_120ms_ease-out]"
          style={{
            top: pos.top,
            right: pos.right,
            background: "rgba(8,8,8,0.72)",
            backdropFilter: "blur(48px) saturate(1.8)",
            WebkitBackdropFilter: "blur(48px) saturate(1.8)",
            border: "1px solid rgba(255,255,255,0.06)",
            boxShadow: "0 12px 40px rgba(0,0,0,0.5), inset 0 0.5px 0 rgba(255,255,255,0.05)",
            minWidth: 140,
            zIndex: 9999,
          }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          {children}
        </div>,
        document.body
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// TodoDropdown
// ---------------------------------------------------------------------------

export default function TodoDropdown({
  todo,
  onAssign,
  onDelegate,
  onPriority,
  onDelete,
  onSnooze,
  onAddSubtask,
  onClose,
  isAssigning,
}: {
  todo: Todo
  onAssign: () => void
  onDelegate?: (e?: React.MouseEvent) => void
  onPriority: (p: Todo["priority"]) => void
  onDelete: () => void
  onSnooze: (until: string) => void
  onAddSubtask?: () => void
  onClose: () => void
  isAssigning: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [showPriority, setShowPriority] = useState(false)
  const [showSnooze, setShowSnooze] = useState(false)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [onClose])

  const priorities: { label: string; value: Todo["priority"] }[] = [
    { label: "None", value: "none" },
    { label: "Low", value: "low" },
    { label: "Medium", value: "medium" },
    { label: "High", value: "high" },
    { label: "Urgent", value: "urgent" },
  ]

  return (
    <div
      ref={ref}
      onClick={(e) => e.stopPropagation()}
      onMouseLeave={() => { setShowPriority(false); setShowSnooze(false) }}
      className="absolute right-0 top-6 z-50 rounded-lg shadow-xl animate-[fadeIn_120ms_ease-out]"
      style={{
        background: "rgba(8,8,8,0.72)",
        backdropFilter: "blur(48px) saturate(1.8)",
        WebkitBackdropFilter: "blur(48px) saturate(1.8)",
        border: "1px solid rgba(255,255,255,0.06)",
        boxShadow: "0 12px 40px rgba(0,0,0,0.5), inset 0 0.5px 0 rgba(255,255,255,0.05)",
        minWidth: 180,
      }}
    >
      <SubmenuTrigger
        label="Priority"
        icon={<BarChart3 size={13} />}
        open={showPriority}
        onHover={() => { setShowPriority(true); setShowSnooze(false) }}
      >
        {priorities.map((p) => (
          <button
            key={p.value}
            onClick={() => { onPriority(p.value); onClose() }}
            className="flex items-center justify-between w-full px-3 py-2 text-[12px] text-left hover:bg-white/10 transition-colors"
            style={{ color: "rgba(255,255,255,1)" }}
          >
            <span className="flex items-center gap-2">
              {p.value !== "none" && (
                <PriorityBarsIcon priority={p.value as Todo["priority"]} size={11} />
              )}
              {p.label}
            </span>
            {todo.priority === p.value && (
              <Check size={11} style={{ color: "rgba(255,255,255,0.6)" }} />
            )}
          </button>
        ))}
      </SubmenuTrigger>

      <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)" }} />

      <SubmenuTrigger
        label="Snooze"
        icon={<Clock size={13} />}
        open={showSnooze}
        onHover={() => { setShowSnooze(true); setShowPriority(false) }}
      >
        {getSnoozeOptions().map((opt) => (
          <button
            key={opt.label}
            onClick={() => { onSnooze(opt.value); onClose() }}
            className="flex items-center gap-2 w-full px-3 py-2 text-[12px] text-left hover:bg-white/10 transition-colors whitespace-nowrap"
            style={{ color: "rgba(255,255,255,1)" }}
          >
            {opt.label}
          </button>
        ))}
        <SnoozeCustomDate onSnooze={onSnooze} onClose={onClose} />
      </SubmenuTrigger>

      <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)" }} />

      {onAddSubtask && (
        <button
          onClick={() => { onAddSubtask(); onClose() }}
          onMouseEnter={() => { setShowPriority(false); setShowSnooze(false) }}
          className="flex items-center gap-2 w-full px-3 py-2 text-[13px] text-left hover:bg-white/10 transition-colors"
          style={{ color: "rgba(255,255,255,1)" }}
        >
          <ListTree size={13} /> Add subtask
        </button>
      )}

      {onDelegate && (
        <button
          onClick={(e) => { onDelegate(e); onClose() }}
          onMouseEnter={() => { setShowPriority(false); setShowSnooze(false) }}
          className="flex items-center gap-2 w-full px-3 py-2 text-[13px] text-left hover:bg-white/10 transition-colors"
          style={{ color: "rgba(59,130,246,0.9)" }}
        >
          <UserPlus size={13} /> {(todo.delegated_to?.length ?? 0) > 0 ? "Reassign" : "Delegate"}
        </button>
      )}

      {!todo.assigned_to_gideon && (
        <button
          onClick={() => { onAssign(); onClose() }}
          onMouseEnter={() => { setShowPriority(false); setShowSnooze(false) }}
          disabled={isAssigning}
          className="flex items-center gap-2 w-full px-3 py-2 text-[13px] text-left hover:bg-white/10 transition-colors"
          style={{ color: "var(--accent)", opacity: isAssigning ? 0.5 : 1 }}
        >
          <Zap size={13} /> {isAssigning ? "Assigning..." : "Assign to Gideon"}
        </button>
      )}

      <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)" }} />

      <button
        onClick={() => { onDelete(); onClose() }}
        onMouseEnter={() => { setShowPriority(false); setShowSnooze(false) }}
        className="flex items-center gap-2 w-full px-3 py-2 text-[13px] text-left hover:bg-white/10 transition-colors"
        style={{ color: "#ef4444" }}
      >
        <Trash2 size={13} /> Delete todo
      </button>
    </div>
  )
}
