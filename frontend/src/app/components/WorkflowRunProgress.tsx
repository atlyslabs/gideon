"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { Brain, Check, Loader2, Wrench, X } from "lucide-react"
import { getIntegrationIcon } from "../utils/integrationIcons"
import { pingman } from "../utils/pingman"

interface Step {
  round: number
  kind: "thinking" | "text" | "tool"
  name?: string
  ok?: boolean | null // tool only: true=ok, false=error, null=in-flight
  text?: string
}

interface Progress {
  status: string
  last_round: number | null
  resume_count: number
  is_active: boolean
  steps: Step[]
}

const POLL_MS = 3000

// Light tool-name → integration mapping for a nicer icon. Falls back to a
// generic wrench. We only need the common providers; unknown tools stay generic.
function iconForTool(name: string): string {
  const n = name.toLowerCase()
  if (n.includes("slack")) return "slack"
  if (n.includes("email") || n.includes("gmail")) return "gmail"
  if (n.includes("calendar")) return "google_calendar"
  if (n.includes("sheet")) return "google_sheets"
  if (n.includes("doc")) return "google_docs"
  if (n.includes("drive") || n.includes("file")) return "google_drive"
  if (n.includes("linear")) return "linear"
  if (n.includes("github") || n.includes("_pr") || n.includes("pull_request")) return "github"
  if (n.includes("notion")) return "notion"
  if (n.includes("grafana") || n.includes("panel")) return "grafana"
  if (n.includes("figma")) return "figma"
  if (n.includes("search") || n.includes("web_") || n.includes("exa")) return ""
  return ""
}

function ToolStatus({ ok }: { ok?: boolean | null }) {
  if (ok === null || ok === undefined)
    return <Loader2 size={13} className="animate-spin text-amber-400 shrink-0" />
  if (ok) return <Check size={13} className="text-emerald-400 shrink-0" />
  return <X size={13} className="text-red-400 shrink-0" />
}

interface Props {
  workflowId: string
  runId: string
  onComplete: () => void // fired once when the run reaches a terminal state
}

export default function WorkflowRunProgress({ workflowId, runId, onComplete }: Props) {
  const [progress, setProgress] = useState<Progress | null>(null)
  const completedRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const poll = async () => {
      try {
        const res = await pingman.get<Progress>(
          `/api/gideon/v1/workflows/${workflowId}/runs/${runId}/progress`
        )
        if (cancelled) return
        if (res.ok) {
          setProgress(res.data)
          if (!res.data.is_active && !completedRef.current) {
            completedRef.current = true
            onComplete() // run finished — let the parent reload report/artifacts
            return // stop polling
          }
        }
      } catch (e) {
        // Transient (network blip / brief 5xx) — keep polling. Log for
        // diagnostics so a persistent failure isn't silently invisible.
        console.debug("[WorkflowRunProgress] progress poll failed, will retry:", e)
      }
      if (!cancelled) timer = setTimeout(poll, POLL_MS)
    }

    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [workflowId, runId, onComplete])

  if (!progress) {
    return (
      <div className="flex items-center gap-2 text-[13px] text-gray-500 py-4">
        <Loader2 size={14} className="animate-spin" /> Loading progress…
      </div>
    )
  }

  const interrupted = progress.status === "interrupted"
  const steps = progress.steps

  return (
    <div className="mb-8 rounded-2xl border border-[#222] bg-[#121212] p-5">
      {/* Live header */}
      <div className="flex items-center gap-2 mb-4">
        <span className="relative flex size-2.5">
          <span
            className={`absolute inline-flex h-full w-full rounded-full opacity-60 animate-ping ${
              interrupted ? "bg-amber-400" : "bg-emerald-400"
            }`}
          />
          <span
            className={`relative inline-flex size-2.5 rounded-full ${
              interrupted ? "bg-amber-400" : "bg-emerald-400"
            }`}
          />
        </span>
        <span className="text-[13px] font-medium text-white">
          {interrupted ? "Interrupted — resuming…" : "Running…"}
        </span>
        {progress.last_round != null && (
          <span className="text-[12px] text-gray-500">· round {progress.last_round + 1}</span>
        )}
        {progress.resume_count > 0 && (
          <span className="text-[11px] text-amber-300/80 ml-1">
            resumed {progress.resume_count}×
          </span>
        )}
      </div>

      {/* Activity timeline */}
      {steps.length === 0 ? (
        <p className="text-[13px] text-gray-500">Getting started…</p>
      ) : (
        <ol className="space-y-1.5">
          {steps.map((s, i) => {
            if (s.kind === "tool") {
              const icon = iconForTool(s.name || "")
              return (
                <li key={i} className="flex items-center gap-2 text-[13px]">
                  <span className="flex items-center justify-center size-5 shrink-0">
                    {icon ? getIntegrationIcon(icon, 14) : <Wrench size={13} className="text-gray-500" />}
                  </span>
                  <span className="font-mono text-[12px] text-gray-300">{s.name}</span>
                  <ToolStatus ok={s.ok} />
                </li>
              )
            }
            if (s.kind === "thinking") {
              return (
                <li key={i} className="flex items-start gap-2 text-[12px] text-gray-500 italic">
                  <Brain size={13} className="shrink-0 mt-0.5 text-gray-600" />
                  <span className="line-clamp-2">{s.text}</span>
                </li>
              )
            }
            return (
              <li key={i} className="flex items-start gap-2 text-[13px] text-gray-300 pl-7">
                <span className="line-clamp-3">{s.text}</span>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}
