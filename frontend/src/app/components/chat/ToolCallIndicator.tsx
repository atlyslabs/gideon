"use client"

import { Loader2 } from "lucide-react"
import { pingman } from "../../utils/pingman"
import { getIntegrationIcon } from "../../utils/integrationIcons"

interface ToolCallIndicatorProps {
  toolName: string
  status: "running" | "done"
  summary?: string
  reconnect?: string
}

/** Map tool name to integration provider for icon lookup */
function getToolProvider(name: string): string {
  if (name.includes("email") || name.includes("gmail") || name.includes("draft")) return "gmail"
  if (name.includes("calendar") || name.includes("event")) return "google_calendar"
  if (name.includes("slack") || name.includes("channel")) return "slack"
  if (name.includes("notion")) return "notion"
  if (name.includes("linear") || name.includes("issue")) return "linear"
  if (name.includes("github") || name.includes("pull_request")) return "github"
  if (name.includes("grafana") || name.includes("dashboard")) return "grafana"
  if (name.includes("drive") || name.includes("file")) return "google_drive"
  if (name.includes("sheet") || name.includes("spreadsheet")) return "google_sheets"
  if (name.includes("doc")) return "google_docs"
  if (name.includes("slide")) return "google_slides"
  if (name.includes("web_search")) return "google"
  if (name.includes("analytics") || name.includes("table_schema")) return "clickhouse"
  if (name.includes("granola")) return "granola"
  if (name.includes("workflow")) return "workflow"
  if (name.includes("posthog")) return "posthog"
  if (name.includes("figma")) return "figma"
  if (name.includes("ramp")) return "ramp"
  if (name.includes("semrush")) return "semrush"
  if (name.includes("recruitee")) return "recruitee"
  if (name.includes("search_console") || name.includes("sitemap") || name.includes("inspect_url")) return "google_search_console"
  if (name.includes("todo") || name.includes("memory") || name.includes("activity")) return ""
  return ""
}

/** Human-readable tool label */
function getToolLabel(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

export { getToolProvider, getToolLabel }

export default function ToolCallIndicator({ toolName, status, summary, reconnect }: ToolCallIndicatorProps) {
  const handleReconnect = async () => {
    if (!reconnect) return
    try {
      const res = await pingman.get<{ auth_url: string }>(
        `/api/gideon/v1/integrations/${reconnect}/auth-url`
      )
      if (res.ok && res.data.auth_url) {
        window.open(res.data.auth_url, "_blank", "width=600,height=700,popup=yes")
      }
    } catch {
      window.location.href = "/settings"
    }
  }

  const provider = getToolProvider(toolName)
  const isRunning = status === "running"
  const isError = !!reconnect

  return (
    <div className="flex items-center gap-2 py-0.5 text-xs">
      {/* Icon */}
      <div className="shrink-0 flex items-center justify-center" style={{ width: 16, height: 16, opacity: 0.7 }}>
        {isRunning ? (
          <Loader2 size={12} className="animate-spin" style={{ color: "var(--text-tertiary)" }} />
        ) : isError ? (
          <div className="w-2 h-2 rounded-full" style={{ background: "#ef4444" }} />
        ) : (
          provider ? getIntegrationIcon(provider, 14) : (
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="3" fill="var(--text-tertiary)" opacity="0.5" /></svg>
          )
        )}
      </div>

      {/* Label */}
      <span className="flex-1 truncate" style={{ color: "var(--text-tertiary)", fontSize: 12 }}>
        {isError
          ? (summary || getToolLabel(toolName))
          : isRunning
            ? getToolLabel(toolName)
            : (summary && summary !== "Done" ? summary : `${getToolLabel(toolName)} completed.`)
        }
      </span>

      {/* Reconnect button */}
      {reconnect && (
        <button
          onClick={handleReconnect}
          className="ml-1 px-2 py-0.5 rounded text-[10px] font-medium transition-colors shrink-0"
          style={{ background: "var(--accent)", color: "#fff" }}
        >
          Reconnect
        </button>
      )}
    </div>
  )
}
