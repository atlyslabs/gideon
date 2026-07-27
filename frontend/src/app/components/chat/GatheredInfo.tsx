"use client"

import { useState, useEffect, useRef } from "react"
import { ChevronRight } from "lucide-react"

interface GatheredInfoProps {
  children: React.ReactNode
  streaming?: boolean
  hasContent?: boolean // true when the assistant has started producing text
}

/**
 * Collapsible "Gathered information" section that groups thinking + tool calls.
 * Auto-collapses once the assistant starts producing text content.
 */
export default function GatheredInfo({ children, streaming, hasContent }: GatheredInfoProps) {
  const [open, setOpen] = useState(true)
  const autoCollapsed = useRef(false)

  // Auto-collapse when content arrives (assistant started responding)
  useEffect(() => {
    if (hasContent && !autoCollapsed.current) {
      autoCollapsed.current = true
      setOpen(false)
    }
  }, [hasContent])

  return (
    <div className="mb-1.5">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-xs py-0.5 hover:opacity-80 transition-opacity"
        style={{ color: "var(--text-secondary)" }}
      >
        <span style={{ color: "var(--text-tertiary)", fontSize: 11, fontWeight: 500 }}>
          Gathered information
        </span>
        <ChevronRight
          size={12}
          style={{
            transform: open ? "rotate(90deg)" : "rotate(0deg)",
            transition: "transform 150ms ease",
            color: "var(--text-tertiary)",
          }}
        />
      </button>
      <div
        style={{
          display: "grid",
          gridTemplateRows: open ? "1fr" : "0fr",
          opacity: open ? 1 : 0,
          transition: "grid-template-rows 200ms ease, opacity 150ms ease",
        }}
      >
        <div style={{ overflow: "hidden" }}>
          <div
            className="ml-1 pl-2.5 mt-0.5"
            style={{ borderLeft: "1px solid var(--border)" }}
          >
            {children}
          </div>
        </div>
      </div>
    </div>
  )
}
