"use client"

import { AlertTriangle, Loader2 } from "lucide-react"
import type { ProviderConfig } from "./providerConfig"

export function Section({ title, children, error }: { title: string; children: React.ReactNode; error?: boolean }) {
  return (
    <section>
      <h2 className="text-[14px] font-semibold mb-3 flex items-center gap-2" style={{ color: error ? "#EF4444" : "var(--text-primary)" }}>
        {error && <AlertTriangle size={14} />}
        {title}
      </h2>
      <div
        className="rounded-xl p-1.5"
        style={{
          background: "var(--surface-card)",
          border: error ? "1px solid rgba(239,68,68,0.25)" : "1px solid var(--border)",
        }}
      >
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3">{children}</div>
      </div>
    </section>
  )
}

export function IntegrationCard({
  provider,
  buttonLabel,
  buttonLoading,
  buttonVariant,
  errorCount,
  onAction,
}: {
  provider: ProviderConfig
  buttonLabel: string
  buttonLoading?: boolean
  buttonVariant: "primary" | "secondary" | "muted" | "error"
  errorCount?: number
  onAction: () => void
}) {
  const btnStyle =
    buttonVariant === "primary"
      ? { background: "var(--accent)", color: "#000", border: "none" }
      : buttonVariant === "error"
        ? { background: "rgba(239,68,68,0.15)", color: "#EF4444", border: "1px solid rgba(239,68,68,0.3)" }
        : buttonVariant === "secondary"
          ? { background: "rgba(255,255,255,0.06)", color: "var(--text-secondary)", border: "1px solid var(--border)" }
          : { background: "rgba(255,255,255,0.04)", color: "var(--text-tertiary)", border: "1px solid transparent" }

  const iconBg = provider.darkIcon
    ? { background: "linear-gradient(to bottom right, #2a2a2a, #1e1e1e)", border: "1px solid rgba(255,255,255,0.08)" }
    : { background: "linear-gradient(to bottom right, #fff, #e8e8e8)", border: "1px solid rgba(0,0,0,0.06)" }

  return (
    <div className="flex items-center gap-3 px-4 py-3.5 rounded-lg cursor-pointer transition hover:bg-[rgba(255,255,255,0.04)]" onClick={onAction}>
      <div className="relative w-11 h-11 shrink-0">
        <div className="w-11 h-11 rounded-xl flex items-center justify-center" style={iconBg}>
          {provider.icon}
        </div>
        {errorCount != null && errorCount > 0 && (
          <span
            className="absolute -top-1 -right-1 min-w-[18px] h-[18px] flex items-center justify-center rounded-full text-[10px] font-bold px-1"
            style={{ background: "#EF4444", color: "#fff" }}
          >
            {errorCount > 9 ? "9+" : errorCount}
          </span>
        )}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-[13px] font-medium truncate" style={{ color: "var(--text-primary)" }}>{provider.name}</p>
        <p className="text-[11px] truncate" style={{ color: buttonVariant === "error" ? "#EF4444" : "var(--text-secondary)" }}>
          {buttonVariant === "error" ? "Sync failing, reconnect required" : provider.description}
        </p>
        {buttonVariant === "primary" && provider.connectHint && (
          <p className="text-[10px] truncate" style={{ color: "var(--text-tertiary)" }}>{provider.connectHint}</p>
        )}
      </div>
      <button
        onClick={(e) => { e.stopPropagation(); onAction() }}
        disabled={buttonLoading}
        className="shrink-0 px-3.5 py-1.5 rounded-lg text-[12px] font-medium transition-colors disabled:opacity-60"
        style={btnStyle}
      >
        {buttonLoading ? <Loader2 size={13} className="animate-spin" /> : buttonLabel}
      </button>
    </div>
  )
}
