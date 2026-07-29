"use client"

import { useEffect, useState } from "react"
import { Loader2, Workflow } from "lucide-react"
import { pingman } from "../utils/pingman"

interface ActiveRun {
  run_id: string
  workflow_id: string
  workflow_name: string | null
  status: string
  last_round: number | null
  triggered_at: string
}

const POLL_MS = 5000

export default function RunningWorkflowsIndicator() {
  const [runs, setRuns] = useState<ActiveRun[]>([])
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      try {
        const res = await pingman.get<{ runs: ActiveRun[] }>("/api/gideon/v1/workflows/runs/active")
        if (!cancelled && res.ok) setRuns(res.data.runs || [])
      } catch (e) {
        // Transient — keep polling; log for diagnostics (a persistent failure
        // would otherwise be silently invisible).
        console.debug("[RunningWorkflowsIndicator] active-runs poll failed, will retry:", e)
      }
      if (!cancelled) timer = setTimeout(poll, POLL_MS)
    }
    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [])

  if (runs.length === 0) return null

  const openRun = (r: ActiveRun) => {
    window.open(`/workflows/${r.workflow_id}/runs/${r.run_id}`, "_blank", "noopener,noreferrer")
  }

  return (
    <div
      className="absolute bottom-24 right-5 z-30 flex flex-col items-end"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      {/* Popover: one card per running workflow */}
      {open && (
        <div className="mb-3 w-72 max-h-[60vh] overflow-y-auto flex flex-col gap-2">
          {runs.map((r) => {
            const interrupted = r.status === "interrupted"
            return (
              <button
                key={r.run_id}
                onClick={() => openRun(r)}
                className="group w-full text-left rounded-xl border p-3 transition-colors"
                style={{
                  background: "var(--surface-elevated, #161616)",
                  borderColor: "var(--border, #262626)",
                }}
              >
                <div className="flex items-center gap-2">
                  <Loader2
                    size={13}
                    className={`shrink-0 animate-spin ${interrupted ? "text-amber-400" : "text-emerald-400"}`}
                  />
                  <span className="truncate text-[13px] font-medium" style={{ color: "var(--text-primary, #fff)" }}>
                    {r.workflow_name || "Workflow run"}
                  </span>
                </div>
                <div className="mt-1.5 flex items-center justify-between gap-2">
                  <span className="text-[11px]" style={{ color: "var(--text-tertiary, #8a8a8a)" }}>
                    {interrupted ? "Interrupted — resuming" : "Running"}
                    {r.last_round != null ? ` · Round ${r.last_round + 1}` : ""}
                  </span>
                  <span className="flex items-center gap-0.5 text-[11px] font-semibold text-emerald-400 group-hover:underline">
                    Open ↗
                  </span>
                </div>
              </button>
            )
          })}
        </div>
      )}

      {/* Trigger pill */}
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label={`${runs.length} workflow${runs.length > 1 ? "s" : ""} running`}
        className="flex items-center gap-2 rounded-full pl-3 pr-3.5 py-2 shadow-lg transition-transform active:scale-95"
        style={{ background: "#1a1a1a", border: "1px solid var(--border, #2a2a2a)" }}
      >
        <span className="relative flex items-center justify-center">
          <Workflow size={15} className="text-emerald-400" />
          <span className="absolute -top-2 -right-2 flex items-center justify-center min-w-[16px] h-[16px] px-1 rounded-full text-[10px] font-semibold text-black bg-emerald-400">
            {runs.length}
          </span>
        </span>
        <span
          className="flex flex-col items-start leading-[1.1] text-[11px] font-medium text-left"
          style={{ color: "var(--text-secondary, #cfcfcf)" }}
        >
          <span>Workflows</span>
          <span>Running</span>
        </span>
      </button>
    </div>
  )
}
