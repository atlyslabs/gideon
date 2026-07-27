"use client"

import { useRef, useEffect, useState } from "react"
import {
  MoreHorizontal, Share2, Loader2,
  Trash2, Play, Check, Copy,
} from "lucide-react"
import toast from "react-hot-toast"
import type { Workflow } from "../../types"

interface WorkflowActionsProps {
  workflow: Workflow | null
  showMenu: boolean
  setShowMenu: (v: boolean) => void
  isDeleting: boolean
  isActivating: boolean
  isRunning: boolean
  onDelete: () => void
  onActivate: () => void
  onRun: () => void
  // Called by the Share button before copying the URL, to flip the
  // `link_shared` opt-in on the workflow. Without this, recipients would
  // 404 the shared link (workflows are owner-only by default).
  onEnableShare?: (linkShared: boolean) => Promise<void>
  // Template mode: this workflow belongs to someone else and the viewer is
  // looking at a shared read-only preview. Hides owner actions; shows only
  // a "Copy to my workflows" CTA.
  templateMode?: boolean
  cloning?: boolean
  onClone?: () => void
}

export default function WorkflowActions({
  workflow,
  showMenu,
  setShowMenu,
  isDeleting,
  isActivating,
  isRunning,
  onDelete,
  onActivate,
  onRun,
  onEnableShare,
  templateMode,
  cloning,
  onClone,
}: WorkflowActionsProps) {
  const menuRef = useRef<HTMLDivElement>(null)
  const [justCopied, setJustCopied] = useState(false)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setShowMenu(false)
    }
    if (showMenu) document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [showMenu, setShowMenu])

  const handleShare = async () => {
    if (!workflow) return
    // Flip link_shared on first click if it's off. Without this, recipients
    // of the URL 404 because workflows are owner-only by default.
    if (!workflow.link_shared && onEnableShare) {
      try {
        await onEnableShare(true)
      } catch {
        toast.error("Couldn't enable sharing — try again")
        return
      }
    }
    const url = `${window.location.origin}/workflows/${workflow.id}`
    try {
      await navigator.clipboard.writeText(url)
      setJustCopied(true)
      toast.success("Workflow link copied. Anyone in your workspace with the link can view and clone it.")
      setTimeout(() => setJustCopied(false), 2000)
    } catch {
      toast.error("Couldn't copy link — copy from the address bar instead")
    }
  }

  // Template (shared) view — only a "Copy to my workflows" button.
  if (templateMode) {
    return (
      <div className="flex items-center gap-2 shrink-0">
        <button
          onClick={onClone}
          disabled={cloning || !workflow}
          className="flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-50"
          style={{ background: "#7c3aed", color: "white", border: "none" }}
          onMouseEnter={(e) => { if (!cloning) e.currentTarget.style.background = "#6d28d9" }}
          onMouseLeave={(e) => { if (!cloning) e.currentTarget.style.background = "#7c3aed" }}
          title="Create a copy of this workflow in your account"
        >
          {cloning ? <Loader2 size={14} className="animate-spin" /> : <Copy size={14} />}
          {cloning ? "Copying..." : "Copy to my workflows"}
        </button>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2 shrink-0 relative">
      {/* More menu */}
      <div ref={menuRef}>
        <button
          onClick={() => setShowMenu(!showMenu)}
          className="p-2 rounded-lg transition-colors"
          style={{ border: "1px solid var(--border)", color: "var(--text-secondary)" }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-elevated)" }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
        >
          <MoreHorizontal size={16} />
        </button>

        {showMenu && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setShowMenu(false)} />
            <div
              className="absolute right-0 top-full mt-1 w-40 rounded-lg py-1 z-50"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
              }}
            >
              <button
                onClick={() => { setShowMenu(false); onDelete() }}
                disabled={isDeleting}
                className="w-full flex items-center gap-2.5 text-left px-4 py-2 text-[13px] transition-colors disabled:opacity-50"
                style={{ color: "var(--error)" }}
                onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-hover)" }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
              >
                <Trash2 size={14} />
                {isDeleting ? "Deleting..." : "Delete"}
              </button>
            </div>
          </>
        )}
      </div>

      {/* Share button — copies the workflow URL to clipboard */}
      <button
        onClick={handleShare}
        disabled={!workflow}
        className="flex items-center gap-2 px-3 py-2 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-50"
        style={{ border: "1px solid var(--border)", color: "var(--text-secondary)" }}
        onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-elevated)" }}
        onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
        title="Copy workflow link"
      >
        {justCopied ? <Check size={14} /> : <Share2 size={14} />}
        {justCopied ? "Copied" : "Share"}
      </button>

      {/* Run Now */}
      {workflow && workflow.status === "active" && (
        <button
          onClick={onRun}
          disabled={isRunning}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-50"
          style={{ border: "1px solid var(--border)", color: "var(--text-secondary)" }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-elevated)" }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
        >
          {isRunning ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <Play size={14} />
          )}
          {isRunning ? "Running..." : "Run Now"}
        </button>
      )}

      {/* Activate / Deactivate */}
      {workflow && (
        <button
          onClick={onActivate}
          disabled={isActivating}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-50"
          style={{
            background: workflow.status === "active" ? "var(--surface-elevated)" : "#7c3aed",
            color: workflow.status === "active" ? "var(--text-secondary)" : "white",
            border: workflow.status === "active" ? "1px solid var(--border)" : "none",
          }}
          onMouseEnter={(e) => {
            if (workflow.status !== "active") e.currentTarget.style.background = "#6d28d9"
            else e.currentTarget.style.background = "var(--surface-hover)"
          }}
          onMouseLeave={(e) => {
            if (workflow.status !== "active") e.currentTarget.style.background = "#7c3aed"
            else e.currentTarget.style.background = "var(--surface-elevated)"
          }}
        >
          {isActivating ? (
            <Loader2 size={14} className="animate-spin" />
          ) : workflow.status === "active" ? (
            <>Deactivate</>
          ) : (
            <>
              <Play size={14} />
              Activate
            </>
          )}
        </button>
      )}
    </div>
  )
}
