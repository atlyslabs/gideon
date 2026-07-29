"use client"

import { useState, useCallback, useEffect, useRef } from "react"
import { ArrowLeft, MoreHorizontal, Share2, Loader2, Check, X, Trash2, Command, CornerDownLeft } from "lucide-react"
import useSkills from "../hooks/useSkills"
import type { Skill } from "../types"
import { getIntegrationIcon, getIntegrationLabel } from "./SkillsPage"

/* ── Memory Update Modal ─────────────────────────────── */

interface MemoryModalProps {
  open: boolean
  onClose: () => void
  content: string
}

function MemoryModal({ open, onClose, content }: MemoryModalProps) {
  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.75)" }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl max-h-[80vh] flex flex-col rounded-xl"
        style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-6 py-4 shrink-0"
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <h3 className="text-[16px] font-semibold" style={{ color: "var(--text-primary)" }}>
            Memory Update
          </h3>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg transition-colors"
            style={{ color: "var(--text-secondary)" }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-elevated)" }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
          >
            <X size={18} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6">
          <div
            className="rounded-lg p-5 prose text-[14px] leading-relaxed"
            style={{
              background: "var(--surface-card)",
              border: "1px solid var(--border-subtle)",
              color: "var(--text-secondary)",
            }}
          >
            <MarkdownContent content={content} />
          </div>
        </div>
      </div>
    </div>
  )
}

/* ── Simple markdown renderer ────────────────────────── */

function MarkdownContent({ content }: { content: string }) {
  const lines = content.split("\n")
  const elements: React.ReactNode[] = []
  let inCodeBlock = false
  let codeBuffer: string[] = []
  let listBuffer: string[] = []

  const flushList = () => {
    if (listBuffer.length > 0) {
      elements.push(
        <ul key={`list-${elements.length}`} style={{ listStyle: "disc", paddingLeft: "1.5em", margin: "8px 0" }}>
          {listBuffer.map((item, i) => (
            <li key={i} style={{ margin: "4px 0" }}>
              <InlineMarkdown text={item} />
            </li>
          ))}
        </ul>
      )
      listBuffer = []
    }
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    if (line.startsWith("```")) {
      if (inCodeBlock) {
        elements.push(
          <pre
            key={`code-${i}`}
            style={{
              background: "var(--surface-elevated)",
              border: "1px solid var(--border)",
              borderRadius: "8px",
              padding: "12px 16px",
              overflowX: "auto",
              margin: "8px 0",
              fontSize: "0.85em",
            }}
          >
            <code>{codeBuffer.join("\n")}</code>
          </pre>
        )
        codeBuffer = []
        inCodeBlock = false
      } else {
        flushList()
        inCodeBlock = true
      }
      continue
    }

    if (inCodeBlock) {
      codeBuffer.push(line)
      continue
    }

    if (/^[-*]\s/.test(line)) {
      listBuffer.push(line.replace(/^[-*]\s+/, ""))
      continue
    } else {
      flushList()
    }

    if (line.startsWith("### ")) {
      elements.push(
        <h3 key={i} style={{ fontWeight: 600, fontSize: "15px", margin: "16px 0 8px", color: "var(--text-primary)" }}>
          <InlineMarkdown text={line.slice(4)} />
        </h3>
      )
    } else if (line.startsWith("## ")) {
      elements.push(
        <h2 key={i} style={{ fontWeight: 600, fontSize: "16px", margin: "16px 0 8px", color: "var(--text-primary)" }}>
          <InlineMarkdown text={line.slice(3)} />
        </h2>
      )
    } else if (line.startsWith("# ")) {
      elements.push(
        <h1 key={i} style={{ fontWeight: 700, fontSize: "18px", margin: "16px 0 8px", color: "var(--text-primary)" }}>
          <InlineMarkdown text={line.slice(2)} />
        </h1>
      )
    } else if (line.trim() === "") {
      // skip empty lines
    } else {
      elements.push(
        <p key={i} style={{ margin: "8px 0" }}>
          <InlineMarkdown text={line} />
        </p>
      )
    }
  }

  flushList()

  return <>{elements}</>
}

