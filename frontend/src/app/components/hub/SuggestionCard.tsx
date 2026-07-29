"use client"

import { useRef, useEffect } from "react"
import { Sparkles, ExternalLink } from "lucide-react"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import type { TodoSuggestion } from "../../types"

// Only render suggestion source links that point at our expected Slack / Gmail
// permalink origins. Anything else (javascript:, data:, or an unexpected host)
// is dropped on the floor. The backend already synthesizes the URL from trusted
// prefixes; this is defense-in-depth for a field that ends up in an anchor href.
const ALLOWED_SOURCE_URL_PREFIXES = [
  "https://app.slack.com/",
  "https://mail.google.com/mail/u/0/#inbox/",
]

function safeSourceUrl(raw: string | null | undefined): string | null {
  if (!raw) return null
  try {
    const u = new URL(raw)
    if (u.protocol !== "https:") return null
  } catch {
    return null
  }
  return ALLOWED_SOURCE_URL_PREFIXES.some((p) => raw.startsWith(p)) ? raw : null
}

// ---------------------------------------------------------------------------
// SourceIcon
// ---------------------------------------------------------------------------

export function SourceIcon({ source }: { source: string | null }) {
  if (!source) return <Sparkles size={14} style={{ color: "var(--accent)" }} />
  return <>{getIntegrationIcon(source)}</>
}

// ---------------------------------------------------------------------------
// SuggestionDetailCard
// ---------------------------------------------------------------------------

export function SuggestionDetailCard({
  suggestion,
  onClose,
}: {
  suggestion: TodoSuggestion
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)

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
      className="rounded-xl p-4 shadow-2xl animate-fade-in-up"
      style={{
        background: "rgba(25,25,25,0.97)",
        backdropFilter: "blur(16px)",
        border: "1px solid rgba(255,255,255,0.1)",
      }}
    >
      <div className="flex items-center gap-2 mb-2">
        <SourceIcon source={suggestion.source_integration} />
        <span
          className="text-[10px] font-semibold tracking-wider uppercase"
          style={{ color: "rgba(255,255,255,1)" }}
        >
          Suggestion Details
        </span>
      </div>
      <p className="text-[13px] leading-relaxed" style={{ color: "rgba(255,255,255,1)" }}>
        {suggestion.detail || suggestion.text}
      </p>
      {(() => {
        const safeUrl = safeSourceUrl(suggestion.source_url)
        if (!safeUrl) return null
        return (
          <a
            href={safeUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="inline-flex items-center gap-1.5 mt-3 px-2.5 py-1.5 rounded-md text-[11px] font-medium transition-colors hover:bg-white/10"
            style={{
              background: "rgba(255,255,255,0.06)",
              border: "1px solid rgba(255,255,255,0.1)",
              color: "rgba(255,255,255,0.9)",
            }}
          >
            {getIntegrationIcon(suggestion.source_integration || "", 12)}
            Open in {suggestion.source_integration === "gmail" ? "Gmail" : suggestion.source_integration === "slack" ? "Slack" : "source"}
            <ExternalLink size={10} style={{ opacity: 0.6 }} />
          </a>
        )
      })()}
    </div>
  )
}
