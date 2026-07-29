"use client"

import { useState, useCallback } from "react"
import { Plus, Loader2, X } from "lucide-react"

import useSkills from "../hooks/useSkills"
import type { Skill } from "../types"

/* ── Integration icons (shared) ───────────────────────── */

import { INTEGRATION_ICONS, getIntegrationIcon, getIntegrationLabel } from "../utils/integrationIcons"

/* ── Create Skill Modal ───────────────────────────────── */

function CreateSkillModal({
  open,
  onClose,
  onCreate,
}: {
  open: boolean
  onClose: () => void
  onCreate: (p: { name: string; description: string; prompt: string; integrations_used: string[] }) => Promise<void>
}) {
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [prompt, setPrompt] = useState("")
  const [creating, setCreating] = useState(false)

  if (!open) return null

  const handleSubmit = async () => {
    if (!name.trim()) return
    setCreating(true)
    try {
      await onCreate({
        name: name.trim(),
        description: description.trim(),
        prompt: prompt.trim(),
        integrations_used: [],
      })
      setName("")
      setDescription("")
      setPrompt("")
      onClose()
    } finally {
      setCreating(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.7)" }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-xl p-6"
        style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-[18px] font-semibold" style={{ color: "var(--text-primary)" }}>
            Create Skill
          </h2>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg transition-colors"
            style={{ color: "var(--text-secondary)" }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = "var(--surface-elevated)"
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "transparent"
            }}
          >
            <X size={18} />
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Name
            </label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Email Tone"
              className="w-full px-3 py-2 rounded-lg text-[14px]"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
              }}
            />
          </div>

          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Description
            </label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Short description of what this skill does"
              className="w-full px-3 py-2 rounded-lg text-[14px]"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
              }}
            />
          </div>

          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Prompt
            </label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="The learning prompt for this skill..."
              rows={4}
              className="w-full px-3 py-2 rounded-lg text-[14px] resize-none"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
              }}
            />
          </div>
        </div>

        <div className="flex justify-end gap-3 mt-6">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-[13px] font-medium"
            style={{ color: "var(--text-secondary)", border: "1px solid var(--border)" }}
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!name.trim() || creating}
            className="px-4 py-2 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-50"
            style={{ background: "#7c3aed", color: "white" }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = "#6d28d9"
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "#7c3aed"
            }}
          >
            {creating ? (
              <span className="flex items-center gap-2">
                <Loader2 size={14} className="animate-spin" />
                Creating
              </span>
            ) : (
              "Create Skill"
            )}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ── Skills Page (list) ───────────────────────────────── */

interface SkillsPageProps {
  onBack: () => void
  onSelectSkill: (skill: Skill) => void
  onCreateSkill: () => void
}

export default function SkillsPage({ onBack, onSelectSkill, onCreateSkill }: SkillsPageProps) {
  const { skills, loading, initializeDefaults } = useSkills()
  const [initializing, setInitializing] = useState(false)

  const handleInitDefaults = useCallback(async () => {
    setInitializing(true)
    try {
      await initializeDefaults()
    } finally {
      setInitializing(false)
    }
  }, [initializeDefaults])

  return (
    <div className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden" style={{ background: "var(--surface)" }}>
      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {/* Header area */}
        <div className="max-w-4xl mx-auto px-8 pt-12 pb-6">
          <div className="flex items-start justify-between">
            <div>
              <h1 className="text-[32px] font-bold tracking-tight" style={{ color: "var(--text-primary)" }}>
                Skills
              </h1>
              <p className="text-[15px] mt-2" style={{ color: "var(--text-secondary)" }}>
                Create a skill for your agent to learn.
              </p>
            </div>
            <button
              onClick={onCreateSkill}
              className="flex items-center gap-2 px-4 py-2.5 rounded-lg text-[13px] font-medium transition-colors"
              style={{ background: "#7c3aed", color: "white" }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "#6d28d9"
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "#7c3aed"
              }}
            >
              <Plus size={16} />
              Create Skill
            </button>
          </div>
        </div>

        {/* Grid */}
        <div className="max-w-4xl mx-auto px-8 pb-12">
          {loading ? (
            <div className="flex items-center justify-center py-20">
              <div className="spinner" />
            </div>
          ) : skills.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20">
              <p className="text-[14px] mb-4" style={{ color: "var(--text-tertiary)" }}>
                No skills yet. Initialize default skills or create your own.
              </p>
              <button
                onClick={handleInitDefaults}
                disabled={initializing}
                className="px-4 py-2 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-50"
                style={{ background: "#7c3aed", color: "white" }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "#6d28d9"
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "#7c3aed"
                }}
              >
                {initializing ? (
                  <span className="flex items-center gap-2">
                    <Loader2 size={14} className="animate-spin" />
                    Initializing...
                  </span>
                ) : (
                  "Initialize Default Skills"
                )}
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {skills.map((skill) => (
                <button
                  key={skill.id}
                  onClick={() => onSelectSkill(skill)}
                  className="text-left rounded-xl p-5 transition-all group"
                  style={{
                    background: "var(--surface-card)",
                    border: "1px solid var(--border)",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = "#333"
                    e.currentTarget.style.background = "var(--surface-elevated)"
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = "var(--border)"
                    e.currentTarget.style.background = "var(--surface-card)"
                  }}
                >
                  {/* Integration icons */}
                  {skill.integrations_used.length > 0 && (
                    <div className="flex items-center gap-2 mb-3">
                      {skill.integrations_used.map((integ) => (
                        <div
                          key={integ}
                          className="w-8 h-8 rounded-lg flex items-center justify-center"
                          style={{ background: "var(--surface-elevated)" }}
                        >
                          {getIntegrationIcon(integ)}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* No integrations -- show a sparkle icon */}
                  {skill.integrations_used.length === 0 && (
                    <div className="flex items-center gap-2 mb-3">
                      <div
                        className="w-8 h-8 rounded-lg flex items-center justify-center"
                        style={{ background: "var(--surface-elevated)" }}
                      >
                        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#7c3aed" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M12 3l1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5L12 3z" />
                        </svg>
                      </div>
                    </div>
                  )}

                  <h3 className="text-[15px] font-semibold mb-1" style={{ color: "var(--text-primary)" }}>
                    {skill.name}
                  </h3>
                  <p
                    className="text-[13px] leading-relaxed line-clamp-2"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    {skill.description || "No description"}
                  </p>

                  {skill.last_run_at && (
                    <p className="text-[11px] mt-3" style={{ color: "var(--text-tertiary)" }}>
                      Last run {formatRelativeDate(skill.last_run_at)}
                    </p>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

    </div>
  )
}

/* ── Helpers ──────────────────────────────────────────── */

function formatRelativeDate(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMins = Math.floor(diffMs / 60000)

  if (diffMins < 1) return "just now"
  if (diffMins < 60) return `${diffMins}m ago`

  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h ago`

  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" })
}

export { INTEGRATION_ICONS, getIntegrationIcon, getIntegrationLabel } from "../utils/integrationIcons"
