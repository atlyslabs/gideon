"use client"

import { useState, useEffect } from "react"
import { Clock, ChevronDown, ExternalLink } from "lucide-react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { mdComponents } from "../chat/markdownConfig"
import type { WorkflowRun } from "../../types"
import RunStatusBadge from "./RunStatusBadge"
import useAppRouter from "../../hooks/useAppRouter"

interface HistoryTabProps {
  workflowId: string | null
  getRuns: (id: string) => Promise<WorkflowRun[]>
  refreshKey?: number
}

export default function HistoryTab({ workflowId, getRuns, refreshKey }: HistoryTabProps) {
  const [runs, setRuns] = useState<WorkflowRun[]>([])
  const [loading, setLoading] = useState(false)
  const [expandedRun, setExpandedRun] = useState<string | null>(null)
  const router = useAppRouter()

  useEffect(() => {
    if (!workflowId) return
    setLoading(true)
    getRuns(workflowId)
      .then(setRuns)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [workflowId, getRuns, refreshKey])

  // Poll while any run is still in progress (serialized to avoid overlap)
  const hasRunning = runs.some((r) => r.status === "running")
  useEffect(() => {
    if (!workflowId || !hasRunning) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      while (!cancelled) {
        await new Promise<void>((r) => { timer = setTimeout(r, 3000) })
        if (cancelled) break
        try {
          const fetched = await getRuns(workflowId)
          if (!cancelled) setRuns(fetched)
          if (!fetched.some((r) => r.status === "running")) break
        } catch { /* retry next iteration */ }
      }
    }
    poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [workflowId, getRuns, hasRunning])

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <div className="spinner" />
      </div>
    )
  }

  if (!workflowId || runs.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <div className="mb-4">
          <Clock size={32} style={{ color: "var(--text-tertiary)" }} />
        </div>
        <p className="text-[15px] font-medium mb-1" style={{ color: "var(--text-secondary)" }}>
          No workflow runs yet
        </p>
        <p className="text-[13px]" style={{ color: "var(--text-tertiary)" }}>
          Once a workflow is run, you can see the history here.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-2 mt-2">
      {runs.map((run) => {
        const triggeredAt = new Date(run.triggered_at)
        const label = triggeredAt.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
          " at " + triggeredAt.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true })

        return (
          <div
            key={run.id}
            className="rounded-lg overflow-hidden"
            style={{ background: "var(--surface-card)", border: "1px solid var(--border)" }}
          >
            <button
              onClick={() => setExpandedRun(expandedRun === run.id ? null : run.id)}
              className="w-full flex items-center justify-between px-4 py-3 text-left"
            >
              <div className="flex items-center gap-3">
                <span className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
                  {label}
                </span>
                <RunStatusBadge status={run.status} />
              </div>
              <ChevronDown
                size={14}
                style={{
                  color: "var(--text-tertiary)",
                  transform: expandedRun === run.id ? "rotate(180deg)" : "none",
                  transition: "transform 150ms",
                }}
              />
            </button>

            {expandedRun === run.id && (
              <div
                className="px-4 pb-3 pt-0"
                style={{ borderTop: "1px solid var(--border)" }}
              >
                {run.output && (
                  <div className="mt-3">
                    <p className="text-[11px] uppercase font-medium mb-1" style={{ color: "var(--text-tertiary)" }}>
                      Output
                    </p>
                    <div
                      className="workflow-output-md text-[13px] leading-relaxed rounded-lg p-3"
                      style={{
                        background: "var(--surface-elevated)",
                        color: "var(--text-secondary)",
                      }}
                    >
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                        {run.output}
                      </ReactMarkdown>
                    </div>
                  </div>
                )}
                {run.error && (
                  <div className="mt-3">
                    <p className="text-[11px] uppercase font-medium mb-1" style={{ color: "var(--error)" }}>
                      Error
                    </p>
                    <pre
                      className="text-[13px] leading-relaxed whitespace-pre-wrap rounded-lg p-3"
                      style={{
                        background: "rgba(239,68,68,0.06)",
                        color: "var(--error)",
                      }}
                    >
                      {run.error}
                    </pre>
                  </div>
                )}
                {!run.output && !run.error && (
                  <p className="text-[13px] mt-3" style={{ color: "var(--text-tertiary)" }}>
                    No output recorded.
                  </p>
                )}
                {run.completed_at && (
                  <p className="text-[11px] mt-2" style={{ color: "var(--text-tertiary)" }}>
                    Completed {new Date(run.completed_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true })}
                  </p>
                )}
                {workflowId && (
                  <button
                    onClick={() => router.navigate(`/workflows/${workflowId}/runs/${run.id}`)}
                    className="mt-3 flex items-center gap-1.5 text-[12px] font-medium transition-colors"
                    style={{ color: "#a78bfa" }}
                    onMouseEnter={(e) => { e.currentTarget.style.color = "#c4b5fd" }}
                    onMouseLeave={(e) => { e.currentTarget.style.color = "#a78bfa" }}
                  >
                    Open run page
                    <ExternalLink size={12} />
                  </button>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