function InlineMarkdown({ text }: { text: string }) {
  const parts: React.ReactNode[] = []
  let remaining = text
  let key = 0

  while (remaining.length > 0) {
    const boldMatch = remaining.match(/\*\*(.+?)\*\*/)
    const codeMatch = remaining.match(/`(.+?)`/)

    let firstMatch: { index: number; length: number; type: "bold" | "code"; content: string } | null = null

    if (boldMatch?.index !== undefined) {
      firstMatch = { index: boldMatch.index, length: boldMatch[0].length, type: "bold", content: boldMatch[1] }
    }
    if (codeMatch?.index !== undefined) {
      if (!firstMatch || codeMatch.index < firstMatch.index) {
        firstMatch = { index: codeMatch.index, length: codeMatch[0].length, type: "code", content: codeMatch[1] }
      }
    }

    if (firstMatch) {
      if (firstMatch.index > 0) {
        parts.push(<span key={key++}>{remaining.slice(0, firstMatch.index)}</span>)
      }
      if (firstMatch.type === "bold") {
        parts.push(<strong key={key++} style={{ color: "var(--text-primary)", fontWeight: 600 }}>{firstMatch.content}</strong>)
      } else {
        parts.push(
          <code
            key={key++}
            style={{
              color: "var(--accent)",
              background: "var(--surface-elevated)",
              padding: "2px 6px",
              borderRadius: "4px",
              fontSize: "0.85em",
            }}
          >
            {firstMatch.content}
          </code>
        )
      }
      remaining = remaining.slice(firstMatch.index + firstMatch.length)
    } else {
      parts.push(<span key={key++}>{remaining}</span>)
      break
    }
  }

  return <>{parts}</>
}

/* ── History Tab ─────────────────────────────────────── */

interface HistoryTabProps {
  skill: Skill | null
  onShowMemory: () => void
}

