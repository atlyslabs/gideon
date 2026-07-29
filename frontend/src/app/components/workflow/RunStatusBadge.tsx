import type { WorkflowRun } from "../../types"

export default function RunStatusBadge({ status }: { status: WorkflowRun["status"] }) {
  const colors: Record<string, { bg: string; text: string }> = {
    running: { bg: "rgba(59,130,246,0.12)", text: "#3b82f6" },
    pending_approval: { bg: "rgba(245,158,11,0.12)", text: "#f59e0b" },
    approved: { bg: "rgba(16,185,129,0.12)", text: "#10b981" },
    rejected: { bg: "rgba(239,68,68,0.12)", text: "#ef4444" },
    completed: { bg: "rgba(16,185,129,0.12)", text: "#10b981" },
    failed: { bg: "rgba(239,68,68,0.12)", text: "#ef4444" },
  }
  const c = colors[status] || colors.running
  const label = status.replace(/_/g, " ").replace(/\b\w/g, l => l.toUpperCase())
  return (
    <span
      className="text-[11px] font-medium px-2 py-0.5 rounded-full"
      style={{ background: c.bg, color: c.text }}
    >
      {label}
    </span>
  )
}
