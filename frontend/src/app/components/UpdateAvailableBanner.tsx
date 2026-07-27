"use client"

import { RefreshCw } from "lucide-react"

/**
 * Small banner shown when `useVersionCheck` detects a newer deployed version.
 * Pinned bottom-right, dismissable by clicking Refresh. Kept intentionally
 * non-blocking — users can finish what they're doing first.
 */
export default function UpdateAvailableBanner() {
  return (
    <div
      className="fixed bottom-4 right-4 z-[90] flex items-center gap-3 px-4 py-2.5 rounded-xl"
      style={{
        background: "rgba(80, 87, 234, 0.95)",
        boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
        border: "1px solid rgba(255,255,255,0.15)",
        backdropFilter: "blur(8px)",
        color: "#ffffff",
      }}
      role="status"
      aria-live="polite"
    >
      <span className="text-[13px]">A new version of Gideon is ready.</span>
      <button
        onClick={() => window.location.reload()}
        className="flex items-center gap-1.5 px-3 py-1 rounded-lg text-[12px] font-medium transition-colors"
        style={{
          background: "rgba(255,255,255,0.18)",
          color: "#ffffff",
        }}
      >
        <RefreshCw size={12} aria-hidden="true" />
        Refresh
      </button>
    </div>
  )
}