function HistoryTab({ skill, onShowMemory }: HistoryTabProps) {
  if (!skill || !skill.last_run_at) {
    return (
      <div className="flex items-center justify-center py-16">
        <p className="text-[14px]" style={{ color: "var(--text-tertiary)" }}>
          {!skill ? "No history yet -- this skill hasn't been created." : "No learning history yet. Run the skill to start learning."}
        </p>
      </div>
    )
  }

  const runDate = new Date(skill.last_run_at)
  const runLabel = runDate.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " at " +
    runDate.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true })

  return (
    <div className="flex gap-6 mt-2">
      {/* Left: Skill Update card */}
      <div
        className="w-64 shrink-0 rounded-xl p-5"
        style={{ background: "var(--surface-card)", border: "1px solid var(--border)" }}
      >
        <h4 className="text-[13px] font-semibold mb-3" style={{ color: "var(--text-primary)" }}>
          Skill Update
        </h4>
        <p className="text-[12px] mb-2" style={{ color: "var(--text-secondary)" }}>
          {runLabel}
        </p>
        <div className="flex items-center gap-1.5">
          <div
            className="w-4 h-4 rounded-full flex items-center justify-center"
            style={{ background: "rgba(16, 185, 129, 0.15)" }}
          >
            <Check size={10} style={{ color: "var(--success)" }} />
          </div>
          <span className="text-[12px] font-medium" style={{ color: "var(--success)" }}>
            Approved
          </span>
        </div>
      </div>

      {/* Right: Timeline */}
      <div className="flex-1 pt-1">
        <div className="relative pl-6">
          {/* Vertical line */}
          <div
            className="absolute left-[7px] top-2 bottom-2 w-px"
            style={{ background: "var(--border)" }}
          />

          {/* Step 1: Started */}
          <div className="relative flex items-start gap-3 mb-6">
            <div
              className="absolute left-[-17px] top-1.5 w-3 h-3 rounded-full"
              style={{ background: "var(--text-tertiary)", border: "2px solid var(--surface)" }}
            />
            <p className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
              Started learning process
            </p>
          </div>

          {/* Step 2: Memory update */}
          <div className="relative flex items-start gap-3 mb-6">
            <div
              className="absolute left-[-17px] top-1.5 w-3 h-3 rounded-full"
              style={{ background: "#7c3aed", border: "2px solid var(--surface)" }}
            />
            <button
              onClick={onShowMemory}
              className="text-[13px] transition-colors"
              style={{ color: "#7c3aed" }}
              onMouseEnter={(e) => { e.currentTarget.style.textDecoration = "underline" }}
              onMouseLeave={(e) => { e.currentTarget.style.textDecoration = "none" }}
            >
              Memory update
            </button>
          </div>

          {/* Step 3: Approved */}
          <div className="relative flex items-start gap-3">
            <div
              className="absolute left-[-17px] top-1.5 w-3 h-3 rounded-full flex items-center justify-center"
              style={{ background: "var(--success)", border: "2px solid var(--surface)" }}
            />
            <div className="flex items-center gap-1.5">
              <span className="text-[13px]" style={{ color: "var(--success)" }}>
                Approved
              </span>
              <Check size={14} style={{ color: "var(--success)" }} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

/* ── Skill Detail Page ───────────────────────────────── */

interface SkillDetailPageProps {
  /** Pass a skill ID for view/edit mode; pass null for create mode */
  skillId: string | null
  onBack: () => void
}

export default function SkillDetailPage({ skillId, onBack }: SkillDetailPageProps) {
  const { skills, getSkill, createSkill, updateSkill, learnSkill, deleteSkill, mutate } = useSkills()

  const [skill, setSkill] = useState<Skill | null>(null)
  const isCreateMode = skillId === null && !skill
  const [loading, setLoading] = useState(skillId !== null)
  const [activeTab, setActiveTab] = useState<"learn" | "history">("learn")
  const [name, setName] = useState("")
  const [prompt, setPrompt] = useState("")
  const [isLearning, setIsLearning] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [showMemoryModal, setShowMemoryModal] = useState(false)
  const [showMenu, setShowMenu] = useState(false)
  const [isDeleting, setIsDeleting] = useState(false)
  const [editingName, setEditingName] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const nameInputRef = useRef<HTMLInputElement>(null)

  // Load existing skill
  useEffect(() => {
    if (isCreateMode) return
    let cancelled = false
    setLoading(true)

    // Try from cache first
    const cached = skills.find((s) => s.id === skillId)
    if (cached) {
      setSkill(cached)
      setName(cached.name)
      setPrompt(cached.prompt)
      setLoading(false)
    }

    // Fetch fresh
    getSkill(skillId!)
      .then((fresh) => {
        if (!cancelled) {
          setSkill(fresh)
          setName(fresh.name)
          setPrompt(fresh.prompt)
          setLoading(false)
        }
      })
      .catch(() => {
        if (!cancelled) setLoading(false)
      })

    return () => { cancelled = true }
  }, [skillId, isCreateMode, skills, getSkill])

  // Auto-focus name input when editing starts
  useEffect(() => {
    if (editingName && nameInputRef.current) {
      nameInputRef.current.focus()
      nameInputRef.current.select()
    }
  }, [editingName])

  // Close menu on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setShowMenu(false)
      }
    }
    if (showMenu) document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [showMenu])

  // Keyboard shortcut: Cmd+Enter to learn
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && activeTab === "learn" && !isLearning && !isSaving) {
        e.preventDefault()
        handleLearn()
      }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  })

  const handleLearn = useCallback(async () => {
    if (isLearning || isSaving || !prompt.trim()) return

    try {
      let targetSkill = skill

      if (isCreateMode || !targetSkill) {
        // Create the skill first via POST /api/gideon/v1/skills/
        setIsSaving(true)
        targetSkill = await createSkill({
          name: name.trim() || "Untitled skill",
          description: "",
          prompt: prompt.trim(),
          integrations_used: [],
        })
        setSkill(targetSkill)
        setIsSaving(false)
      } else if (prompt.trim() !== targetSkill.prompt || name.trim() !== targetSkill.name) {
        // Update if changed
        setIsSaving(true)
        targetSkill = await updateSkill(targetSkill.id, {
          prompt: prompt.trim(),
          name: name.trim(),
        })
        setSkill(targetSkill)
        setIsSaving(false)
      }

      // Trigger learning via POST /api/gideon/v1/skills/{id}/learn
      setIsLearning(true)
      const learned = await learnSkill(targetSkill.id)
      setSkill(learned)
      setIsLearning(false)
    } catch {
      setIsLearning(false)
      setIsSaving(false)
    }
  }, [isCreateMode, skill, name, prompt, isLearning, isSaving, createSkill, updateSkill, learnSkill])

  const handleDelete = useCallback(async () => {
    if (!skill) {
      // In create mode with no saved skill, just go back
      onBack()
      return
    }
    setIsDeleting(true)
    try {
      await deleteSkill(skill.id)
      onBack()
    } catch {
      setIsDeleting(false)
    }
  }, [skill, deleteSkill, onBack])

  const handleNameBlur = useCallback(() => {
    setEditingName(false)
    // Save name if skill exists and name changed
    if (skill && name.trim() && name.trim() !== skill.name) {
      updateSkill(skill.id, { name: name.trim() }).then(setSkill).catch(() => {})
    }
  }, [name, skill, updateSkill])

  const handleNameKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault()
      ;(e.target as HTMLInputElement).blur()
    }
  }, [])

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center rounded-xl" style={{ background: "var(--surface)" }}>
        <div className="spinner" />
      </div>
    )
  }

  if (!isCreateMode && !skill) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center rounded-xl" style={{ background: "var(--surface)" }}>
        <p className="text-[14px] mb-4" style={{ color: "var(--text-tertiary)" }}>Skill not found.</p>
        <button
          onClick={onBack}
          className="text-[13px] font-medium"
          style={{ color: "#7c3aed" }}
        >
          Back to Skills
        </button>
      </div>
    )
  }

  const integrations = skill?.integrations_used || []
  const lastRunAt = skill?.last_run_at || null

  const lastRunLabel = lastRunAt
    ? (() => {
        const d = new Date(lastRunAt)
        return "Last run at " +
          d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
          " at " +
          d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true })
      })()
    : null

  return (
    <div className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden" style={{ background: "var(--surface)" }}>
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-8 pt-8 pb-12">
          {/* Back link + actions row */}
          <div className="flex items-center justify-between mb-6">
            <button
              onClick={onBack}
              className="flex items-center gap-1.5 text-[13px] transition-colors"
              style={{ color: "var(--text-secondary)" }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "var(--text-primary)" }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "var(--text-secondary)" }}
            >
              <ArrowLeft size={14} />
              All skills
            </button>

            <div className="flex items-center gap-2 shrink-0 relative">
              {/* Share button (non-functional) */}
              <button
                className="flex items-center gap-2 px-3 py-2 rounded-lg text-[13px] font-medium transition-colors"
                style={{ border: "1px solid var(--border)", color: "var(--text-secondary)" }}
                onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-elevated)" }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
              >
                <Share2 size={14} />
                Share
              </button>

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
                        onClick={() => { setShowMenu(false); handleDelete() }}
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
            </div>
          </div>

          {/* Skill name — editable heading */}
          <div className="mb-1">
            {editingName ? (
              <input
                ref={nameInputRef}
                value={name}
                onChange={(e) => setName(e.target.value)}
                onBlur={handleNameBlur}
                onKeyDown={handleNameKeyDown}
                className="w-full text-[28px] font-bold tracking-tight bg-transparent outline-none border-none"
                style={{
                  color: "var(--text-primary)",
                  caretColor: "#7c3aed",
                }}
                placeholder="Untitled skill"
              />
            ) : (
              <h1
                className="text-[28px] font-bold tracking-tight cursor-text"
                style={{ color: name ? "var(--text-primary)" : "var(--text-tertiary)" }}
                onClick={() => setEditingName(true)}
              >
                {name || "Untitled skill"}
              </h1>
            )}
          </div>

          {/* Last run timestamp — only for existing skills that have run */}
          {lastRunLabel && (
            <p className="text-[13px] mt-1 mb-6" style={{ color: "var(--text-tertiary)" }}>
              {lastRunLabel}
            </p>
          )}
          {!lastRunLabel && <div className="mb-6" />}

          {/* Tabs */}
          <div className="flex gap-6 mb-6" style={{ borderBottom: "1px solid var(--border)" }}>
            {(["learn", "history"] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className="pb-3 text-[14px] font-medium transition-colors relative"
                style={{
                  color: activeTab === tab ? "var(--text-primary)" : "var(--text-tertiary)",
                }}
              >
                {tab.charAt(0).toUpperCase() + tab.slice(1)}
                {activeTab === tab && (
                  <div
                    className="absolute bottom-0 left-0 right-0 h-[2px] rounded-full"
                    style={{ background: "#7c3aed" }}
                  />
                )}
              </button>
            ))}
          </div>

          {/* Tab content */}
          {activeTab === "learn" ? (
            <div className="space-y-6 mt-2">
              {/* Using Integrations */}
              <div>
                <h4 className="text-[12px] font-medium uppercase tracking-wider mb-3" style={{ color: "var(--text-tertiary)" }}>
                  Using Integrations
                </h4>
                {integrations.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {integrations.map((integ) => (
                      <div
                        key={integ}
                        className="flex items-center gap-2 px-3 py-1.5 rounded-full text-[13px]"
                        style={{
                          background: "var(--surface-elevated)",
                          border: "1px solid var(--border)",
                          color: "var(--text-primary)",
                        }}
                      >
                        {getIntegrationIcon(integ)}
                        {getIntegrationLabel(integ)}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-[13px]" style={{ color: "var(--text-tertiary)" }}>
                    You can mention integrations using @ in the prompt
                  </p>
                )}
              </div>

              {/* Prompt */}
              <div>
                <h4 className="text-[12px] font-medium uppercase tracking-wider mb-3" style={{ color: "var(--text-tertiary)" }}>
                  Prompt
                </h4>
                <textarea
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  rows={8}
                  className="w-full px-4 py-3 rounded-xl text-[14px] leading-relaxed resize-none outline-none transition-colors"
                  style={{
                    background: "var(--surface-card)",
                    border: "1px solid var(--border)",
                    color: "var(--text-primary)",
                  }}
                  onFocus={(e) => { e.currentTarget.style.borderColor = "#7c3aed" }}
                  onBlur={(e) => { e.currentTarget.style.borderColor = "var(--border)" }}
                  placeholder="Tell Gideon what you want it to learn..."
                />

                {/* Status indicator when learning */}
                {(skill?.status === "learning" || isLearning) && (
                  <div
                    className="flex items-center gap-2 mt-3 px-4 py-3 rounded-lg"
                    style={{ background: "rgba(124,58,237,0.08)", border: "1px solid rgba(124,58,237,0.2)" }}
                  >
                    <Loader2 size={14} className="animate-spin" style={{ color: "#7c3aed" }} />
                    <span className="text-[13px]" style={{ color: "#a78bfa" }}>
                      Learning in progress...
                    </span>
                  </div>
                )}

                <div className="flex justify-end mt-3">
                  <button
                    onClick={handleLearn}
                    disabled={isLearning || isSaving || !prompt.trim()}
                    className="flex items-center gap-2 px-5 py-2.5 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-40"
                    style={{ background: "#7c3aed", color: "white" }}
                    onMouseEnter={(e) => { if (!e.currentTarget.disabled) e.currentTarget.style.background = "#6d28d9" }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = "#7c3aed" }}
                  >
                    {isLearning || isSaving ? (
                      <>
                        <Loader2 size={14} className="animate-spin" />
                        {isSaving ? "Saving..." : "Learning..."}
                      </>
                    ) : (
                      <>
                        Learn
                        <span
                          className="flex items-center gap-0.5 text-[11px] opacity-60 ml-1 px-1.5 py-0.5 rounded"
                          style={{ background: "rgba(255,255,255,0.1)" }}
                        >
                          <Command size={10} />
                          <CornerDownLeft size={10} />
                        </span>
                      </>
                    )}
                  </button>
                </div>
              </div>

              {/* Memory Update (result of learning) */}
              {skill?.memory_content && (
                <div>
                  <h4 className="text-[12px] font-medium uppercase tracking-wider mb-3" style={{ color: "var(--text-tertiary)" }}>
                    Memory Update
                  </h4>
                  <div
                    className="rounded-xl p-5 max-h-[400px] overflow-y-auto prose text-[14px] leading-relaxed"
                    style={{
                      background: "var(--surface-card)",
                      border: "1px solid var(--border)",
                      color: "var(--text-secondary)",
                    }}
                  >
                    <MarkdownContent content={skill.memory_content} />
                  </div>
                </div>
              )}
            </div>
          ) : (
            <HistoryTab
              skill={skill}
              onShowMemory={() => setShowMemoryModal(true)}
            />
          )}
        </div>
      </div>

      {/* Memory Modal */}
      {skill?.memory_content && (
        <MemoryModal
          open={showMemoryModal}
          onClose={() => setShowMemoryModal(false)}
          content={skill.memory_content}
        />
      )}
    </div>
  )
}
