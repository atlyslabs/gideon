"use client"

import { useState, useRef, useEffect } from "react"
import { Clock, Calendar } from "lucide-react"
import { getSnoozeOptions } from "./utils"

export default function SnoozePopover({
  onSnooze,
  onClose,
}: {
  onSnooze: (until: string) => void
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [showCustom, setShowCustom] = useState(false)
  const [customDate, setCustomDate] = useState("")
  const options = getSnoozeOptions()

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [onClose])

  return (
    <div
      ref={ref}
      onClick={(e) => e.stopPropagation()}
      className="absolute right-0 top-6 z-50 rounded-lg overflow-hidden shadow-xl"
      style={{
        background: "rgba(8,8,8,0.72)",
        backdropFilter: "blur(48px) saturate(1.8)",
        WebkitBackdropFilter: "blur(48px) saturate(1.8)",
        border: "1px solid rgba(255,255,255,0.06)",
        boxShadow: "0 12px 40px rgba(0,0,0,0.5), inset 0 0.5px 0 rgba(255,255,255,0.05)",
        width: 200,
      }}
    >
      <div className="px-3 py-2 text-[10px] font-semibold tracking-wider uppercase" style={{ color: "rgba(255,255,255,1)" }}>
        Snooze until
      </div>
      {options.map((opt) => (
        <button
          key={opt.label}
          onClick={() => { onSnooze(opt.value); onClose() }}
          className="flex items-center gap-2 w-full px-3 py-2 text-[13px] text-left hover:bg-white/10 transition-colors"
          style={{ color: "rgba(255,255,255,1)" }}
        >
          <Clock size={13} style={{ color: "rgba(255,255,255,1)" }} />
          {opt.label}
        </button>
      ))}
      <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)" }} />
      {showCustom ? (
        <div className="px-3 py-2">
          <input
            type="datetime-local"
            value={customDate}
            onChange={(e) => setCustomDate(e.target.value)}
            className="w-full bg-transparent text-[12px] outline-none rounded px-2 py-1.5"
            style={{
              color: "rgba(255,255,255,1)",
              border: "1px solid rgba(255,255,255,0.15)",
              colorScheme: "dark",
            }}
            autoFocus
          />
          <button
            onClick={() => {
              if (customDate) {
                onSnooze(new Date(customDate).toISOString())
                onClose()
              }
            }}
            disabled={!customDate}
            className="w-full mt-1.5 px-2 py-1.5 rounded text-[12px] font-medium transition-colors"
            style={{
              background: customDate ? "var(--accent)" : "rgba(255,255,255,0.05)",
              color: customDate ? "#fff" : "rgba(255,255,255,0.3)",
            }}
          >
            Confirm
          </button>
        </div>
      ) : (
        <button
          onClick={() => setShowCustom(true)}
          className="flex items-center gap-2 w-full px-3 py-2 text-[13px] text-left hover:bg-white/10 transition-colors"
          style={{ color: "rgba(255,255,255,1)" }}
        >
          <Calendar size={13} style={{ color: "rgba(255,255,255,1)" }} />
          Pick date & time
        </button>
      )}
    </div>
  )
}
