"use client"

import { useState } from "react"
import { Plus, Clock, Zap } from "lucide-react"
import useWorkflows from "../hooks/useWorkflows"
import type { Workflow } from "../types"
import { getIntegrationIcon } from "../utils/integrationIcons"

function formatScheduleSummary(w: Workflow): string {
  if (w.when_type === "trigger") {
    const integ = w.trigger_integration || "event"
    const mins = w.trigger_minutes_before
    if (mins) return `${mins} minutes before an event starts in a specific calendar`
    return `Trigger: ${integ} on ${w.trigger_event || "event"}`
  }
  const unit = w.schedule_unit || "day"
  const interval = w.schedule_interval || 1
  const time = w.schedule_time || "09:00"
  const day = w.schedule_day_of_week
  const days = w.schedule_days_of_week
  const isInterval = unit === "minute" || unit === "hour"
  let s = `Every ${interval > 1 ? interval + " " : ""}${unit}${interval > 1 ? "s" : ""}`
  if (unit === "week" && day) s += ` on ${day}`
  // Weekday filter applies to every-day and to sub-daily intervals.
  if ((unit === "day" || isInterval) && days && days.length > 0 && days.length < 7) {
    const set = new Set(days)
    const wd = ["MO", "TU", "WE", "TH", "FR"]
    const we = ["SA", "SU"]
    if (set.size === 5 && wd.every((d) => set.has(d))) s += " on weekdays"
    else if (set.size === 2 && we.every((d) => set.has(d))) s += " on weekends"
    else s += ` on ${days.join(", ")}`
  }
  // Sub-daily intervals repeat through the day — no fixed time-of-day.
  if (!isInterval) s += ` at ${formatTime12(time)}`
  return s
}

function formatTime12(time24: string): string {
  const [h, m] = time24.split(":").map(Number)
  const ampm = h >= 12 ? "PM" : "AM"
  const h12 = h % 12 || 12
  return `${String(h12).padStart(2, "0")}:${String(m).padStart(2, "0")} ${ampm}`
}

function formatRunTime(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ""
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  })
}

// Status line shown on each card: last run outcome + when.
function lastRunInfo(w: Workflow): { label: string; color: string } {
  const status = w.last_run_status
  const when = w.last_run_at ? formatRunTime(w.last_run_at) : ""
  if (status === "completed")
    return { label: when ? `Last run succeeded · ${when}` : "Last run succeeded", color: "#34d399" }
  if (status === "failed")
    return { label: when ? `Last run failed · ${when}` : "Last run failed", color: "#f87171" }
  if (status === "running")
    return { label: when ? `Running since ${when}` : "Running now", color: "#fbbf24" }
  if (status)
    return { label: when ? `Last run ${status} · ${when}` : `Last run ${status}`, color: "#9ca3af" }
  return { label: "Not run yet", color: "#6b7280" }
}

function LastRunBadge({ wf }: { wf: Workflow }) {
  const info = lastRunInfo(wf)
  return (
    <div className="flex items-center gap-1.5">
      <span
        className="h-1.5 w-1.5 shrink-0 rounded-full"
        style={{ backgroundColor: info.color }}
      />
      <span className="text-[12px]" style={{ color: info.color }}>
        {info.label}
      </span>
    </div>
  )
}

// Card border uses ~25% of full frost (matches `before:opacity-25` in the design).
const CARD_BORDER_BG =
  "linear-gradient(to bottom right, rgba(255,255,255,0.25) 0%, rgba(255,255,255,0.08) 42%, rgba(255,255,255,0.08) 62%, rgba(255,255,255,0.25) 100%)"

// Tile border uses ~30% of full frost (matches `before:opacity-30`).
const TILE_BORDER_BG =
  "linear-gradient(to bottom right, rgba(255,255,255,0.3) 0%, rgba(255,255,255,0.1) 42%, rgba(255,255,255,0.1) 62%, rgba(255,255,255,0.3) 100%)"

const cardFrostStyle: React.CSSProperties = {
  backgroundImage: `linear-gradient(to bottom, #181818, #131313), ${CARD_BORDER_BG}`,
  backgroundOrigin: "border-box",
  backgroundClip: "padding-box, border-box",
  border: "0.5px solid transparent",
}

const tileFrostStyle: React.CSSProperties = {
  backgroundImage: `linear-gradient(to bottom, #1a1a1a, #131313), ${TILE_BORDER_BG}`,
  backgroundOrigin: "border-box",
  backgroundClip: "padding-box, border-box",
  border: "0.5px solid transparent",
}

interface WorkflowsPageProps {
  onBack: () => void
  onSelectWorkflow: (workflow: Workflow) => void
  onCreateWorkflow: () => void
}

