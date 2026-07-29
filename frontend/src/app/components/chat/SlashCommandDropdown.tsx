"use client"

import { useState, useEffect, useRef, useCallback } from "react"
import { createPortal } from "react-dom"
import { Slash } from "lucide-react"
import useSlashCommands, { type SlashCommand } from "../../hooks/useSlashCommands"

interface SlashCommandDropdownProps {
  anchorRef: React.RefObject<HTMLElement | null>
  query: string  // text after the leading "/"
  onSelect: (command: SlashCommand) => void
  onClose: () => void
}

export default function SlashCommandDropdown({
  anchorRef,
  query,
  onSelect,
  onClose,
}: SlashCommandDropdownProps) {
  const { commands } = useSlashCommands()
  const [activeIndex, setActiveIndex] = useState(0)
  // Ref shadow of activeIndex. The keyboard handler is bridged through a
  // window-attached function (`__slashDropdownKeyDown`) which can read a
  // stale closure of `activeIndex` between rapid arrow + enter presses
  // before the next render commits. The ref is updated in the same tick
  // as the state setter, so reading it gives the live value regardless of
  // when the bridged handler is invoked.
  const activeIndexRef = useRef(0)
  const [pos, setPos] = useState({ bottom: 0, left: 0, width: 400 })

  const q = query.trim().toLowerCase()
  const filtered = q
    ? commands.filter(
        (c) => c.slug.includes(q) || c.name.toLowerCase().includes(q),
      )
    : commands

  const filteredRef = useRef(filtered)
  filteredRef.current = filtered

  // Reset active index when filter changes
  useEffect(() => {
    setActiveIndex(0)
    activeIndexRef.current = 0
  }, [q])

  // Position above the anchor (same pattern as MentionDropdown)
  useEffect(() => {
    if (!anchorRef.current) return
    const computePos = () => {
      const rect = anchorRef.current!.getBoundingClientRect()
      const margin = 8
      const maxWidth = Math.max(240, Math.min(420, window.innerWidth - margin * 2))
      const width = Math.min(rect.width, maxWidth)
      const left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin))
      setPos({ bottom: window.innerHeight - rect.top + 8, left, width })
    }
    computePos()
    window.addEventListener("resize", computePos)
    return () => window.removeEventListener("resize", computePos)
  }, [anchorRef])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    const items = filteredRef.current
    if (items.length === 0) {
      if (e.key === "Escape") onClose()
      return
    }
    if (e.key === "ArrowDown") {
      e.preventDefault()
      // Ref is the source of truth (read from it synchronously above and on
      // bridged Enter below). Compute `next` outside the state updater so
      // the updater stays pure — React may call it multiple times under
      // StrictMode or concurrent rendering, and mutating the ref inside
      // would over-advance or leave the ref out of sync on aborted updates.
      const next = activeIndexRef.current < items.length - 1 ? activeIndexRef.current + 1 : 0
      activeIndexRef.current = next
      setActiveIndex(next)
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      const next = activeIndexRef.current > 0 ? activeIndexRef.current - 1 : items.length - 1
      activeIndexRef.current = next
      setActiveIndex(next)
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault()
      // Read from the ref so rapid Arrow+Enter is correct even if the
      // bridged handler captured a stale closure.
      onSelect(items[activeIndexRef.current] || items[0])
    } else if (e.key === "Escape") {
      e.preventDefault()
      onClose()
    }
  }, [onClose, onSelect])

  // Bridge keyboard events from ChatInput's contentEditable, same pattern the
  // mention dropdown uses (shared `__mentionDropdownKeyDown` is fine because
  // only one of the two dropdowns is open at a time, but use a separate slot
  // to avoid race conditions if that ever changes).
  useEffect(() => {
    (window as any).__slashDropdownKeyDown = handleKeyDown
    return () => { delete (window as any).__slashDropdownKeyDown }
  }, [handleKeyDown])

  if (filtered.length === 0) return null

  const dropdown = (
    <div
      className="fixed z-[9999] rounded-xl overflow-hidden shadow-2xl"
      style={{
        bottom: pos.bottom,
        left: pos.left,
        width: pos.width,
        maxHeight: 360,
        background: "rgba(24,24,24,0.95)",
        border: "1px solid rgba(255,255,255,0.1)",
        backdropFilter: "blur(16px)",
      }}
      onMouseDown={(e) => e.preventDefault()}
    >
      <div
        className="flex items-center gap-2 px-3 py-2 text-[11px] uppercase tracking-wider"
        style={{
          color: "var(--text-tertiary)",
          borderBottom: "1px solid rgba(255,255,255,0.06)",
        }}
      >
        <Slash size={11} />
        <span>Commands</span>
      </div>
      <div className="overflow-y-auto" style={{ maxHeight: 320 }}>
        {filtered.map((cmd, i) => {
          const isActive = i === activeIndex
          return (
            <button
              key={cmd.slug}
              onMouseEnter={() => { setActiveIndex(i); activeIndexRef.current = i }}
              onClick={() => onSelect(cmd)}
              className="w-full flex items-start gap-3 px-3 py-2 text-left transition-colors"
              style={{
                background: isActive ? "rgba(124,58,237,0.15)" : "transparent",
              }}
            >
              <span
                className="font-mono text-[12px] mt-[2px] px-1.5 py-0.5 rounded shrink-0"
                style={{
                  background: "rgba(255,255,255,0.06)",
                  color: "var(--text-primary)",
                  border: "1px solid rgba(255,255,255,0.08)",
                }}
              >
                /{cmd.slug}
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-[13px]" style={{ color: "var(--text-primary)" }}>
                  {cmd.name}
                </div>
                <div className="text-[11px] mt-0.5" style={{ color: "var(--text-tertiary)" }}>
                  {cmd.description}
                </div>
                {cmd.param_hint && cmd.param_hint !== "(no args)" && (
                  <div
                    className="text-[10px] mt-0.5 font-mono"
                    style={{ color: "var(--text-tertiary)", opacity: 0.7 }}
                  >
                    {cmd.param_hint}
                  </div>
                )}
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )

  if (typeof document === "undefined") return null
  return createPortal(dropdown, document.body)
}