export default function WorkflowsPage({ onSelectWorkflow, onCreateWorkflow }: WorkflowsPageProps) {
  const { workflows, loading, activateWorkflow, deactivateWorkflow } = useWorkflows()
  const [pendingToggle, setPendingToggle] = useState<string | null>(null)
  // `null` = the user hasn't picked a filter yet, so we auto-default below.
  // Once they tap a chip it holds a concrete value and the auto-default stops
  // applying (even across re-renders / data refreshes).
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "inactive" | null>(null)

  const hasActiveWorkflow = workflows.some((w) => (w.status || "inactive") === "active")
  // Open on "active" so the page shows what's actually running — but only if at
  // least one active workflow exists, otherwise fall back to "all" so a user
  // whose workflows are all inactive doesn't land on an empty view.
  const effectiveFilter: "all" | "active" | "inactive" =
    statusFilter ?? (hasActiveWorkflow ? "active" : "all")

  // Treat a missing status as inactive (matches the toggle's own fallback).
  const filteredWorkflows =
    effectiveFilter === "all"
      ? workflows
      : workflows.filter((w) => (w.status || "inactive") === effectiveFilter)

  const handleToggle = async (e: React.MouseEvent, wf: Workflow) => {
    e.stopPropagation()
    e.preventDefault()
    if (pendingToggle === wf.id) return
    setPendingToggle(wf.id)
    try {
      if (wf.status === "active") {
        await deactivateWorkflow(wf.id)
      } else {
        await activateWorkflow(wf.id)
      }
    } finally {
      setPendingToggle(null)
    }
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden bg-[#0c0c0c]">
      <div className="flex-1 overflow-y-auto">
        <div className="relative min-h-full px-4 py-6 pt-14 sm:p-12 sm:pt-24 md:pt-24">
          {/* Header */}
          <div className="max-w-6xl mx-auto">
            <div className="text-center">
              <h1 className="text-[36px] font-semibold tracking-tight text-white">Workflows</h1>
              <p className="text-[14px] mt-2 text-gray-400">
                Create a scheduled or trigger-based workflow.
              </p>
            </div>
            <div className="flex justify-end items-center gap-3 mt-6">
              {/* Status filter — only meaningful once at least one workflow exists. */}
              {workflows.length > 0 && (
                <div
                  className="inline-flex items-center rounded-full p-0.5"
                  style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)" }}
                >
                  {(["all", "active", "inactive"] as const).map((opt) => {
                    const selected = effectiveFilter === opt
                    return (
                      <button
                        key={opt}
                        type="button"
                        onClick={() => setStatusFilter(opt)}
                        aria-pressed={selected}
                        className="px-3 py-1.5 rounded-full text-[12px] font-medium capitalize transition-colors"
                        style={{
                          background: selected ? "rgba(124,58,237,0.25)" : "transparent",
                          color: selected ? "#c4b5fd" : "rgba(255,255,255,0.55)",
                        }}
                      >
                        {opt}
                      </button>
                    )
                  })}
                </div>
              )}
              <button
                onClick={onCreateWorkflow}
                className="flex items-center gap-2 px-4 py-2 rounded-full text-[13px] font-medium text-white transition-opacity hover:opacity-90"
                style={{
                  background: "linear-gradient(to bottom, #8b5cf6, #6d28d9)",
                  boxShadow: "0 1px 0 rgba(255,255,255,0.2) inset, 0 8px 24px rgba(124,58,237,0.35)",
                }}
              >
                <Plus size={14} />
                Create Workflow
              </button>
            </div>
          </div>

          {/* Grid */}
          <div className="max-w-6xl mx-auto mt-8">
            {loading ? (
              <div className="flex items-center justify-center py-20">
                <div className="spinner" />
              </div>
            ) : workflows.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-20">
                <div className="mb-4">
                  <svg viewBox="0 0 48 48" width="48" height="48" fill="none" stroke="#555" strokeWidth="1.5">
                    <rect x="8" y="8" width="32" height="32" rx="6" />
                    <path d="M16 24h16M24 16v16" strokeLinecap="round" />
                  </svg>
                </div>
                <p className="text-[14px] mb-1 text-gray-400">No workflows yet.</p>
                <p className="text-[13px] text-gray-500">
                  Create your first workflow to automate tasks on a schedule or trigger.
                </p>
              </div>
            ) : filteredWorkflows.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-20">
                <p className="text-[14px] text-gray-400">No {effectiveFilter} workflows.</p>
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {filteredWorkflows.map((wf) => (
                  // Card is a div with role=button (not an <a>) because it
                  // contains a real <button> for the active/inactive toggle.
                  // Nesting interactive elements inside <a> is invalid HTML
                  // and inconsistent across browsers/screen readers.
                  <div
                    key={wf.id}
                    role="button"
                    tabIndex={0}
                    aria-label={`Open workflow ${wf.name || "Untitled workflow"}`}
                    onClick={() => onSelectWorkflow(wf)}
                    onKeyDown={(e) => {
                      // Only react when focus is on the card itself. Without
                      // this check, Enter/Space pressed while the inner
                      // toggle button is focused would bubble up here and
                      // open the workflow instead of just toggling state.
                      if (e.target !== e.currentTarget) return
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault()
                        onSelectWorkflow(wf)
                      }
                    }}
                    className="flex flex-col p-6 rounded-3xl backdrop-blur-sm transition duration-200 hover:brightness-110 shadow-[0px_0px_0px_0.5px_rgba(0,0,0,0.4)] cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/50"
                    style={cardFrostStyle}
                  >
                    {/* Top row: integration tiles + status pill */}
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex flex-wrap gap-2">
                        {wf.integrations_used.length > 0 ? (
                          wf.integrations_used.slice(0, 4).map((integ) => (
                            <span
                              key={integ}
                              className="flex items-center justify-center rounded-lg backdrop-blur-sm size-7 shrink-0"
                              style={tileFrostStyle}
                            >
                              {getIntegrationIcon(integ, 14)}
                            </span>
                          ))
                        ) : (
                          <span
                            className="flex items-center justify-center rounded-lg backdrop-blur-sm size-7 shrink-0"
                            style={tileFrostStyle}
                          >
                            <Zap size={14} className="text-violet-400" />
                          </span>
                        )}
                      </div>
                      <button
                        type="button"
                        aria-pressed={wf.status === "active"}
                        onClick={(e) => handleToggle(e, wf)}
                        disabled={pendingToggle === wf.id}
                        className="rounded-[10px] backdrop-blur-sm transition-opacity hover:opacity-90 active:opacity-80 disabled:opacity-70"
                        style={{
                          // Outer backdrop: soft emerald wash for active, the
                          // existing dark gradient for inactive. Glass effect
                          // is preserved — only the tint changes.
                          backgroundImage:
                            wf.status === "active"
                              ? "linear-gradient(to bottom, rgba(16,185,129,0.22), rgba(16,185,129,0.08))"
                              : "linear-gradient(to bottom, #0f0f0f, #1e1e1e)",
                        }}
                      >
                        <span
                          className="flex items-center gap-1.5 px-2.5 py-[5px] rounded-[10px] backdrop-blur-sm"
                          style={{
                            // Same border-box double-gradient pattern that
                            // gives the pill its frosted-glass border. For
                            // active we shift the border-side gradient from
                            // silver (#c1c1c1) to emerald so the rim glows.
                            backgroundImage:
                              wf.status === "active"
                                ? "linear-gradient(to bottom, rgba(16,185,129,0.10) 5%, transparent), linear-gradient(to bottom, rgba(16,185,129,0.25) 40%, rgba(52,211,153,0.6) 100%)"
                                : "linear-gradient(to bottom, #141414 5%, transparent), linear-gradient(to bottom, rgba(0,0,0,0.15) 40%, rgba(193,193,193,0.3) 100%)",
                            backgroundOrigin: "border-box",
                            backgroundClip: "padding-box, border-box",
                            border: "0.9px solid transparent",
                          }}
                        >
                          <span
                            className="w-1.5 h-1.5 rounded-full shrink-0"
                            style={{
                              background: wf.status === "active" ? "#10b981" : "#6b7280",
                              boxShadow:
                                wf.status === "active"
                                  ? "0 0 6px rgba(16,185,129,0.7)"
                                  : "none",
                            }}
                          />
                          <span
                            className="text-[12px] font-medium"
                            style={{
                              color: wf.status === "active" ? "#34d399" : "rgba(255,255,255,0.55)",
                            }}
                          >
                            {wf.status === "active" ? "Active" : "Inactive"}
                          </span>
                        </span>
                      </button>
                    </div>

                    {/* Title */}
                    <h3 className="mt-4 line-clamp-2 text-[15px] font-medium text-white">
                      {wf.name || "Untitled workflow"}
                    </h3>

                    {/* Description */}
                    <p className="mt-1 line-clamp-3 text-[13px] leading-relaxed text-gray-400 mb-4">
                      {wf.description || "Click to edit your workflow"}
                    </p>

                    {/* Footer: schedule/trigger summary + last-run status */}
                    {wf.name ? (
                      <div className="mt-auto flex flex-col gap-1.5">
                        <div className="flex items-start gap-1.5">
                          {wf.when_type === "schedule" ? (
                            <Clock size={14} className="shrink-0 text-gray-500 mt-[1px]" />
                          ) : (
                            <Zap size={14} className="shrink-0 text-gray-500 mt-[1px]" />
                          )}
                          <span className="text-[12px] text-gray-400">{formatScheduleSummary(wf)}</span>
                        </div>
                        <LastRunBadge wf={wf} />
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
